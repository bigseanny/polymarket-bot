"""
Main loop for the Polymarket near-certainty bot.

Hosted-env hardened:
  * SIGTERM / SIGINT trigger graceful shutdown (Render sends SIGTERM on redeploy)
  * Exponential backoff on crash loops
  * Hourly stale-order cancellation pass
  * Unbuffered logging → streams live to Render console

Usage:
    python bot.py                # continuous loop (Render default)
    python bot.py --once         # single scan
    python bot.py --once --dry-run
"""
from __future__ import annotations
import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from config import CFG
from scanner import scan
from sizing import size_portfolio
from executor import execute, cancel_stale_orders
from notify import notify
from bankroll import effective_bankroll


# ── Graceful shutdown ────────────────────────────────────────────────────
_stop = False


def _handle_signal(signum, _frame):
    global _stop
    logging.getLogger(__name__).info("Received signal %d — shutting down cleanly", signum)
    _stop = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def _setup_logging():
    Path(CFG.LOG_DIR).mkdir(parents=True, exist_ok=True)
    log_path = Path(CFG.LOG_DIR) / f"bot-{datetime.now(timezone.utc):%Y%m%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path),
        ],
    )


def _print_header():
    mode = "DRY-RUN (simulated)" if CFG.DRY_RUN else "LIVE TRADING"
    logging.info("=" * 78)
    logging.info("  Polymarket near-certainty bot — %s", mode)
    logging.info(
        "  Bankroll=$%s  Kelly=%s  MaxAsk=%s  MinBid=%s  MinEdge=%s  "
        "MinVol=$%s  MinLiq=$%s",
        f"{CFG.BANKROLL_USD:,.0f}", CFG.KELLY_FRACTION, CFG.MAX_ASK,
        CFG.MIN_BID, CFG.MIN_EDGE,
        f"{CFG.MIN_VOLUME_USD:,.0f}", f"{CFG.MIN_LIQUIDITY_USD:,.0f}",
    )
    logging.info("=" * 78)


def run_once() -> None:
    candidates = scan()
    logging.info("Top qualifying candidates: %d", len(candidates))
    for c in candidates[:10]:
        logging.info(
            "  apr=%+.0f%%  edge=%+.3f  %3s bid=%.3f ask=%.3f  %5.1fd  vol=$%11s  %s",
            c.annualized_return * 100, c.edge, c.outcome, c.best_bid, c.best_ask,
            c.days_to_resolution, f"{c.volume_usd:,.0f}", c.market_slug[:50],
        )

    if not candidates:
        return

    # Live bankroll: read the proxy wallet's USDC.e balance on-chain so we
    # automatically deploy freed capital after redemptions. Falls back to
    # static .env BANKROLL_USD if RPC is unreachable.
    if not CFG.DRY_RUN and CFG.FUNDER_ADDRESS:
        live_bankroll = effective_bankroll(CFG.FUNDER_ADDRESS, fallback=CFG.BANKROLL_USD)
    else:
        live_bankroll = CFG.BANKROLL_USD

    sized = size_portfolio(candidates, bankroll=live_bankroll)
    logging.info(
        "Sizing → %d orders, total $%.2f of $%.2f bankroll",
        len(sized), sum(s.usd for s in sized), live_bankroll,
    )
    if not sized:
        return

    results = execute(sized)
    submitted = [r for r in results if r.get("status") in ("submitted", "simulated")]
    logging.info(
        "%s: %d/%d orders",
        "Simulated" if CFG.DRY_RUN else "Submitted",
        len(submitted), len(results),
    )

    audit = Path(CFG.LOG_DIR) / f"orders-{datetime.now(timezone.utc):%Y%m%d}.jsonl"
    with audit.open("a") as f:
        for r in results:
            f.write(json.dumps(r, default=str) + "\n")


def main_loop():
    _print_header()
    mode = "DRY-RUN" if CFG.DRY_RUN else "LIVE"
    notify(
        f"\U0001F680 <b>Polymarket bot started</b> ({mode})\n"
        f"Bankroll: <b>${CFG.BANKROLL_USD:,.0f}</b> \u00b7 Kelly {CFG.KELLY_FRACTION} "
        f"\u00b7 MaxAsk {CFG.MAX_ASK} \u00b7 MinEdge {CFG.MIN_EDGE}",
        silent=True,
    )
    last_cleanup = 0.0
    failures = 0

    while not _stop:
        loop_start = time.time()
        try:
            run_once()

            # Hourly stale-order cleanup (live mode only).
            if not CFG.DRY_RUN and time.time() - last_cleanup > 3600:
                n = cancel_stale_orders()
                if n:
                    logging.info("Stale-order cleanup: cancelled %d", n)
                last_cleanup = time.time()

            failures = 0
        except KeyboardInterrupt:
            return
        except Exception as exc:
            failures += 1
            logging.exception("Scan loop error (%d consecutive)", failures)
            if failures in (1, 3, 5):  # ping on first error + if it persists
                notify(f"\u26A0\uFE0F <b>Scan error (#{failures})</b>\n<code>{str(exc)[:300]}</code>")
            # Exponential backoff on repeated failures (max 10 min).
            backoff = min(600, CFG.POLL_SECONDS * (2 ** min(failures, 4)))
            logging.info("Backing off %ds", backoff)
            _sleep_interruptible(backoff)
            continue

        # Normal sleep until next poll, but wake on shutdown signal.
        elapsed = time.time() - loop_start
        remaining = max(1, CFG.POLL_SECONDS - int(elapsed))
        _sleep_interruptible(remaining)

    logging.info("Bot exited cleanly.")
    notify("\U0001F6D1 <b>Polymarket bot stopped</b> (clean shutdown)", silent=True)


def _sleep_interruptible(seconds: int) -> None:
    for _ in range(seconds):
        if _stop:
            return
        time.sleep(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="Run a single scan and exit.")
    ap.add_argument("--dry-run", action="store_true", help="Force simulation regardless of env.")
    args = ap.parse_args()

    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
        import importlib, config
        importlib.reload(config)

    _setup_logging()

    if args.once:
        _print_header()
        run_once()
        return

    main_loop()


if __name__ == "__main__":
    main()

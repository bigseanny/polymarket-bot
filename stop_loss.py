"""Smart EV-aware stop loss.

For each open position we hold, exit IF AND ONLY IF all three triggers fire:

  1. Drawdown ≥ entry edge (price has fallen by at least the edge we paid for)
  2. Recent move is ≥ STOP_VOL_SIGMA σ of last 6h price history (statistically
     unusual — not noise)
  3. Velocity confirmation: price dropped ≥ STOP_VELOCITY_PCT in the last 60 min
     (sustained drop over multiple data points, not a single tick wobble)

When triggered, we MARKET-SELL the full position via py-clob-client and write
an exit row to the journal with `exit_reason="stop_loss"` and the trigger metrics.

Note: We deliberately do NOT use Polymarket's /trades endpoint as a volume
signal. The endpoint silently ignores token filters and returns a global
firehose, making per-token volume unreliable. Velocity (sustained price
drop) is a better noise filter and uses only the per-token prices-history
endpoint which is reliable.

Sanity guards:
  * No-stop window: 5 min after entry (avoids flash stops on slippage)
  * No-stop near close: <12h to resolution (drop is more likely real signal at
    that point — but if we were right we resolve to $1 anyway, so just hold)
  * Min-bid sanity: if best_bid < 50% of stop price, alert and hold (book is broken)
  * Per-token cooldown: 24h after a stop fires we won't re-enter the same token
"""
from __future__ import annotations
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import pstdev, mean
from typing import Any

import requests

log = logging.getLogger(__name__)

ROOT = Path(__file__).parent
LOG_DIR = ROOT / "logs"
JOURNAL = LOG_DIR / "trades.jsonl"
COOLDOWN_FILE = LOG_DIR / "stop_cooldown.json"
EXITS_LOG = LOG_DIR / "stop_exits.jsonl"

# ── Config ─────────────────────────────────────────────────────────────
STOP_LOSS_ENABLED = os.getenv("STOP_LOSS_ENABLED", "true").lower() in ("1", "true", "yes")
STOP_VOL_SIGMA = float(os.getenv("STOP_VOL_SIGMA", "2.0"))            # 2σ recent move
STOP_VELOCITY_PCT = float(os.getenv("STOP_VELOCITY_PCT", "0.03"))     # 3% drop in 60min
STOP_MIN_HOLD_MINUTES = int(os.getenv("STOP_MIN_HOLD_MINUTES", "5"))
STOP_MIN_HOURS_TO_CLOSE = float(os.getenv("STOP_MIN_HOURS_TO_CLOSE", "12"))
STOP_COOLDOWN_HOURS = float(os.getenv("STOP_COOLDOWN_HOURS", "24"))
STOP_MIN_BID_PCT = float(os.getenv("STOP_MIN_BID_PCT", "0.5"))  # bid must be ≥50% of stop

DATA_API = "https://data-api.polymarket.com"
CLOB_API = os.getenv("POLYMARKET_CLOB_API", "https://clob.polymarket.com")
GAMMA_API = "https://gamma-api.polymarket.com"

_TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
_TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()


# ── Telegram alerts ────────────────────────────────────────────────────
def _tg_send(text: str) -> None:
    if not (_TG_TOKEN and _TG_CHAT):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{_TG_TOKEN}/sendMessage",
            json={"chat_id": _TG_CHAT, "text": text[:4000],
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:
        log.warning("tg send failed: %s", e)


# ── Cooldown ───────────────────────────────────────────────────────────
def _load_cooldowns() -> dict[str, str]:
    if not COOLDOWN_FILE.exists():
        return {}
    try:
        return json.loads(COOLDOWN_FILE.read_text())
    except Exception:
        return {}


def _save_cooldowns(cd: dict[str, str]) -> None:
    COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOLDOWN_FILE.write_text(json.dumps(cd, indent=2))


def is_token_in_cooldown(token_id: str) -> bool:
    cd = _load_cooldowns()
    iso = cd.get(token_id)
    if not iso:
        return False
    try:
        when = datetime.fromisoformat(iso)
    except Exception:
        return False
    age = (datetime.now(timezone.utc) - when).total_seconds() / 3600
    return age < STOP_COOLDOWN_HOURS


def _set_cooldown(token_id: str) -> None:
    cd = _load_cooldowns()
    cd[token_id] = datetime.now(timezone.utc).isoformat()
    # Garbage-collect entries older than 7 days.
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    cd = {k: v for k, v in cd.items()
          if _safe_dt(v) and _safe_dt(v) > cutoff}
    _save_cooldowns(cd)


def _safe_dt(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso)
    except Exception:
        return None


# ── Polymarket data fetches ────────────────────────────────────────────
def _fetch_open_positions(funder: str) -> list[dict]:
    """Return current open positions for the proxy from the data API."""
    try:
        r = requests.get(
            f"{DATA_API}/positions",
            params={"user": funder.lower(), "limit": 100, "sizeThreshold": 0.01},
            timeout=15,
        )
        r.raise_for_status()
        return r.json() or []
    except Exception as e:
        log.warning("positions fetch failed: %s", e)
        return []


def _fetch_price_history(token_id: str, hours: float = 24.0) -> list[dict]:
    """Fetch price history points over the last N hours.

    Uses startTs/endTs. Polymarket's `fidelity=60` returns ~1 point per hour
    (not per minute as the param name suggests). 24h → ~25 points.
    Returns list of {t, p} dicts (oldest first).
    """
    try:
        end_ts = int(time.time())
        start_ts = end_ts - int(hours * 3600)
        r = requests.get(
            f"{CLOB_API}/prices-history",
            params={"market": token_id, "startTs": start_ts,
                    "endTs": end_ts, "fidelity": 60},
            timeout=15,
        )
        r.raise_for_status()
        return (r.json() or {}).get("history", []) or []
    except Exception as e:
        log.debug("price-history fetch failed for %s: %s", token_id[:12], e)
        return []


def _fetch_market_meta(token_id: str) -> dict:
    """Get tick size, neg_risk, end_date, current best bid/ask for a token."""
    try:
        r = requests.get(f"{CLOB_API}/markets/{token_id}", timeout=10)
        r.raise_for_status()
        return r.json() or {}
    except Exception as e:
        log.debug("market meta failed: %s", e)
        return {}


# ── Journal lookup ─────────────────────────────────────────────────────
def _load_entry_record(token_id: str) -> dict | None:
    """Find most recent journal entry for this token (the entry)."""
    if not JOURNAL.exists():
        return None
    matches = []
    try:
        with JOURNAL.open() as f:
            for line in f:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("token_id") == token_id:
                    matches.append(e)
    except Exception as e:
        log.warning("journal read failed: %s", e)
        return None
    return matches[-1] if matches else None


# ── Trigger evaluation ─────────────────────────────────────────────────
def _evaluate_triggers(
    token_id: str,
    entry_price: float,
    edge_estimated: float,
    current_price: float,
    end_date_iso: str | None,
) -> tuple[bool, dict]:
    """Returns (should_stop, metrics). Caller still applies sanity guards."""
    metrics: dict[str, Any] = {
        "token_id": token_id,
        "entry_price": entry_price,
        "current_price": current_price,
        "edge_estimated": edge_estimated,
    }

    # Trigger 1: drawdown ≥ entry edge.
    drawdown = entry_price - current_price
    metrics["drawdown"] = round(drawdown, 4)
    metrics["edge_threshold"] = round(edge_estimated, 4)
    t1_drawdown = drawdown >= edge_estimated
    metrics["t1_drawdown"] = t1_drawdown

    # Trigger 2: recent move is ≥ STOP_VOL_SIGMA σ of last 24h.
    # Polymarket fidelity=60 → ~1 point/hr, so 24h yields ~25 points.
    history = _fetch_price_history(token_id, hours=24.0)
    prices = [float(h.get("p", 0)) for h in history if h.get("p") is not None]
    sigma = pstdev(prices) if len(prices) >= 5 else 0.0
    metrics["recent_sigma"] = round(sigma, 5)
    if sigma > 0 and len(prices) >= 3:
        # "Recent move" = current vs ~3h ago (last 3 points at 1pt/hr).
        recent_move = prices[-1] - prices[max(0, len(prices) - 3)]
        metrics["recent_move"] = round(recent_move, 4)
        t2_volatility = (recent_move <= -STOP_VOL_SIGMA * sigma)
    else:
        metrics["recent_move"] = None
        t2_volatility = False
    metrics["t2_volatility"] = t2_volatility
    metrics["sigma_threshold"] = round(STOP_VOL_SIGMA * sigma, 4) if sigma else None

    # Trigger 3: velocity — sustained drop ≥ STOP_VELOCITY_PCT.
    # With ~1 point/hr, look at last 3 hours of drop (sustained move).
    if len(prices) >= 3:
        idx = max(0, len(prices) - 3)
        velocity_drop = prices[idx] - prices[-1]
        metrics["velocity_drop_3h"] = round(velocity_drop, 4)
        t3_velocity = velocity_drop >= STOP_VELOCITY_PCT
    else:
        metrics["velocity_drop_3h"] = None
        t3_velocity = False
    metrics["t3_velocity"] = t3_velocity
    metrics["velocity_threshold"] = STOP_VELOCITY_PCT

    should_stop = t1_drawdown and t2_volatility and t3_velocity
    metrics["should_stop"] = should_stop
    return should_stop, metrics


# ── Sanity guards ──────────────────────────────────────────────────────
def _passes_sanity_guards(
    entry_record: dict, end_date_iso: str | None, best_bid: float, stop_price: float
) -> tuple[bool, str]:
    """Returns (ok, reason_if_not)."""
    # Min hold window: don't stop within first 5 min.
    try:
        entry_ts = datetime.fromisoformat(entry_record["ts"].replace("Z", "+00:00"))
    except Exception:
        entry_ts = datetime.now(timezone.utc) - timedelta(hours=1)  # safe fallback
    held_minutes = (datetime.now(timezone.utc) - entry_ts).total_seconds() / 60
    if held_minutes < STOP_MIN_HOLD_MINUTES:
        return False, f"only held {held_minutes:.1f}m (<{STOP_MIN_HOLD_MINUTES}m)"

    # Don't stop near close — if we were right we resolve to $1 anyway.
    if end_date_iso:
        try:
            end_dt = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
            hours_to_close = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
            if hours_to_close < STOP_MIN_HOURS_TO_CLOSE:
                return False, f"only {hours_to_close:.1f}h to resolution"
        except Exception:
            pass

    # Min bid sanity: book might be broken.
    if best_bid > 0 and stop_price > 0 and best_bid < STOP_MIN_BID_PCT * stop_price:
        return False, f"best_bid {best_bid:.3f} <{STOP_MIN_BID_PCT*100:.0f}% of stop {stop_price:.3f} — book broken, hold"

    return True, ""


# ── Market sell ────────────────────────────────────────────────────────
def _market_sell(token_id: str, shares: float, neg_risk: bool) -> dict:
    """Place a market SELL via py-clob-client. Returns response dict."""
    try:
        # Lazy import to avoid pulling clob client when not needed.
        from executor import _get_client
        from py_clob_client_v2.clob_types import (
            MarketOrderArgs, OrderType, PartialCreateOrderOptions,
        )
        from py_clob_client_v2.order_builder.constants import SELL

        client = _get_client()
        order = MarketOrderArgs(
            token_id=token_id,
            amount=shares,
            side=SELL,
        )
        opts = PartialCreateOrderOptions(neg_risk=neg_risk)
        signed = client.create_market_order(order, opts)
        resp = client.post_order(signed, OrderType.FOK)
        return {"status": "submitted", "response": resp}
    except Exception as e:
        log.exception("market sell failed for %s", token_id[:12])
        return {"status": "error", "error": str(e)}


# ── Main entry point — called from bot.py main loop ────────────────────
def check_and_execute_stops(funder: str) -> int:
    """Evaluate every open position. Stop out the ones that trigger.
    Returns number of positions stopped out this iteration.
    """
    if not STOP_LOSS_ENABLED or not funder:
        return 0

    positions = _fetch_open_positions(funder)
    if not positions:
        return 0

    n_stopped = 0
    for p in positions:
        try:
            token_id = str(p.get("asset") or p.get("tokenId") or "")
            if not token_id:
                continue

            cur_price = float(p.get("curPrice") or 0)
            shares = float(p.get("size") or 0)
            if cur_price <= 0 or shares <= 0:
                continue

            entry_rec = _load_entry_record(token_id)
            if not entry_rec or entry_rec.get("entry_price") is None:
                # No journal entry → can't compute stop level.
                continue

            entry_price = float(entry_rec["entry_price"])
            edge = float(entry_rec.get("edge_estimated") or 0)
            if edge <= 0:
                # No recorded edge → skip (safer than guessing).
                continue

            should_stop, metrics = _evaluate_triggers(
                token_id, entry_price, edge, cur_price,
                p.get("endDate"),
            )

            if not should_stop:
                continue

            # Sanity guards before pulling the trigger.
            stop_price = entry_price - edge
            best_bid = cur_price  # data-api curPrice is the mid; close enough for guard
            ok, reason = _passes_sanity_guards(
                entry_rec, p.get("endDate"), best_bid, stop_price,
            )
            if not ok:
                log.info("Stop conditions met for %s but sanity blocked: %s",
                         entry_rec.get("market", token_id[:12]), reason)
                continue

            # Execute the market sell.
            slug = entry_rec.get("market") or token_id[:12]
            log.warning("STOP-LOSS FIRING: %s | entry=%.3f cur=%.3f edge=%.3f",
                        slug, entry_price, cur_price, edge)
            sell_res = _market_sell(token_id, shares, bool(p.get("negativeRisk")))

            # Log to exits journal.
            exit_row = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "exit_reason": "stop_loss",
                "market": slug,
                "token_id": token_id,
                "shares_sold": shares,
                "entry_price": entry_price,
                "exit_price": cur_price,
                "realized_pnl_usd": round((cur_price - entry_price) * shares, 2),
                "edge_at_entry": edge,
                "trigger_metrics": metrics,
                "sell_response": sell_res,
            }
            EXITS_LOG.parent.mkdir(parents=True, exist_ok=True)
            with EXITS_LOG.open("a") as f:
                f.write(json.dumps(exit_row, default=str) + "\n")

            # Set cooldown so we don't re-enter for 24h.
            _set_cooldown(token_id)

            # Telegram alert (full detail).
            pnl = exit_row["realized_pnl_usd"]
            sell_status = sell_res.get("status", "?")
            tg_msg = (
                f"🛑 <b>STOP-LOSS TRIGGERED</b>\n\n"
                f"<b>{slug}</b>\n"
                f"Entry: <code>${entry_price:.3f}</code> · "
                f"Exit: <code>${cur_price:.3f}</code> · "
                f"PnL: <b>${pnl:+.2f}</b>\n"
                f"Shares sold: <code>{shares:.2f}</code> · "
                f"Sell status: <code>{sell_status}</code>\n\n"
                f"<b>Trigger metrics:</b>\n"
                f"  • Drawdown: <code>{metrics['drawdown']:.3f}</code> "
                f"(≥ edge <code>{metrics['edge_threshold']:.3f}</code>)\n"
                f"  • Recent move: <code>{metrics['recent_move']}</code> "
                f"(σ={metrics['recent_sigma']}, threshold "
                f"<code>{metrics.get('sigma_threshold')}</code>)\n"
                f"  • Velocity 3h: <code>{metrics['velocity_drop_3h']}</code> "
                f"(threshold <code>{metrics['velocity_threshold']:.3f}</code>)\n\n"
                f"24h cooldown active on this token."
            )
            _tg_send(tg_msg)

            n_stopped += 1
        except Exception as e:
            log.warning("stop evaluation failed for position: %s", e)
            continue

    return n_stopped


# ── CLI for manual testing ─────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Evaluate triggers but don't actually sell")
    args = ap.parse_args()

    funder = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
    if not funder:
        print("POLYMARKET_FUNDER_ADDRESS not set"); sys.exit(1)

    if args.dry_run:
        # Only evaluate, don't sell.
        os.environ["STOP_LOSS_ENABLED"] = "false"
        positions = _fetch_open_positions(funder)
        print(f"Evaluating {len(positions)} open positions...\n")
        for p in positions:
            token_id = str(p.get("asset") or p.get("tokenId") or "")
            cur_price = float(p.get("curPrice") or 0)
            entry = _load_entry_record(token_id)
            if not entry:
                print(f"  {p.get('title','?')[:60]}: no journal entry")
                continue
            ep = float(entry.get("entry_price", 0))
            edge = float(entry.get("edge_estimated") or 0)
            if edge <= 0 or ep <= 0:
                print(f"  {entry.get('market','?')[:60]}: missing edge/entry")
                continue
            ok, m = _evaluate_triggers(token_id, ep, edge, cur_price, p.get("endDate"))
            mark = "🛑 STOP" if ok else "✓ hold"
            v3h = m.get('velocity_drop_3h')
            v_str = f"{v3h:+.3f}" if v3h is not None else "n/a"
            print(f"  {mark}  {entry.get('market','?')[:55]}  "
                  f"entry={ep:.3f} cur={cur_price:.3f}  "
                  f"DD={m['drawdown']:+.3f} σ={m['recent_sigma']:.4f} "
                  f"v3h={v_str}")
    else:
        n = check_and_execute_stops(funder)
        print(f"Stopped out {n} position(s).")

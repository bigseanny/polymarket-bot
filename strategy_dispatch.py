"""Strategy dispatcher — routes the main loop to the right scanner/executor.

Three modes selected by `STRATEGY_MODE` env var:
  - nearcert  → existing scan() + size_portfolio() + execute() (the original bot)
  - arb       → arb_scanner.scan_arb_baskets() + execute_arb_basket()
  - momentum  → momentum_scanner.scan_momentum_candidates() + execute_momentum()

If STRATEGY_MODE is unset, defaults to 'nearcert' for backward compatibility.

Each mode keeps its own state files (cooldowns, journals) inside the same
logs/ directory but with mode-prefixed names so multiple instances on the
same host could in theory share storage (we don't — each runs in its own
directory).
"""
from __future__ import annotations
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from config import CFG

log = logging.getLogger(__name__)

STRATEGY_MODE = os.environ.get("STRATEGY_MODE", "nearcert").lower().strip()
VALID_MODES = ("nearcert", "arb", "momentum")
if STRATEGY_MODE not in VALID_MODES:
    raise SystemExit(f"Invalid STRATEGY_MODE={STRATEGY_MODE!r}; must be one of {VALID_MODES}")

log.info("Strategy mode: %s", STRATEGY_MODE)


def _live_nav_and_bankroll() -> tuple[float, float]:
    """Returns (live_bankroll_cash, live_nav_cash_plus_positions)."""
    from bankroll import effective_bankroll
    if CFG.DRY_RUN or not CFG.FUNDER_ADDRESS:
        return CFG.BANKROLL_USD, CFG.BANKROLL_USD
    cash = effective_bankroll(CFG.FUNDER_ADDRESS, fallback=CFG.BANKROLL_USD)
    nav = cash
    try:
        import requests
        r = requests.get(
            "https://data-api.polymarket.com/positions",
            params={"user": CFG.FUNDER_ADDRESS.lower(), "limit": 100, "sizeThreshold": 0.01},
            timeout=10,
        )
        r.raise_for_status()
        pos_value = sum(float(p.get("currentValue") or 0) for p in (r.json() or []))
        nav = cash + pos_value
    except Exception as e:
        log.debug("nav fetch failed: %s", e)
    return cash, nav


# ───────────────── nearcert (existing flow) ─────────────────
def run_once_nearcert() -> None:
    """Delegate to the original nearcert pipeline already implemented in bot.py."""
    # We import here to avoid circular imports
    from scanner import scan
    from sizing import size_portfolio
    from executor import execute

    candidates = scan()
    log.info("Top qualifying candidates: %d", len(candidates))
    for c in candidates[:10]:
        log.info(
            "  apr=%+.0f%%  edge=%+.3f  %3s bid=%.3f ask=%.3f  %5.1fd  vol=$%11s  %s",
            c.annualized_return * 100, c.edge, c.outcome, c.best_bid, c.best_ask,
            c.days_to_resolution, f"{c.volume_usd:,.0f}", c.market_slug[:50],
        )
    if not candidates:
        return

    bankroll, nav = _live_nav_and_bankroll()
    sized = size_portfolio(candidates, bankroll=bankroll, nav=nav)
    log.info("Sizing → %d orders, total $%.2f of $%.2f bankroll",
             len(sized), sum(s.usd for s in sized), bankroll)
    if not sized:
        return

    results = execute(sized)
    submitted = [r for r in results if r.get("status") in ("submitted", "simulated")]
    log.info("%s: %d/%d orders",
             "Simulated" if CFG.DRY_RUN else "Submitted",
             len(submitted), len(results))

    audit = Path(CFG.LOG_DIR) / f"orders-{datetime.now(timezone.utc):%Y%m%d}.jsonl"
    with audit.open("a") as f:
        for r in results:
            f.write(json.dumps(r, default=str) + "\n")


# ───────────────── arb ──────────────────────────────────────
def run_once_arb() -> None:
    from scanner import fetch_active_markets
    from arb_scanner import scan_arb_baskets, baskets_to_log_dicts

    markets = fetch_active_markets()
    baskets = scan_arb_baskets(markets)
    log.info("Arb opportunities found: %d", len(baskets))

    if not baskets:
        return

    # Log top opportunities
    for b in baskets[:5]:
        log.info(
            "  arb profit=$%.2f (%.2f%%) cost=$%.2f legs=%d days=%.1f event=%s",
            b.expected_profit_usd, b.profit_pct * 100, b.basket_cost_usd,
            len(b.legs), b.days_to_resolution, b.event_slug[:50],
        )

    # Persist scan output for inspection
    audit = Path(CFG.LOG_DIR) / f"arb-scans-{datetime.now(timezone.utc):%Y%m%d}.jsonl"
    with audit.open("a") as f:
        for entry in baskets_to_log_dicts(baskets):
            f.write(json.dumps(entry, default=str) + "\n")

    bankroll, nav = _live_nav_and_bankroll()

    # Execute baskets in order of expected profit, until we run out of cash
    from executor_arb import execute_arb_basket
    remaining_cash = bankroll
    placed = 0
    for b in baskets:
        if b.basket_cost_usd > remaining_cash:
            log.info("Skipping basket %s: cost $%.2f > remaining cash $%.2f",
                     b.event_slug, b.basket_cost_usd, remaining_cash)
            continue
        result = execute_arb_basket(b)
        if result.get("status") == "submitted":
            remaining_cash -= b.basket_cost_usd
            placed += 1
        elif result.get("status") == "simulated":
            log.info("(simulated) would place basket %s for $%.2f", b.event_slug, b.basket_cost_usd)
            placed += 1
        else:
            log.warning("Basket %s failed: %s", b.event_slug, result.get("error"))

    log.info("Arb placed: %d / %d", placed, len(baskets))


# ───────────────── momentum ─────────────────────────────────
def run_once_momentum() -> None:
    from scanner import fetch_active_markets
    from momentum_scanner import scan_momentum_candidates
    from momentum_executor import execute_momentum_entries, manage_open_positions

    # First: manage existing momentum positions (TP/SL/time-stop exits)
    if not CFG.DRY_RUN and CFG.FUNDER_ADDRESS:
        try:
            n_exits = manage_open_positions(CFG.FUNDER_ADDRESS)
            if n_exits:
                log.warning("Momentum exits this loop: %d", n_exits)
        except Exception as e:
            log.warning("momentum exit manager failed: %s", e)

    bankroll, nav = _live_nav_and_bankroll()
    markets = fetch_active_markets()
    candidates = scan_momentum_candidates(markets, nav_usd=nav)
    log.info("Momentum candidates: %d", len(candidates))

    if not candidates:
        return

    # Log top 5
    for c in candidates[:5]:
        log.info(
            "  momentum: %s @%.3f Δ%+.1f%% volX%.1f  %s",
            c.direction, c.entry_price, c.velocity_pct * 100, c.volume_multiple,
            c.market_slug[:60],
        )

    audit = Path(CFG.LOG_DIR) / f"momentum-scans-{datetime.now(timezone.utc):%Y%m%d}.jsonl"
    with audit.open("a") as f:
        for c in candidates:
            f.write(json.dumps(c.to_log(), default=str) + "\n")

    # Execute top candidates (one per loop to avoid stacking on the same news event)
    top = candidates[:1]
    results = execute_momentum_entries(top)
    log.info("Momentum entries placed: %d", sum(1 for r in results if r.get("status") in ("submitted", "simulated")))


# ───────────────── public entrypoint ────────────────────────
def run_once() -> None:
    """Called once per main loop iteration. Routes by STRATEGY_MODE."""
    if STRATEGY_MODE == "arb":
        run_once_arb()
    elif STRATEGY_MODE == "momentum":
        run_once_momentum()
    else:
        run_once_nearcert()

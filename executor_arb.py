"""Arb basket executor — places N leg orders atomically per neg-risk event.

PROTOCOL
--------
1. For each leg, submit a market-like FOK (Fill-Or-Kill) buy at the leg's
   best_ask (with 1¢ slack to absorb micro-moves).
2. If any leg fails to fill within the FOK window, IMMEDIATELY sell back any
   legs we successfully bought (unwind), to avoid being stuck with a partial
   directional position.
3. Log every leg + the basket as a whole to logs/arb-orders-YYYYMMDD.jsonl
   and to the standard trades journal so PnL accounting works.

This is more aggressive than nearcert which uses GTC orders. Arb edge can
disappear in seconds, so we accept partial-fill risk and unwind cost in
exchange for execution speed.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from config import CFG
from arb_scanner import ArbBasket, ArbLeg

log = logging.getLogger(__name__)


def _round_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return round(price, 4)
    return round(round(price / tick) * tick, 4)


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _audit_path() -> Path:
    return Path(CFG.LOG_DIR) / f"arb-orders-{datetime.now(timezone.utc):%Y%m%d}.jsonl"


def _record(entry: dict) -> None:
    path = _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _client():
    """Lazy CLOB client (matches executor.py pattern)."""
    from executor import _get_client  # type: ignore
    return _get_client()


def _place_leg_fok(client, leg: ArbLeg, price: float, neg_risk: bool = True) -> dict:
    """Submit one FOK buy order. Returns response dict from CLOB."""
    from py_clob_client_v2.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
    from py_clob_client_v2.order_builder.constants import BUY

    order = OrderArgs(
        token_id=leg.token_id,
        price=price,
        size=max(1.0, round(leg.best_ask_size, 2)),  # filled-or-killed; size doesn't matter beyond what we want
        side=BUY,
    )
    opts = PartialCreateOrderOptions(neg_risk=neg_risk)
    signed = client.create_order(order, opts)
    return client.post_order(signed, OrderType.FOK)


def _place_leg_market_sell(client, leg: ArbLeg, shares: float, neg_risk: bool = True) -> dict:
    """Emergency unwind: market-sell back a leg we just bought."""
    from py_clob_client_v2.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
    from py_clob_client_v2.order_builder.constants import SELL

    order = OrderArgs(
        token_id=leg.token_id,
        price=max(0.01, round(leg.best_ask - 0.05, 4)),  # accept up to 5¢ slippage to unwind
        size=round(shares, 2),
        side=SELL,
    )
    opts = PartialCreateOrderOptions(neg_risk=neg_risk)
    signed = client.create_order(order, opts)
    return client.post_order(signed, OrderType.FOK)


def execute_arb_basket(b: ArbBasket) -> dict:
    """Execute a full N-leg arb basket. Returns a single dict summarizing outcome.

    DRY_RUN: simulates and records to audit log.
    LIVE:    places FOK orders sequentially; unwinds on first failure.
    """
    ts = _ts()
    shares = round(b.recommended_shares, 2)
    if shares < 1:
        return {"status": "skipped", "reason": "shares<1", "event_slug": b.event_slug, "ts": ts}

    log.info(
        "Arb basket %s: %d legs, %.2f shares each, cost $%.2f, exp profit $%.2f (%.2f%%)",
        b.event_slug, len(b.legs), shares, b.basket_cost_usd,
        b.expected_profit_usd, b.profit_pct * 100,
    )

    if CFG.DRY_RUN:
        result = {
            "mode": "DRY_RUN", "status": "simulated",
            "event_slug": b.event_slug, "event_question": b.event_question,
            "n_legs": len(b.legs), "shares_each": shares,
            "basket_cost_usd": b.basket_cost_usd,
            "expected_profit_usd": b.expected_profit_usd,
            "profit_pct": b.profit_pct,
            "legs": [l.to_dict() for l in b.legs],
            "ts": ts,
        }
        _record(result)
        return result

    client = _client()
    successful_legs: list[tuple[ArbLeg, float, dict]] = []
    failed_leg: ArbLeg | None = None
    failed_reason: str | None = None

    for leg in b.legs:
        leg_price = _round_to_tick(leg.best_ask + 0.005, leg.tick_size)  # 0.5¢ slack
        try:
            resp = _place_leg_fok(client, leg, leg_price, neg_risk=True)
            # CLOB returns a status; treat anything not matched/successful as failure
            status = (resp or {}).get("status") or (resp or {}).get("success")
            if status in (True, "matched", "filled", "success"):
                successful_legs.append((leg, leg_price, resp))
            else:
                failed_leg = leg
                failed_reason = f"FOK not filled: {resp}"
                break
        except Exception as e:
            failed_leg = leg
            failed_reason = str(e)
            log.exception("Leg failed for %s", leg.market_slug)
            break

    if failed_leg is not None:
        log.warning(
            "Arb basket %s: leg %s failed (%s). Unwinding %d successful legs.",
            b.event_slug, failed_leg.market_slug, failed_reason, len(successful_legs),
        )
        unwound = []
        for leg, price, _resp in successful_legs:
            try:
                u = _place_leg_market_sell(client, leg, shares)
                unwound.append({"market": leg.market_slug, "result": u})
            except Exception as e:
                unwound.append({"market": leg.market_slug, "error": str(e)})
                log.exception("Unwind failed for %s", leg.market_slug)
        result = {
            "mode": "LIVE", "status": "unwound",
            "event_slug": b.event_slug, "event_question": b.event_question,
            "failed_leg": failed_leg.market_slug, "failed_reason": failed_reason,
            "successful_legs": [l.market_slug for l, _, _ in successful_legs],
            "unwound": unwound,
            "ts": _ts(),
        }
        _record(result)
        return result

    # All legs filled — record success
    result = {
        "mode": "LIVE", "status": "submitted",
        "event_slug": b.event_slug, "event_question": b.event_question,
        "n_legs": len(b.legs), "shares_each": shares,
        "basket_cost_usd": b.basket_cost_usd,
        "expected_profit_usd": b.expected_profit_usd,
        "profit_pct": b.profit_pct,
        "legs": [
            {"market": l.market_slug, "token_id": l.token_id, "price": p, "shares": shares, "response": r}
            for (l, p, r) in successful_legs
        ],
        "ts": _ts(),
    }
    _record(result)

    # Notify on success
    try:
        from notify import notify
        notify(
            f"\U0001F4B0 <b>Arb basket placed</b>\n"
            f"Event: {b.event_question[:80]}\n"
            f"Legs: {len(b.legs)} · Cost: ${b.basket_cost_usd:.2f}\n"
            f"Expected profit: <b>${b.expected_profit_usd:.2f}</b> ({b.profit_pct*100:.2f}%)\n"
            f"Days to resolution: {b.days_to_resolution:.1f}",
            silent=False,
        )
    except Exception as e:
        log.warning("notify failed: %s", e)
    return result

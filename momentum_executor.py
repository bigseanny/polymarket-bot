"""Momentum executor — enters on signal, manages TP/SL/time-stop exits.

State
-----
logs/momentum_positions.json   : {token_id: {entry_price, shares, tp, sl, time_stop_at, market_slug, condition_id}}
logs/momentum_cooldown.json    : {token_id: last_exit_iso}  (managed by momentum_scanner.record_exit)
logs/momentum-orders-YYYYMMDD.jsonl : audit log

Exit triggers (any one fires):
  - mid_price >= tp  → take profit
  - mid_price <= sl  → stop loss
  - now >= time_stop_at → time stop
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from config import CFG
from momentum_scanner import MomentumCandidate, record_exit

log = logging.getLogger(__name__)

POSITIONS_FILE = Path(CFG.LOG_DIR) / "momentum_positions.json"


# ── State helpers ────────────────────────────────────────────────────
def _load_positions() -> dict[str, dict]:
    try:
        with POSITIONS_FILE.open() as f:
            return json.load(f)
    except Exception:
        return {}


def _save_positions(data: dict[str, dict]) -> None:
    POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with POSITIONS_FILE.open("w") as f:
        json.dump(data, f, indent=2)


def _audit_path() -> Path:
    return Path(CFG.LOG_DIR) / f"momentum-orders-{datetime.now(timezone.utc):%Y%m%d}.jsonl"


def _record(entry: dict) -> None:
    path = _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _round_to_tick(price: float, tick: float = 0.01) -> float:
    return round(round(price / tick) * tick, 4)


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _best_quote(token_id: str) -> tuple[float, float] | None:
    """Return (best_bid, best_ask) or None."""
    try:
        r = requests.get(f"{CFG.CLOB_API}/book", params={"token_id": token_id}, timeout=6)
        r.raise_for_status()
        book = r.json()
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        best_bid = float(bids[0]["price"]) if bids else 0.0
        best_ask = float(asks[0]["price"]) if asks else 1.0
        return (best_bid, best_ask)
    except Exception:
        return None


# ── Entry ────────────────────────────────────────────────────────────
def execute_momentum_entries(candidates: list[MomentumCandidate]) -> list[dict]:
    """Place GTC buy orders for each momentum candidate. Skip if we already hold."""
    positions = _load_positions()
    results: list[dict] = []

    for c in candidates:
        if c.token_id in positions:
            log.info("Momentum: already in position on %s, skipping", c.market_slug[:50])
            continue

        shares = round(c.recommended_size_usd / c.entry_price, 2)
        if shares < 1:
            continue

        ts = _ts()

        if CFG.DRY_RUN:
            res = {
                "mode": "DRY_RUN", "status": "simulated", "side": "BUY",
                "market": c.market_slug, "outcome": c.outcome, "direction": c.direction,
                "token_id": c.token_id, "price": c.entry_price, "shares": shares,
                "usd": round(shares * c.entry_price, 2),
                "velocity_pct": c.velocity_pct, "volume_multiple": c.volume_multiple,
                "tp": c.take_profit_price, "sl": c.stop_loss_price,
                "time_stop_at": c.time_stop_at, "ts": ts,
            }
        else:
            try:
                from py_clob_client_v2.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
                from py_clob_client_v2.order_builder.constants import BUY
                from executor import _get_client  # type: ignore

                client = _get_client()
                price = _round_to_tick(c.entry_price + 0.005)  # 0.5¢ slack
                order = OrderArgs(token_id=c.token_id, price=price, size=shares, side=BUY)
                # Momentum markets can be neg-risk or binary; pass through
                opts = PartialCreateOrderOptions(neg_risk=False)  # default; safe for binary
                signed = client.create_order(order, opts)
                resp = client.post_order(signed, OrderType.GTC)
                res = {
                    "mode": "LIVE", "status": "submitted", "side": "BUY",
                    "market": c.market_slug, "outcome": c.outcome, "direction": c.direction,
                    "token_id": c.token_id, "price": price, "shares": shares,
                    "usd": round(shares * price, 2),
                    "velocity_pct": c.velocity_pct, "volume_multiple": c.volume_multiple,
                    "tp": c.take_profit_price, "sl": c.stop_loss_price,
                    "time_stop_at": c.time_stop_at,
                    "response": resp, "ts": ts,
                }
                log.info("MOMENTUM BUY: %s @%.3f x%.2f", c.market_slug[:50], price, shares)
                try:
                    from notify import notify
                    notify(
                        f"\U0001F4C8 <b>Momentum entry</b> ({c.direction})\n"
                        f"{c.market_slug[:80]}\n"
                        f"Entry: <b>${price}</b> x {shares} (\\${res['usd']})\n"
                        f"Velocity: {c.velocity_pct*100:+.1f}% · VolX: {c.volume_multiple:.1f}\n"
                        f"TP {c.take_profit_price} · SL {c.stop_loss_price} · TimeStop 12h"
                    )
                except Exception as e:
                    log.warning("notify failed: %s", e)
            except Exception as e:
                log.exception("Momentum entry failed for %s", c.market_slug)
                res = {"mode": "LIVE", "status": "error", "error": str(e),
                       "market": c.market_slug, "ts": ts}

        if res.get("status") in ("submitted", "simulated"):
            positions[c.token_id] = {
                "market_slug": c.market_slug,
                "condition_id": c.condition_id,
                "outcome": c.outcome,
                "direction": c.direction,
                "entry_price": c.entry_price,
                "shares": shares,
                "tp": c.take_profit_price,
                "sl": c.stop_loss_price,
                "time_stop_at": c.time_stop_at,
                "entered_at": ts,
            }

        _record(res)
        results.append(res)

    _save_positions(positions)
    return results


# ── Exit management ──────────────────────────────────────────────────
def manage_open_positions(funder_address: str) -> int:
    """Check each open momentum position for TP/SL/time-stop. Returns # of exits."""
    positions = _load_positions()
    if not positions:
        return 0

    now = datetime.now(timezone.utc)
    exits_count = 0
    keep: dict[str, dict] = {}

    for token_id, pos in positions.items():
        # Time stop?
        try:
            time_stop_at = datetime.fromisoformat(pos["time_stop_at"])
        except Exception:
            time_stop_at = now  # corrupted — exit immediately

        quote = _best_quote(token_id)
        if not quote:
            keep[token_id] = pos
            continue
        best_bid, best_ask = quote
        mid = (best_bid + best_ask) / 2 if best_bid > 0 else best_ask

        reason = None
        if mid >= pos["tp"]:
            reason = f"take_profit (mid={mid:.3f} >= tp={pos['tp']})"
        elif mid <= pos["sl"]:
            reason = f"stop_loss (mid={mid:.3f} <= sl={pos['sl']})"
        elif now >= time_stop_at:
            reason = f"time_stop (held > {(now - datetime.fromisoformat(pos['entered_at'])).total_seconds() / 3600:.1f}h)"

        if not reason:
            keep[token_id] = pos
            continue

        # Exit: market sell at best_bid (or slightly below for FOK speed)
        sell_price = max(0.01, round(best_bid - 0.005, 4))
        shares = pos["shares"]
        pnl = round((sell_price - pos["entry_price"]) * shares, 2)
        ts = _ts()

        if CFG.DRY_RUN:
            res = {
                "mode": "DRY_RUN", "status": "simulated_exit", "side": "SELL",
                "market": pos["market_slug"], "token_id": token_id,
                "exit_price": sell_price, "shares": shares,
                "entry_price": pos["entry_price"], "pnl_usd": pnl,
                "reason": reason, "ts": ts,
            }
        else:
            try:
                from py_clob_client_v2.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
                from py_clob_client_v2.order_builder.constants import SELL
                from executor import _get_client  # type: ignore
                client = _get_client()
                order = OrderArgs(token_id=token_id, price=sell_price, size=shares, side=SELL)
                opts = PartialCreateOrderOptions(neg_risk=False)
                signed = client.create_order(order, opts)
                resp = client.post_order(signed, OrderType.FOK)
                res = {
                    "mode": "LIVE", "status": "exited", "side": "SELL",
                    "market": pos["market_slug"], "token_id": token_id,
                    "exit_price": sell_price, "shares": shares,
                    "entry_price": pos["entry_price"], "pnl_usd": pnl,
                    "reason": reason, "response": resp, "ts": ts,
                }
                log.warning("MOMENTUM EXIT: %s @%.3f x%.2f pnl=$%.2f (%s)",
                            pos["market_slug"][:50], sell_price, shares, pnl, reason)
                try:
                    from notify import notify
                    notify(
                        f"\U0001F4C9 <b>Momentum exit</b>\n"
                        f"{pos['market_slug'][:80]}\n"
                        f"Reason: <b>{reason}</b>\n"
                        f"Entry {pos['entry_price']} → Exit {sell_price} · x{shares}\n"
                        f"PnL: <b>${pnl:+.2f}</b>"
                    )
                except Exception as e:
                    log.warning("notify failed: %s", e)
            except Exception as e:
                log.exception("Momentum exit failed for %s", pos['market_slug'])
                # Keep position; retry next loop
                keep[token_id] = pos
                continue

        _record(res)
        record_exit(token_id)  # set 6h cooldown
        exits_count += 1

    _save_positions(keep)
    return exits_count

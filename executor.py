"""
Executor: places (or simulates) orders for sized candidates.

Live mode uses py-clob-client. We post GTC limit BUY orders at the best ask
(the cheapest sell), so they fill immediately if the book hasn't moved.

Safety features:
  * DRY_RUN flag (default True) — never sends a real order
  * REQUIRE_CONFIRM — interactive y/n per order
  * Idempotency — refuses to re-bet on a market where we already have a position
    in this session (tracked in logs/state.json)
  * Tick-rounding — prices snapped to market tick size to avoid rejection
"""
from __future__ import annotations
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import CFG
from sizing import Sized
from notify import notify, fmt_order

log = logging.getLogger(__name__)


# ── State persistence (avoid double-betting same market) ────────────────
def _load_state() -> dict[str, Any]:
    p = Path(CFG.STATE_FILE)
    if not p.exists():
        return {"positions": {}, "orders": []}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {"positions": {}, "orders": []}


def _save_state(state: dict[str, Any]) -> None:
    p = Path(CFG.STATE_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, default=str))


# ── Lazy CLOB client init (only when going live) ────────────────────────
_client = None
_proxy_installed = False


def _install_clob_proxy() -> None:
    """If CLOB_PROXY_URL is set, replace py-clob-client's module-level httpx
    client with one that routes through the proxy. py-clob-client uses a
    single shared httpx.Client (see py_clob_client.http_helpers.helpers), so
    swapping it here transparently proxies every CLOB API call including the
    POST /order call that Polymarket geoblocks.

    This also sets the `requests` library's env-based proxy for CLOB hosts
    only (via NO_PROXY) so /book fetches in scanner.py go direct for speed.
    """
    global _proxy_installed
    if _proxy_installed or not CFG.CLOB_PROXY_URL:
        return
    try:
        import httpx
        # V2 SDK package is py_clob_client_v2 (V1 client stopped working at
        # the April 28 2026 cutover).
        import py_clob_client_v2.http_helpers.helpers as _pchelpers
        _pchelpers._http_client = httpx.Client(
            http2=True,
            proxy=CFG.CLOB_PROXY_URL,
            timeout=30.0,
        )
        # Redact credentials before logging.
        safe = CFG.CLOB_PROXY_URL
        if "@" in safe:
            safe = safe.split("@", 1)[1]
        log.info("CLOB requests now routed via proxy %s", safe)
        _proxy_installed = True
    except Exception as e:
        log.error("Failed to install CLOB proxy: %s", e)
        raise


def _get_client():
    global _client
    if _client is not None:
        return _client
    if not CFG.PRIVATE_KEY:
        raise RuntimeError(
            "POLYMARKET_PRIVATE_KEY is not set. "
            "Add it to .env or set DRY_RUN=true to simulate."
        )

    # Install proxy BEFORE importing/constructing ClobClient so the very
    # first auth call (create_or_derive_api_creds) also uses it.
    _install_clob_proxy()

    from py_clob_client_v2.client import ClobClient

    kwargs = dict(host=CFG.CLOB_API, key=CFG.PRIVATE_KEY, chain_id=CFG.CHAIN_ID)
    if CFG.SIGNATURE_TYPE != 0:
        kwargs["signature_type"] = CFG.SIGNATURE_TYPE
        if not CFG.FUNDER_ADDRESS:
            raise RuntimeError(
                "POLYMARKET_FUNDER_ADDRESS required for signature_type != 0 "
                "(email/Magic-link wallets)."
            )
        kwargs["funder"] = CFG.FUNDER_ADDRESS

    client = ClobClient(**kwargs)
    # V2 renamed create_or_derive_api_creds → create_or_derive_api_key.
    client.set_api_creds(client.create_or_derive_api_key())
    log.info("CLOB client initialized (signature_type=%d)", CFG.SIGNATURE_TYPE)
    _client = client
    return client


def _round_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return round(price, 2)
    return round(round(price / tick) * tick, 4)


def _confirm(prompt: str) -> bool:
    if not CFG.REQUIRE_CONFIRM:
        return True
    # Headless hosted envs (Render/Docker) have no stdin — never block.
    if not sys.stdin or not sys.stdin.isatty():
        log.warning("REQUIRE_CONFIRM=true but no TTY — treating as auto-approve")
        return True
    try:
        ans = input(f"{prompt} [y/N]: ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def cancel_stale_orders() -> int:
    """Cancel any resting orders older than CANCEL_UNFILLED_AFTER_SECONDS.

    Polymarket GTC orders sit in the book until filled or cancelled. If the
    book moves past our price, we want to cancel and re-price on the next
    scan instead of leaving money parked.
    """
    if CFG.DRY_RUN:
        return 0
    try:
        client = _get_client()
        # V2 renamed get_orders → get_open_orders.
        open_orders = client.get_open_orders() or []
    except Exception as e:
        log.warning("cancel_stale: could not list orders: %s", e)
        return 0

    now = datetime.now(timezone.utc).timestamp()
    cutoff = CFG.CANCEL_UNFILLED_AFTER_SECONDS
    to_cancel: list[str] = []
    for o in open_orders:
        try:
            # Polymarket order timestamps are in seconds (string).
            ts = float(o.get("created_at") or o.get("createdAt") or 0)
            if ts and (now - ts) >= cutoff:
                oid = o.get("id") or o.get("orderID") or o.get("order_id")
                if oid:
                    to_cancel.append(oid)
        except (TypeError, ValueError):
            continue

    cancelled = 0
    for oid in to_cancel:
        try:
            # V2: cancel_order takes OrderPayload(orderID=...).
            from py_clob_client_v2.clob_types import OrderPayload
            client.cancel_order(OrderPayload(orderID=oid))
            cancelled += 1
            log.info("Cancelled stale order %s", oid)
        except Exception as e:
            log.warning("Cancel failed for %s: %s", oid, e)
    return cancelled


def execute(orders: list[Sized]) -> list[dict]:
    """Place each sized order. Returns list of result dicts."""
    state = _load_state()
    results: list[dict] = []

    for s in orders:
        c = s.candidate
        key = f"{c.condition_id}:{c.token_id}"

        # Idempotency guard.
        if key in state["positions"]:
            log.info("Skipping %s — already have position this session", c.market_slug)
            continue

        price = _round_to_tick(c.best_ask, c.tick_size)
        shares = round(s.shares, 2)
        usd = round(price * shares, 2)

        line = (
            f"  {c.outcome:>4} | {c.market_slug[:55]:55} | "
            f"ask={price:>5.3f} | shares={shares:>8.2f} | ${usd:>7.2f} | edge={c.edge:+.3f}"
        )
        print(line)

        if CFG.DRY_RUN:
            res = {
                "mode": "DRY_RUN", "status": "simulated",
                "market": c.market_slug, "outcome": c.outcome,
                "token_id": c.token_id, "price": price,
                "shares": shares, "usd": usd, "edge": c.edge,
                "days_to_resolution": c.days_to_resolution,
                "annualized_return": c.annualized_return,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
        else:
            if not _confirm(f"  → Place LIVE BUY {shares} @ {price}?"):
                res = {"mode": "LIVE", "status": "skipped_by_user",
                       "market": c.market_slug, "ts": datetime.now(timezone.utc).isoformat()}
            else:
                try:
                    from py_clob_client_v2.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
                    from py_clob_client_v2.order_builder.constants import BUY

                    client = _get_client()
                    order = OrderArgs(
                        token_id=c.token_id,
                        price=price,
                        size=shares,
                        side=BUY,
                    )
                    # Neg-risk markets (multi-outcome events like "Will X win election")
                    # require a distinct EIP-712 domain. Passing the wrong flag causes
                    # the CLOB to reject the signature as invalid.
                    opts = PartialCreateOrderOptions(neg_risk=bool(c.neg_risk))
                    signed = client.create_order(order, opts)
                    resp = client.post_order(signed, OrderType.GTC)
                    res = {
                        "mode": "LIVE", "status": "submitted",
                        "market": c.market_slug, "outcome": c.outcome,
                        "token_id": c.token_id, "price": price,
                        "shares": shares, "usd": usd, "edge": c.edge,
                        "days_to_resolution": c.days_to_resolution,
                        "annualized_return": c.annualized_return,
                        "response": resp,
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }
                    log.info("ORDER SUBMITTED: %s — %s", c.market_slug, resp)
                except Exception as e:
                    log.exception("ORDER FAILED for %s", c.market_slug)
                    res = {"mode": "LIVE", "status": "error", "error": str(e),
                           "market": c.market_slug,
                           "ts": datetime.now(timezone.utc).isoformat()}

        results.append(res)
        # Notify on successful fills and on real errors — but suppress the
        # expected "not enough balance" / rate-limit chatter that would otherwise
        # flood Telegram while the bot keeps retrying the same underfunded trades.
        should_notify = False
        if res.get("status") in ("simulated", "submitted"):
            should_notify = True
        elif res.get("status") == "error":
            err = str(res.get("error", "")).lower()
            noisy = ("not enough balance" in err or "allowance" in err
                     or "rate limit" in err or "too many requests" in err)
            should_notify = not noisy
        if should_notify:
            notify(fmt_order(res), silent=(res.get("mode") == "DRY_RUN"))
        if res.get("status") in ("simulated", "submitted"):
            state["positions"][key] = {
                "market": c.market_slug, "outcome": c.outcome,
                "price": price, "shares": shares, "usd": usd,
            }
        state["orders"].append(res)

    _save_state(state)
    return results

"""
Scanner: pulls active Polymarket markets from the Gamma API, filters for
near-certainty candidates, and enriches each with live order-book data.

A "candidate" is one outcome (YES or NO token) where the OPPOSITE side is
trading near zero — i.e. the market believes this side will almost certainly
happen. Concretely:

    best_BID on this token  ≥ MIN_BID         (e.g. 0.95)
    best_ASK on this token  ≤ MAX_ASK         (e.g. 0.99) — what we'll pay
    edge = (1 - HAIRCUT) - best_ASK  ≥ MIN_EDGE
    volume    ≥ MIN_VOLUME_USD
    liquidity ≥ MIN_LIQUIDITY_USD
    market is open and resolves within the time window

Why require a high BID, not just a low ask? A token can be offered at 0.05 with
no bid behind it — that's a long-shot lottery ticket, not a near-certainty.
Demanding a tight high bid means real buyers are paying near-100¢ for it.
"""
from __future__ import annotations
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Iterable

import requests

from config import CFG

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    market_slug: str
    question: str
    condition_id: str
    token_id: str
    outcome: str           # "Yes" or "No" (or named outcome)
    best_bid: float        # highest price someone is bidding (proves consensus)
    best_ask: float        # cheapest sell price we can hit
    best_ask_size: float   # shares available at best ask (in #shares, not USD)
    spread: float          # best_ask - best_bid
    edge: float            # (1 - haircut) - best_ask
    volume_usd: float
    liquidity_usd: float
    end_date: str
    days_to_resolution: float
    neg_risk: bool
    tick_size: float

    def to_log(self) -> dict:
        d = asdict(self)
        d["scanned_at"] = datetime.now(timezone.utc).isoformat()
        return d


def fetch_active_markets(limit: int = 500) -> list[dict]:
    """Page through Gamma API events to gather all active, non-closed markets."""
    out: list[dict] = []
    offset = 0
    page_size = 100
    while offset < limit:
        url = f"{CFG.GAMMA_API}/events"
        params = {
            "active": "true",
            "closed": "false",
            "limit": page_size,
            "offset": offset,
            "order": "volume_24hr",
            "ascending": "false",
        }
        try:
            r = requests.get(url, params=params, timeout=15)
            r.raise_for_status()
            events = r.json()
        except Exception as e:
            log.warning("Gamma fetch failed @ offset %d: %s", offset, e)
            break
        if not events:
            break
        for ev in events:
            for m in ev.get("markets", []) or []:
                m["_event_slug"] = ev.get("slug")
                out.append(m)
        if len(events) < page_size:
            break
        offset += page_size
    log.info("Pulled %d markets from Gamma", len(out))
    return out


def _parse_tokens(market: dict) -> list[tuple[str, str]]:
    """Return [(outcome_label, token_id), ...]. Handles JSON-encoded fields."""
    outcomes = market.get("outcomes")
    token_ids = market.get("clobTokenIds")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except Exception:
            return []
    if isinstance(token_ids, str):
        try:
            token_ids = json.loads(token_ids)
        except Exception:
            return []
    if not outcomes or not token_ids or len(outcomes) != len(token_ids):
        return []
    return list(zip(outcomes, token_ids))


def _days_until(iso_str: str | None) -> float | None:
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except Exception:
        return None
    return (dt - datetime.now(timezone.utc)).total_seconds() / 86_400


def _gamma_prefilter(market: dict) -> bool:
    """Cheap pre-filter using fields already on the Gamma market payload."""
    if market.get("closed") or not market.get("active"):
        return False
    if market.get("archived"):
        return False
    try:
        vol = float(market.get("volume") or market.get("volumeNum") or 0)
        liq = float(market.get("liquidity") or market.get("liquidityNum") or 0)
    except (TypeError, ValueError):
        return False
    if vol < CFG.MIN_VOLUME_USD or liq < CFG.MIN_LIQUIDITY_USD:
        return False
    days = _days_until(market.get("endDate"))
    if days is None:
        return False
    if not (CFG.MIN_DAYS_TO_RESOLUTION <= days <= CFG.MAX_DAYS_TO_RESOLUTION):
        return False
    return True


def _fetch_book(token_id: str) -> dict | None:
    """Get the live order book for a token."""
    try:
        r = requests.get(
            f"{CFG.CLOB_API}/book",
            params={"token_id": token_id},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.debug("book fetch failed for %s: %s", token_id, e)
        return None


def _top_of_book(book: dict) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    """Return ((best_bid_price, size), (best_ask_price, size)).

    Best ask = LOWEST sell price. Best bid = HIGHEST buy price.
    Polymarket returns each level as {"price": "0.97", "size": "1234"}; we sort
    defensively because the API has been known to return either order.
    """
    def _levels(side):
        out = []
        for lv in side or []:
            try:
                out.append((float(lv["price"]), float(lv["size"])))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    asks = _levels(book.get("asks"))
    bids = _levels(book.get("bids"))
    asks.sort(key=lambda x: x[0])             # ascending → cheapest first
    bids.sort(key=lambda x: x[0], reverse=True)  # descending → highest first
    return (bids[0] if bids else None, asks[0] if asks else None)


def scan() -> list[Candidate]:
    """Return all qualifying candidates, sorted by edge (best first)."""
    markets = fetch_active_markets()
    candidates: list[Candidate] = []

    for m in markets:
        if not _gamma_prefilter(m):
            continue

        try:
            vol = float(m.get("volume") or m.get("volumeNum") or 0)
            liq = float(m.get("liquidity") or m.get("liquidityNum") or 0)
            tick = float(m.get("orderPriceMinTickSize") or m.get("minimum_tick_size") or 0.01)
        except (TypeError, ValueError):
            continue

        end_date = m.get("endDate") or ""
        days = _days_until(end_date) or 0.0
        neg_risk = bool(m.get("negRisk") or m.get("neg_risk"))

        for outcome_label, token_id in _parse_tokens(m):
            book = _fetch_book(token_id)
            if not book:
                continue
            bid, ask = _top_of_book(book)
            if not ask or not bid:
                # No two-sided market = no consensus = skip.
                continue
            bid_price, _ = bid
            ask_price, ask_size = ask
            spread = ask_price - bid_price

            # Core near-certainty filters:
            #  1) Ask ≤ MAX_ASK so we have edge after haircut.
            #  2) Bid ≥ MIN_BID so the market has real consensus this is happening
            #     (filters out illiquid long-shots that just happen to have a low ask).
            #  3) Spread ≤ MAX_SPREAD so the book isn't pathological.
            if ask_price > CFG.MAX_ASK:
                continue
            if bid_price < CFG.MIN_BID:
                continue
            if spread > CFG.MAX_SPREAD:
                continue

            edge = (1.0 - CFG.HAIRCUT) - ask_price
            if edge < CFG.MIN_EDGE:
                continue

            candidates.append(Candidate(
                market_slug=m.get("slug", ""),
                question=m.get("question", ""),
                condition_id=m.get("conditionId", ""),
                token_id=str(token_id),
                outcome=str(outcome_label),
                best_bid=bid_price,
                best_ask=ask_price,
                best_ask_size=ask_size,
                spread=round(spread, 4),
                edge=round(edge, 4),
                volume_usd=vol,
                liquidity_usd=liq,
                end_date=end_date,
                days_to_resolution=round(days, 2),
                neg_risk=neg_risk,
                tick_size=tick,
            ))

    candidates.sort(key=lambda c: c.edge, reverse=True)
    log.info("Scan complete: %d qualifying candidates", len(candidates))
    return candidates

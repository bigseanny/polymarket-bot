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
    annualized_return: float  # gross annualized % if we win
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


def fetch_active_markets(max_events: int = 5000) -> list[dict]:
    """Page through Gamma API events to gather all active, non-closed markets.

    We query the `/events` endpoint sorted by endDate ascending so we always
    get the soonest-resolving events first — the ones that matter for a
    short-timeframe strategy. We also deduplicate by market id because Gamma
    sometimes returns the same market under multiple events.
    """
    out: list[dict] = []
    seen_ids: set[str] = set()
    offset = 0
    page_size = 100
    max_days = CFG.MAX_DAYS_TO_RESOLUTION

    while offset < max_events:
        url = f"{CFG.GAMMA_API}/events"
        params = {
            "active": "true",
            "closed": "false",
            "archived": "false",
            "limit": page_size,
            "offset": offset,
            "order": "endDate",
            "ascending": "true",
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

        # Early exit: once event endDates pass our MAX_DAYS window we can stop.
        # Use the minimum endDate of this page to decide; safe for short strats.
        stop_after_page = False
        for ev in events:
            ev_days = _days_until(ev.get("endDate"))
            for m in ev.get("markets", []) or []:
                mid = str(m.get("id") or m.get("conditionId") or m.get("slug"))
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)
                m["_event_slug"] = ev.get("slug")
                out.append(m)

            # Only stop paging when event endDates clearly exceed our window.
            # Guarantee we page at least far enough to cover the window —
            # Polymarket has 1000+ events resolving within a 14-day horizon.
            if ev_days is not None and ev_days > max_days + 7:
                stop_after_page = True

        if len(events) < page_size or stop_after_page:
            break
        offset += page_size

    # Merge in watchlist slugs — markets/events Polymarket hides from bulk
    # listings but we can fetch directly.
    _merge_watchlist(out, seen_ids)

    log.info("Pulled %d unique markets from Gamma (incl. %d watchlist)",
             len(out), _watchlist_count(seen_ids))
    return out


_WL_ADDED: int = 0


def _watchlist_count(_seen) -> int:
    return _WL_ADDED


def _merge_watchlist(out: list[dict], seen_ids: set[str]) -> None:
    """Fetch each watchlist slug directly (as event OR market) and append any
    missing markets into `out`."""
    global _WL_ADDED
    _WL_ADDED = 0
    raw = (CFG.WATCHLIST_SLUGS or "").strip()
    if not raw:
        return
    slugs = [s.strip() for s in raw.split(",") if s.strip()]
    for slug in slugs:
        # Try event first (multi-market), then market (single).
        try:
            r = requests.get(f"{CFG.GAMMA_API}/events", params={"slug": slug}, timeout=10)
            r.raise_for_status()
            events = r.json() or []
            if events:
                for m in events[0].get("markets", []) or []:
                    mid = str(m.get("id") or m.get("conditionId") or m.get("slug"))
                    if mid and mid not in seen_ids:
                        m["_event_slug"] = events[0].get("slug")
                        out.append(m)
                        seen_ids.add(mid)
                        _WL_ADDED += 1
                continue
        except Exception as e:
            log.debug("watchlist event fetch failed for %s: %s", slug, e)
        try:
            r = requests.get(f"{CFG.GAMMA_API}/markets", params={"slug": slug}, timeout=10)
            r.raise_for_status()
            markets = r.json() or []
            for m in markets:
                mid = str(m.get("id") or m.get("conditionId") or m.get("slug"))
                if mid and mid not in seen_ids:
                    out.append(m)
                    seen_ids.add(mid)
                    _WL_ADDED += 1
        except Exception as e:
            log.debug("watchlist market fetch failed for %s: %s", slug, e)


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


_SPORTS_TAG_HINTS = (
    "sports", "nba", "nfl", "nhl", "mlb", "ncaa", "soccer", "football",
    "basketball", "baseball", "hockey", "tennis", "ufc", "mma", "boxing",
    "golf", "cricket", "formula", "f1", "esports", "games",
)


def _is_sports_market(market: dict) -> bool:
    """Detect a sports/game market. Polymarket tags sports events and also
    populates `gameStartTime` / `clearBookOnStart` on per-game markets, so we
    use any of those signals as evidence."""
    if market.get("gameStartTime") or market.get("clearBookOnStart"):
        return True
    # Tags can live on the market or the parent event; check both.
    tag_sources = []
    tag_sources.extend(market.get("tags") or [])
    for ev in market.get("events") or []:
        tag_sources.extend(ev.get("tags") or [])
    for t in tag_sources:
        label = (t.get("slug") or t.get("label") or "") if isinstance(t, dict) else str(t)
        label = label.lower()
        if any(hint in label for hint in _SPORTS_TAG_HINTS):
            return True
    return False


def _parse_dt(iso_str: str | None) -> datetime | None:
    if not iso_str:
        return None
    # Polymarket returns gameStartTime as "2025-11-24 05:00:00+00" (space, no Z)
    # and endDate as "2025-11-24T05:00:00Z". Handle both.
    s = str(iso_str).strip().replace(" ", "T").replace("Z", "+00:00")
    # "+00" → "+00:00" for fromisoformat
    if s.endswith("+00"):
        s += ":00"
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _sports_game_started(market: dict) -> bool:
    """True iff we have a game-start timestamp and it's in the past."""
    start = _parse_dt(market.get("gameStartTime"))
    if start is None:
        # Fall back to the market's startDate only if clearBookOnStart is set
        # (indicates per-game market) — otherwise startDate is just listing
        # time and not meaningful for this check.
        if market.get("clearBookOnStart"):
            start = _parse_dt(market.get("startDate"))
    if start is None:
        # No reliable start timestamp. Be conservative: treat as NOT started.
        return False
    return start <= datetime.now(timezone.utc)


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
    # Sports markets: only bet once the game is live. Pre-game favorites can
    # get torched by late scratches, weather, and lineup changes.
    if CFG.SPORTS_REQUIRE_GAME_STARTED and _is_sports_market(market):
        if not _sports_game_started(market):
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

            # Time-scaled edge floor: longer-dated bets must earn more to
            # match the same annualized return as short-dated ones.
            required_edge = CFG.MIN_EDGE
            if CFG.TARGET_APR > 0 and days > 0:
                apr_required = ask_price * CFG.TARGET_APR * (days / 365.0)
                required_edge = max(required_edge, apr_required)

            if edge < required_edge:
                continue

            # Compute gross annualized return if the bet wins.
            # Profit per $1 staked = (1-ask)/ask; extrapolate to 365 days.
            gross_per_dollar = (1.0 - ask_price) / ask_price if ask_price > 0 else 0.0
            annualized = gross_per_dollar * (365.0 / max(days, 0.01))

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
                annualized_return=round(annualized, 3),
                volume_usd=vol,
                liquidity_usd=liq,
                end_date=end_date,
                days_to_resolution=round(days, 2),
                neg_risk=neg_risk,
                tick_size=tick,
            ))

    # Rank by annualized return rather than raw edge — a 4¢ edge resolving in
    # 3 days beats a 4¢ edge resolving in 14 days.
    candidates.sort(key=lambda c: c.annualized_return, reverse=True)
    log.info("Scan complete: %d qualifying candidates", len(candidates))
    return candidates

"""Cross-market arbitrage scanner — neg-risk basket detector.

THESIS
------
Polymarket neg-risk events have N mutually exclusive outcomes (e.g. "Who wins
the NBA Finals?" with N=4 teams). At resolution exactly one outcome pays $1
and the rest pay $0. Therefore at any moment:

    sum(best_ask_outcome_i) for i in 1..N  >=  1.0 + Σfees

When the sum of best asks across all outcomes drops *below* this threshold,
we can buy exactly 1 share of each outcome and lock in deterministic profit
at resolution, regardless of which outcome wins.

This scanner finds those baskets. It does not compute Kelly sizing because
the bet is deterministic; instead it sizes to the smallest liquidity leg
and caps by per-position $-limit.

DESIGN NOTES
------------
- Reuses Gamma `/events` data already pulled by scanner.fetch_active_markets()
- An "event" in Polymarket's data model groups outcome markets together. We
  identify neg-risk by checking m["negRisk"] OR ev.get("negRisk") on any market
- For each candidate event, we pull live orderbook (best_ask + size) from CLOB
- Profit/share = 1 - Σ(best_ask)  (assumes flat fee ~0.5% applied at resolution)
- Position size = min(best_ask_size_per_leg) * 1 share each leg
- A basket is "executable" if min size >= MIN_BASKET_SHARES and total cost
  >= MIN_BASKET_USD (gas overhead floor)
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

import requests

from config import CFG

log = logging.getLogger(__name__)

# Per-strategy config (env-overridable so each deployment can tune)
MIN_ARB_PROFIT_PCT = float(os.environ.get("MIN_ARB_PROFIT_PCT", "0.02"))   # 2% per basket after fees
ARB_FEE_HAIRCUT    = float(os.environ.get("ARB_FEE_HAIRCUT", "0.005"))    # 0.5% safety buffer
MIN_BASKET_USD     = float(os.environ.get("MIN_BASKET_USD", "20"))         # smallest economic basket
MAX_BASKET_USD     = float(os.environ.get("MAX_BASKET_USD", "100"))        # per-basket cap (15% of $500 = $75 ideal)
MIN_BASKET_SHARES  = float(os.environ.get("MIN_BASKET_SHARES", "5"))       # ≥5 shares of each leg
MIN_EVENT_VOL_USD  = float(os.environ.get("MIN_EVENT_VOL_USD", "10000"))   # parent event must have $10k+ volume
MAX_LEGS           = int(os.environ.get("MAX_LEGS", "12"))                  # gas blows up beyond this
ORDERBOOK_CACHE_S  = 8.0  # short cache so multi-leg quote is consistent


@dataclass
class ArbLeg:
    """One leg of an arb basket — one outcome of a neg-risk event."""
    market_slug: str
    token_id: str
    outcome: str
    best_ask: float
    best_ask_size: float        # shares
    condition_id: str
    tick_size: float = 0.01

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ArbBasket:
    """A complete buy-all-outcomes basket on a single neg-risk event."""
    event_slug: str
    event_question: str
    event_id: str
    legs: list[ArbLeg] = field(default_factory=list)
    sum_asks: float = 0.0
    profit_per_share: float = 0.0     # 1.0 - sum_asks - fee_haircut
    profit_pct: float = 0.0           # profit_per_share / sum_asks
    max_basket_shares: float = 0.0    # limited by smallest leg
    recommended_shares: float = 0.0   # capped by MAX_BASKET_USD
    basket_cost_usd: float = 0.0
    expected_profit_usd: float = 0.0
    days_to_resolution: float = 0.0
    total_volume_usd: float = 0.0

    def to_log(self) -> dict:
        d = asdict(self)
        d["scanned_at"] = datetime.now(timezone.utc).isoformat()
        return d


# ── Orderbook cache ─────────────────────────────────────────────────
_ORDERBOOK_CACHE: dict[str, tuple[float, dict]] = {}


def _get_orderbook(token_id: str) -> dict | None:
    """Fetch CLOB book for a token. Cached briefly so basket legs are coherent."""
    now = time.time()
    if token_id in _ORDERBOOK_CACHE:
        ts, book = _ORDERBOOK_CACHE[token_id]
        if now - ts < ORDERBOOK_CACHE_S:
            return book
    try:
        r = requests.get(f"{CFG.CLOB_API}/book", params={"token_id": token_id}, timeout=6)
        r.raise_for_status()
        book = r.json()
        _ORDERBOOK_CACHE[token_id] = (now, book)
        return book
    except Exception as e:
        log.debug("book fetch failed for token %s: %s", token_id[:10], e)
        return None


def _best_ask(book: dict) -> tuple[float, float] | None:
    """Return (best_ask_price, best_ask_size_shares) or None if no asks."""
    asks = book.get("asks") or []
    if not asks:
        return None
    # CLOB returns asks sorted ascending; take cheapest
    try:
        price = float(asks[0].get("price"))
        size = float(asks[0].get("size"))
        return (price, size)
    except (KeyError, ValueError, TypeError):
        return None


def _days_until(end_iso: str | None) -> float:
    if not end_iso:
        return 999.0
    try:
        end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        return max(0.0, (end - datetime.now(timezone.utc)).total_seconds() / 86400)
    except Exception:
        return 999.0


# ── Event grouping ──────────────────────────────────────────────────
def _group_markets_by_event(markets: list[dict]) -> dict[str, list[dict]]:
    """Re-group flat market list back into event clusters by event_slug.

    Note: scanner.fetch_active_markets() stamps each market with `_event_slug`
    of the parent event. We reuse that here.
    """
    grouped: dict[str, list[dict]] = {}
    for m in markets:
        slug = m.get("_event_slug")
        if not slug:
            continue
        grouped.setdefault(slug, []).append(m)
    return grouped


def _is_neg_risk_event(markets: list[dict]) -> bool:
    """An event is neg-risk if ≥2 markets and any market has negRisk=True."""
    if len(markets) < 2:
        return False
    return any(m.get("negRisk") or m.get("neg_risk") for m in markets)


def _winning_token(market: dict) -> tuple[str, str] | None:
    """For a neg-risk market, return (token_id, outcome_label) for the Yes leg.

    In neg-risk events, only the YES outcome of each market matters — we buy
    Yes on every outcome to cover the event.
    """
    tokens = market.get("clobTokenIds") or market.get("tokens") or []
    outcomes = market.get("outcomes") or ["Yes", "No"]
    if isinstance(tokens, str):
        # Sometimes Gamma serializes as JSON string
        import json as _json
        try:
            tokens = _json.loads(tokens)
        except Exception:
            return None
    if not tokens or len(tokens) < 1:
        return None
    if isinstance(outcomes, str):
        import json as _json
        try:
            outcomes = _json.loads(outcomes)
        except Exception:
            outcomes = ["Yes", "No"]
    # Yes token is index 0 by Polymarket convention
    yes_token = str(tokens[0])
    yes_outcome = outcomes[0] if outcomes else "Yes"
    return (yes_token, str(yes_outcome))


# ── Main scanner ────────────────────────────────────────────────────
def scan_arb_baskets(markets: list[dict]) -> list[ArbBasket]:
    """Return all neg-risk events with positive-edge arb baskets.

    Caller passes the same `markets` list scanner.fetch_active_markets() returns.
    """
    grouped = _group_markets_by_event(markets)
    baskets: list[ArbBasket] = []
    examined = 0
    skipped_not_neg = 0
    skipped_too_many_legs = 0
    skipped_no_book = 0
    skipped_negative_edge = 0
    skipped_low_volume_prefilter = 0
    max_events_to_probe = int(os.environ.get("ARB_MAX_EVENTS_PROBED", "200"))

    # Pre-filter: keep only neg-risk events with sufficient volume BEFORE we
    # hit the orderbook API. Each book fetch is ~6s worst-case, so this is
    # the most expensive loop in the bot — cut it down aggressively.
    candidate_events: list[tuple[str, list[dict]]] = []
    for event_slug, mkts in grouped.items():
        if not _is_neg_risk_event(mkts):
            skipped_not_neg += 1
            continue
        if len(mkts) > MAX_LEGS:
            skipped_too_many_legs += 1
            continue
        total_vol = sum(float(m.get("volume24hr") or m.get("volume") or 0) for m in mkts)
        if total_vol < MIN_EVENT_VOL_USD:
            skipped_low_volume_prefilter += 1
            continue
        candidate_events.append((event_slug, mkts))

    log.info(
        "Arb pre-filter: %d events grouped → %d neg-risk + high-volume candidates "
        "(skipped: not-neg=%d, too-many-legs=%d, low-volume=%d)",
        len(grouped), len(candidate_events),
        skipped_not_neg, skipped_too_many_legs, skipped_low_volume_prefilter,
    )

    # Sort by volume desc so we probe the most active baskets first
    candidate_events.sort(
        key=lambda kv: sum(float(m.get("volume24hr") or m.get("volume") or 0) for m in kv[1]),
        reverse=True,
    )
    if len(candidate_events) > max_events_to_probe:
        log.warning(
            "Arb scan capped at top %d events by volume (had %d candidates)",
            max_events_to_probe, len(candidate_events),
        )
        candidate_events = candidate_events[:max_events_to_probe]

    for idx, (event_slug, mkts) in enumerate(candidate_events):
        if idx > 0 and idx % 50 == 0:
            log.info("Arb scan progress: %d/%d events probed, %d baskets found so far",
                     idx, len(candidate_events), len(baskets))

        examined += 1

        # Gather best asks for the Yes leg of each outcome
        legs: list[ArbLeg] = []
        bad_book = False
        for m in mkts:
            wt = _winning_token(m)
            if not wt:
                bad_book = True
                break
            token_id, outcome_label = wt
            book = _get_orderbook(token_id)
            if not book:
                bad_book = True
                break
            ba = _best_ask(book)
            if not ba:
                bad_book = True
                break
            ask_price, ask_size = ba
            tick = float(m.get("orderPriceMinTickSize") or m.get("minimumTickSize") or 0.01)
            legs.append(ArbLeg(
                market_slug=m.get("slug") or "",
                token_id=token_id,
                outcome=m.get("groupItemTitle") or outcome_label or m.get("question", "")[:40],
                best_ask=ask_price,
                best_ask_size=ask_size,
                condition_id=m.get("conditionId") or m.get("condition_id") or "",
                tick_size=tick,
            ))
        if bad_book or not legs:
            skipped_no_book += 1
            continue

        sum_asks = sum(l.best_ask for l in legs)
        # Edge after safety fee buffer
        profit_per_share = 1.0 - sum_asks - ARB_FEE_HAIRCUT
        if profit_per_share <= 0:
            skipped_negative_edge += 1
            continue
        profit_pct = profit_per_share / sum_asks if sum_asks > 0 else 0.0
        if profit_pct < MIN_ARB_PROFIT_PCT:
            skipped_negative_edge += 1
            continue

        # Limit basket size by smallest leg's available size
        max_shares = min(l.best_ask_size for l in legs)
        if max_shares < MIN_BASKET_SHARES:
            continue

        # Cap by USD budget
        cost_per_share = sum_asks
        max_shares_by_usd = MAX_BASKET_USD / cost_per_share if cost_per_share > 0 else 0
        recommended_shares = min(max_shares, max_shares_by_usd)
        basket_cost = recommended_shares * sum_asks
        if basket_cost < MIN_BASKET_USD:
            continue
        expected_profit = recommended_shares * profit_per_share

        # Compute parent event volume + days_to_resolution (already pre-filtered above)
        total_vol = sum(float(m.get("volume24hr") or m.get("volume") or 0) for m in mkts)
        days = min(_days_until(m.get("endDate")) for m in mkts)

        # Use first market's question as event header (Polymarket events usually share parent)
        event_q = mkts[0].get("eventTitle") or mkts[0].get("question", "")[:80]
        event_id = mkts[0].get("eventId") or mkts[0].get("event_id") or ""

        baskets.append(ArbBasket(
            event_slug=event_slug,
            event_question=event_q,
            event_id=str(event_id),
            legs=legs,
            sum_asks=round(sum_asks, 4),
            profit_per_share=round(profit_per_share, 4),
            profit_pct=round(profit_pct, 4),
            max_basket_shares=round(max_shares, 2),
            recommended_shares=round(recommended_shares, 2),
            basket_cost_usd=round(basket_cost, 2),
            expected_profit_usd=round(expected_profit, 2),
            days_to_resolution=round(days, 2),
            total_volume_usd=round(total_vol, 2),
        ))

    # Sort by absolute expected profit descending
    baskets.sort(key=lambda b: b.expected_profit_usd, reverse=True)

    log.info(
        "Arb scan complete: %d events probed → %d baskets profitable; "
        "skipped: no-book=%d, negative-edge=%d (pre-filter dropped: not-neg=%d, too-many-legs=%d, low-volume=%d)",
        examined, len(baskets), skipped_no_book, skipped_negative_edge,
        skipped_not_neg, skipped_too_many_legs, skipped_low_volume_prefilter,
    )
    return baskets


def baskets_to_log_dicts(baskets: list[ArbBasket]) -> list[dict]:
    return [b.to_log() for b in baskets]

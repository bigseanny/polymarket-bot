"""Strategy filters and improvements applied at scan + sizing time.

Five rules layered on top of the base near-certainty scanner:

  #1B  BTC threshold buffer
       Skip BTC "above $X on date Y" markets unless current spot has at least
       MIN_BTC_BUFFER_PCT separation from the strike. Daily BTC σ ≈ 2.5%, so
       at 5% buffer the strike is ~2σ away — a real near-certainty.

  #2   Correlated-bet dedupe
       Within the same event_id, refuse a candidate if we already hold (or
       are about to place) an anti-correlated position. Anti-correlation =
       same event, mutually exclusive outcomes (e.g. "Team A wins" + "Team B
       wins"; "Elon 65-89 No" + "Elon 90-119 No" → both lose iff one range
       hits). Heuristic: same event_slug and either same outcome label on
       different markets, or opposite outcome on the same market.

  #3   Per-event position cap
       At most MAX_POSITIONS_PER_EVENT (default 2) positions in any event.
       Prevents multi-range Elon-tweet stacks from becoming one giant
       correlated bet.

  #4   Per-category edge floor
       Higher implied-vol categories (crypto, oil) require larger edge to
       compensate. Sports/celeb stay at base MIN_EDGE.

  #5   Hard NAV cap per position
       No single position bigger than MAX_PCT_OF_NAV (default 15%) of total
       account NAV. Tail-risk protection independent of Kelly.

Each function returns (ok: bool, reason: str). Reasons are logged so we can
audit later why specific candidates were rejected.
"""
from __future__ import annotations
import logging
import os
import re
import time
from typing import Iterable

import requests

log = logging.getLogger(__name__)

# ── Tunables (env-overridable) ─────────────────────────────────────────
MIN_BTC_BUFFER_PCT = float(os.getenv("MIN_BTC_BUFFER_PCT", "0.05"))  # 5% spot↔strike
BTC_FILTER_ENABLED = os.getenv("BTC_FILTER_ENABLED", "true").lower() in ("1", "true", "yes")

MAX_POSITIONS_PER_EVENT = int(os.getenv("MAX_POSITIONS_PER_EVENT", "2"))

# Per-category edge floors (in cents, e.g. 0.06 = 6¢). Falls back to CFG.MIN_EDGE.
CATEGORY_MIN_EDGE = {
    "crypto": float(os.getenv("MIN_EDGE_CRYPTO", "0.06")),
    "oil": float(os.getenv("MIN_EDGE_OIL", "0.06")),
}
# All other categories use CFG.MIN_EDGE.

MAX_PCT_OF_NAV = float(os.getenv("MAX_PCT_OF_NAV", "0.15"))

# ── BTC spot cache ─────────────────────────────────────────────────────
_btc_spot_cache: dict[str, float] = {"price": 0.0, "ts": 0.0}
_BTC_CACHE_TTL = 30  # seconds


def get_btc_spot() -> float | None:
    """Get BTC spot in USD. Cached 30s.

    Tries Coinbase first (no auth, reliable), falls back to Binance.
    Returns None on failure → caller should fail open (don't reject bets).
    """
    now = time.time()
    if _btc_spot_cache["price"] > 0 and (now - _btc_spot_cache["ts"]) < _BTC_CACHE_TTL:
        return _btc_spot_cache["price"]

    for url, parser in [
        ("https://api.coinbase.com/v2/prices/BTC-USD/spot",
         lambda r: float(r["data"]["amount"])),
        ("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
         lambda r: float(r["price"])),
    ]:
        try:
            r = requests.get(url, timeout=5)
            r.raise_for_status()
            price = parser(r.json())
            if price > 0:
                _btc_spot_cache["price"] = price
                _btc_spot_cache["ts"] = now
                return price
        except Exception as e:
            log.debug("btc spot fetch failed via %s: %s", url, e)
            continue
    return None


# ── Category inference ─────────────────────────────────────────────────
def infer_category(question: str, market_slug: str = "") -> str:
    """Tag a market by category for per-category rules."""
    q = (question + " " + market_slug).lower()
    if "elon musk" in q and "tweet" in q:
        return "elon-tweets"
    if any(k in q for k in ("bitcoin", "btc ", "ethereum", "eth ", "$")):
        if any(k in q for k in ("bitcoin", "btc")):
            return "crypto"
    if any(k in q for k in ("wti", "crude", "oil ", " oil")):
        return "oil"
    if any(k in q for k in (
        "nba", "nfl", "nhl", "mlb", "soccer", "uefa", "champions league",
        "premier league", "playoff", "conference semi", "world cup",
    )):
        return "sports"
    if any(k in q for k in (
        "election", "senedd", "parliament", "mayor", "primary", "reform party",
        "green party", "trump", "biden", "harris",
    )):
        return "politics"
    if any(k in q for k in ("iran", "hormuz", "blockade", "diplomatic", "shutdown")):
        return "geopolitics"
    if any(k in q for k in ("temperature", "climate", "celsius", "fahrenheit")):
        return "climate"
    if any(k in q for k in ("song", "spotify", "billboard", "no. 1")):
        return "music"
    if any(k in q for k in ("ai model", "gpt", "claude", "gemini", "alibaba")):
        return "ai"
    return "other"


# ── Rule #1B: BTC threshold buffer ─────────────────────────────────────
_BTC_STRIKE_RE = re.compile(
    r"bitcoin.*?\$?(\d{2,3}),?(\d{3})|"
    r"btc.*?\$?(\d{2,3}),?(\d{3})",
    re.IGNORECASE,
)


def _parse_btc_strike(question: str) -> float | None:
    """Extract the $X strike from a BTC threshold question.
    'Will the price of Bitcoin be above $80,000 on May 4?' → 80000.0
    """
    m = _BTC_STRIKE_RE.search(question)
    if not m:
        return None
    # Pick whichever pair of groups matched.
    groups = [g for g in m.groups() if g]
    if len(groups) < 2:
        return None
    try:
        return float(groups[0]) * 1000 + float(groups[1])
    except ValueError:
        return None


def check_btc_buffer(question: str, outcome: str, category: str) -> tuple[bool, str]:
    """Returns (ok, reason). Only applies to BTC threshold markets."""
    if not BTC_FILTER_ENABLED or category != "crypto":
        return True, ""
    strike = _parse_btc_strike(question)
    if strike is None:
        # Not a threshold question (or couldn't parse) — let it through.
        return True, ""
    spot = get_btc_spot()
    if spot is None:
        # Fail open: data unavailable, don't block.
        log.debug("BTC spot unavailable, allowing bet through")
        return True, ""

    # The bet's "safe side" depends on outcome label and "above" vs "below".
    # We approximate: if buying YES on "above X", we need spot > X by buffer.
    # If buying NO on "above X", we need spot < X by buffer.
    # We can detect "above" with regex; for now assume "above" (covers nearly
    # all BTC threshold markets on Polymarket).
    is_above = "above" in question.lower() or "reach" in question.lower()
    if not is_above:
        return True, ""  # Unknown phrasing — fail open.

    diff_pct = (spot - strike) / strike  # positive = spot > strike
    if outcome.lower() in ("yes", "above"):
        # Buying YES — bet that BTC IS above X. Need spot already above by buffer.
        if diff_pct < MIN_BTC_BUFFER_PCT:
            return False, (f"BTC spot ${spot:,.0f} only {diff_pct*100:+.1f}% from "
                           f"strike ${strike:,.0f} (need ≥{MIN_BTC_BUFFER_PCT*100:.0f}% buffer)")
    else:
        # Buying NO — bet that BTC stays below X. Need spot already below by buffer.
        if diff_pct > -MIN_BTC_BUFFER_PCT:
            return False, (f"BTC spot ${spot:,.0f} only {diff_pct*100:+.1f}% from "
                           f"strike ${strike:,.0f} (need ≤-{MIN_BTC_BUFFER_PCT*100:.0f}% buffer)")
    return True, f"BTC buffer OK ({diff_pct*100:+.1f}% vs strike)"


# ── Rule #4: per-category edge floor ───────────────────────────────────
def required_edge_for(category: str, base_min_edge: float) -> float:
    """Return the edge floor for this category. Falls back to base."""
    return max(base_min_edge, CATEGORY_MIN_EDGE.get(category, 0.0))


def check_category_edge(category: str, edge: float, base_min_edge: float
                        ) -> tuple[bool, str]:
    """Returns (ok, reason)."""
    required = required_edge_for(category, base_min_edge)
    if edge < required:
        return False, (f"edge {edge:.3f} < required {required:.3f} for category={category}")
    return True, ""


# ── Rules #2 + #3: event-level dedupe and cap ──────────────────────────
def filter_by_event_rules(candidates: list, existing_positions_by_event: dict[str, int]
                          ) -> list:
    """Apply rules #2 and #3 across the candidate list.

    Args:
      candidates: list[Candidate] sorted by annualized_return desc
      existing_positions_by_event: dict[event_slug → count of open positions]

    Returns: filtered candidates list (preserving order)

    Within this pass, we also enforce: never queue >1 candidate from the same
    event_slug PER SCAN (acts as the dedupe). Combined with the per-event
    open-position cap, this prevents stacking.
    """
    out = []
    per_event_this_scan: dict[str, int] = {}
    for c in candidates:
        ev = getattr(c, "event_slug", "") or "?solo?"

        # Per-event TOTAL cap (open + queued this scan).
        already_open = existing_positions_by_event.get(ev, 0)
        queued = per_event_this_scan.get(ev, 0)
        if already_open + queued >= MAX_POSITIONS_PER_EVENT:
            log.info("Filter #3 drop: %s — event '%s' already at cap (%d open + %d queued)",
                     c.market_slug, ev, already_open, queued)
            continue

        # Rule #2 dedupe within this scan: only 1 candidate per event_slug per
        # scan to avoid double-Kelly on the same underlying.
        if queued >= 1:
            log.info("Filter #2 drop: %s — already queued a candidate from event '%s' this scan",
                     c.market_slug, ev)
            continue

        out.append(c)
        per_event_this_scan[ev] = queued + 1
    return out


# ── Rule #5: NAV cap per position ──────────────────────────────────────
def cap_position_by_nav(usd: float, nav_usd: float) -> float:
    """Cap a single position size to MAX_PCT_OF_NAV of total NAV.
    Returns the (possibly reduced) usd amount.
    """
    if nav_usd <= 0:
        return usd
    cap = nav_usd * MAX_PCT_OF_NAV
    if usd > cap:
        log.info("Filter #5 cap: $%.2f → $%.2f (15%% of NAV $%.2f)", usd, cap, nav_usd)
        return cap
    return usd


# ── Helper: query current per-event position counts ────────────────────
def fetch_existing_positions_by_event(funder: str) -> dict[str, int]:
    """Count current open positions grouped by event_slug.

    We map each held conditionId → event by querying Gamma. To avoid an
    N×API-call blowup, we cache and skip events that fail to resolve.
    """
    if not funder:
        return {}
    try:
        r = requests.get(
            "https://data-api.polymarket.com/positions",
            params={"user": funder.lower(), "limit": 100, "sizeThreshold": 0.01},
            timeout=10,
        )
        r.raise_for_status()
        positions = r.json() or []
    except Exception as e:
        log.warning("fetch positions failed: %s", e)
        return {}

    counts: dict[str, int] = {}
    for p in positions:
        ev = (p.get("eventSlug") or p.get("event_slug") or "").strip()
        if not ev:
            # Fallback: use conditionId so at least same-condition stacks count.
            ev = p.get("conditionId", "")
        if ev:
            counts[ev] = counts.get(ev, 0) + 1
    return counts


# ── Composite check: run all per-candidate filters ─────────────────────
def passes_strategy_filters(question: str, market_slug: str, outcome: str,
                            edge: float, base_min_edge: float
                            ) -> tuple[bool, str, str]:
    """Run the per-candidate filters (#1B, #4). Returns (ok, reason, category)."""
    category = infer_category(question, market_slug)

    ok, reason = check_btc_buffer(question, outcome, category)
    if not ok:
        return False, f"#1B {reason}", category

    ok, reason = check_category_edge(category, edge, base_min_edge)
    if not ok:
        return False, f"#4 {reason}", category

    return True, "", category

"""
Sizing: fractional Kelly across qualifying near-certainty candidates.

Kelly for a binary bet at decimal odds:
    For a YES bought at price p (so winning pays out 1.0, profit per $1 = (1-p)/p
    if right, lose $p if wrong):
        b = (1 - p) / p   # payoff multiple of stake
        q = 1 - prob_true
        f* = (b * prob_true - q) / b
           = prob_true - q / b
           = prob_true - (1 - prob_true) * p / (1 - p)

We use prob_true = 1 - HAIRCUT (e.g. 0.99) and apply KELLY_FRACTION (e.g. 0.25)
to shrink. Also enforce per-market and book-depth caps.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass

from config import CFG
from scanner import Candidate

log = logging.getLogger(__name__)


@dataclass
class Sized:
    candidate: Candidate
    usd: float        # dollars to deploy
    shares: float     # shares to buy = usd / price (rounded to tick)
    kelly_raw: float  # unscaled Kelly fraction


def _kelly_fraction(price: float, prob_true: float) -> float:
    """Full-Kelly fraction of bankroll for a YES bet at `price`."""
    if price <= 0 or price >= 1:
        return 0.0
    b = (1.0 - price) / price
    q = 1.0 - prob_true
    f = (b * prob_true - q) / b
    return max(0.0, f)


def size_portfolio(
    candidates: list[Candidate],
    bankroll: float | None = None,
) -> list[Sized]:
    """Allocate `bankroll` across the top candidates using fractional Kelly."""
    bankroll = bankroll if bankroll is not None else CFG.BANKROLL_USD
    prob_true = 1.0 - CFG.HAIRCUT

    if not candidates:
        return []

    # Take top N by edge.
    pool = candidates[: CFG.MAX_POSITIONS]

    # Compute raw Kelly weights, then normalize so we don't over-deploy.
    raw = []
    for c in pool:
        f = _kelly_fraction(c.best_ask, prob_true) * CFG.KELLY_FRACTION
        raw.append(f)

    total_f = sum(raw)
    if total_f <= 0:
        return []

    # If sum of fractional-Kelly stakes exceeds 1.0 of bankroll, scale down.
    scale = min(1.0, 1.0 / total_f) if total_f > 0 else 0.0

    sized: list[Sized] = []
    for c, f in zip(pool, raw):
        usd = bankroll * f * scale

        # Hard per-market cap.
        usd = min(usd, CFG.MAX_PER_MARKET_USD)

        # Don't eat too much of the best-ask depth. ask_size is in shares.
        max_book_usd = c.best_ask_size * c.best_ask * CFG.MAX_PCT_OF_BOOK
        usd = min(usd, max_book_usd)

        if usd < CFG.MIN_ORDER_USD:
            continue

        # Round shares to satisfy tick & lot. Polymarket min order = 5 shares
        # at most prices; we round to whole shares to be safe.
        shares = round(usd / c.best_ask, 2)
        if shares < 5:
            continue

        sized.append(Sized(candidate=c, usd=round(usd, 2), shares=shares, kelly_raw=f))

    return sized

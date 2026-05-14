"""Momentum scanner — velocity + volume entry signal.

THESIS
------
Polymarket prices that move sharply on news with above-normal volume tend to
keep moving in the same direction for the next 4-12 hours as the market
absorbs the catalyst. We enter ON the move (not against it) and exit after
small profit, small loss, or time expiry.

This is the *inverse* of the near-certainty strategy: we are buying volatility
and trading the catalyst window, not buying resolution edge.

SIGNAL
------
Trigger candidate IF all four:
  1. |Δprice over 1h| ≥ 5%                         (significant move)
  2. volume_1h ≥ 2 × (avg volume_1h over 24h)        (above-normal participation)
  3. 0.10 ≤ current_price ≤ 0.90                     (avoid resolution-zone)
  4. days_to_resolution ≥ 1                          (need room for the move)

Direction
---------
- If Δprice > 0 → buy the Yes side (going up)
- If Δprice < 0 → buy the No side  (going down — i.e. anti-Yes)

Sizing
------
Each position capped at MOMENTUM_MAX_PCT_NAV (default 5% of NAV).
Smaller per-position than nearcert because variance is much higher.

Exit (handled by executor / stop_loss; we only emit candidates)
---------------------------------------------------------------
- Take profit at +5%
- Stop at -3%
- Time stop at 12h
- Cooldown: same token can't be re-entered within 6h of exit
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Any

import requests

from config import CFG

log = logging.getLogger(__name__)

# Env-tunable knobs
MOMENTUM_MIN_VELOCITY_PCT  = float(os.environ.get("MOMENTUM_MIN_VELOCITY_PCT", "0.05"))   # 5% move
MOMENTUM_VELOCITY_WINDOW_H = float(os.environ.get("MOMENTUM_VELOCITY_WINDOW_H", "1.0"))   # last 1h
MOMENTUM_VOL_MULTIPLIER    = float(os.environ.get("MOMENTUM_VOL_MULTIPLIER", "2.0"))      # 2x avg
MOMENTUM_MIN_PRICE         = float(os.environ.get("MOMENTUM_MIN_PRICE", "0.10"))
MOMENTUM_MAX_PRICE         = float(os.environ.get("MOMENTUM_MAX_PRICE", "0.90"))
MOMENTUM_MIN_DAYS          = float(os.environ.get("MOMENTUM_MIN_DAYS", "1.0"))
MOMENTUM_MAX_DAYS          = float(os.environ.get("MOMENTUM_MAX_DAYS", "14.0"))
MOMENTUM_MIN_VOLUME_USD    = float(os.environ.get("MOMENTUM_MIN_VOLUME_USD", "20000"))    # parent market $20k+/24h
MOMENTUM_MAX_PCT_NAV       = float(os.environ.get("MOMENTUM_MAX_PCT_NAV", "0.05"))        # 5% per position
MOMENTUM_TAKE_PROFIT_PCT   = float(os.environ.get("MOMENTUM_TAKE_PROFIT_PCT", "0.05"))
MOMENTUM_STOP_LOSS_PCT     = float(os.environ.get("MOMENTUM_STOP_LOSS_PCT", "0.03"))
MOMENTUM_TIME_STOP_H       = float(os.environ.get("MOMENTUM_TIME_STOP_H", "12.0"))
MOMENTUM_COOLDOWN_H        = float(os.environ.get("MOMENTUM_COOLDOWN_H", "6.0"))


@dataclass
class MomentumCandidate:
    market_slug: str
    question: str
    condition_id: str
    token_id: str         # token to buy
    outcome: str          # "Yes" or "No" — which side we're buying
    direction: str        # "long_yes" or "long_no"
    entry_price: float    # current best ask of the side we'd buy
    price_1h_ago: float
    velocity_pct: float   # signed % change over the window
    abs_velocity_pct: float
    volume_1h: float
    avg_volume_1h_24h: float
    volume_multiple: float
    days_to_resolution: float
    take_profit_price: float
    stop_loss_price: float
    time_stop_at: str     # ISO timestamp of forced exit
    recommended_size_usd: float

    def to_log(self) -> dict:
        d = asdict(self)
        d["scanned_at"] = datetime.now(timezone.utc).isoformat()
        return d


# ── prices-history helpers (reuse stop_loss conventions) ─────────────
def _fetch_prices_history(token_id: str, hours: float = 26.0) -> list[tuple[datetime, float]]:
    """Pull prices-history for the last N hours. Returns sorted (ts, price) tuples.

    NOTE: fidelity=60 returns ~1 point/hour on Polymarket (see stop_loss API gotcha).
    We use a 26h window to ensure we have 24h+ of context for averaging.
    """
    end = int(time.time())
    start = int(end - hours * 3600)
    try:
        r = requests.get(
            f"{CFG.CLOB_API}/prices-history",
            params={"market": token_id, "startTs": start, "endTs": end, "fidelity": 60},
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()
        history = data.get("history") if isinstance(data, dict) else data
        out: list[tuple[datetime, float]] = []
        for pt in history or []:
            if "t" in pt and "p" in pt:
                ts = datetime.fromtimestamp(int(pt["t"]), tz=timezone.utc)
                out.append((ts, float(pt["p"])))
        out.sort(key=lambda x: x[0])
        return out
    except Exception as e:
        log.debug("prices-history failed for %s: %s", token_id[:10], e)
        return []


def _velocity(history: list[tuple[datetime, float]], window_h: float) -> tuple[float, float, float] | None:
    """Return (price_now, price_at_window_start, velocity_pct).

    velocity_pct is signed: positive means up over the window.
    """
    if len(history) < 2:
        return None
    now = history[-1]
    target_ts = now[0] - timedelta(hours=window_h)
    # Find closest earlier point to target_ts
    best = None
    for ts, price in history:
        if ts <= target_ts:
            best = (ts, price)
        else:
            break
    if best is None:
        # Window longer than our data; use earliest point
        best = history[0]
    p_now = now[1]
    p_then = best[1]
    if p_then <= 0:
        return None
    return (p_now, p_then, (p_now - p_then) / p_then)


def _hourly_volume_series(history: list[tuple[datetime, float]]) -> list[float]:
    """We don't have a volume time-series from prices-history. Approximate with
    *price-change magnitude per hour* as a proxy for activity. This is a known
    limitation — for a more accurate volume proxy we'd need /trades data, but
    /trades is the global firehose (token filter broken).

    Alternative: use rolling absolute returns as a 'realized vol' proxy.
    Returns list of |Δp| per hourly bucket over the last ~24h.
    """
    if len(history) < 4:
        return []
    deltas: list[float] = []
    for i in range(1, len(history)):
        prev = history[i - 1]
        cur = history[i]
        gap = (cur[0] - prev[0]).total_seconds() / 3600
        if gap <= 0 or gap > 2:
            # Skip gaps wider than 2h (data hole)
            continue
        deltas.append(abs(cur[1] - prev[1]))
    return deltas[-24:]  # last ~24 hourly deltas


def _avg(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


# ── Orderbook (re-use simple book fetch) ─────────────────────────────
def _best_ask(token_id: str) -> tuple[float, float] | None:
    try:
        r = requests.get(f"{CFG.CLOB_API}/book", params={"token_id": token_id}, timeout=6)
        r.raise_for_status()
        book = r.json()
        asks = book.get("asks") or []
        if not asks:
            return None
        return (float(asks[0]["price"]), float(asks[0]["size"]))
    except Exception:
        return None


def _days_until(end_iso: str | None) -> float:
    if not end_iso:
        return 999.0
    try:
        end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        return max(0.0, (end - datetime.now(timezone.utc)).total_seconds() / 86400)
    except Exception:
        return 999.0


# ── Cooldown ─────────────────────────────────────────────────────────
def _load_cooldown(path: str) -> dict[str, str]:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _in_cooldown(token_id: str, cooldown: dict[str, str], hours: float) -> bool:
    last_iso = cooldown.get(token_id)
    if not last_iso:
        return False
    try:
        last = datetime.fromisoformat(last_iso)
        return (datetime.now(timezone.utc) - last).total_seconds() < hours * 3600
    except Exception:
        return False


# ── Main scan ────────────────────────────────────────────────────────
def scan_momentum_candidates(
    markets: list[dict],
    nav_usd: float,
    cooldown_path: str = "logs/momentum_cooldown.json",
) -> list[MomentumCandidate]:
    """For each market, compute 1h velocity, check signal, emit candidate."""
    cooldown = _load_cooldown(cooldown_path)
    candidates: list[MomentumCandidate] = []
    examined = 0
    skipped_book = 0
    skipped_history = 0
    skipped_signal = 0
    skipped_filter = 0
    skipped_cooldown = 0

    for m in markets:
        days = _days_until(m.get("endDate"))
        if days < MOMENTUM_MIN_DAYS or days > MOMENTUM_MAX_DAYS:
            skipped_filter += 1
            continue
        vol24 = float(m.get("volume24hr") or m.get("volume") or 0)
        if vol24 < MOMENTUM_MIN_VOLUME_USD:
            skipped_filter += 1
            continue
        tokens = m.get("clobTokenIds") or m.get("tokens") or []
        outcomes = m.get("outcomes") or ["Yes", "No"]
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except Exception:
                continue
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except Exception:
                outcomes = ["Yes", "No"]
        if not tokens or len(tokens) < 2:
            continue
        yes_token = str(tokens[0])
        no_token = str(tokens[1])
        yes_label = outcomes[0] if len(outcomes) >= 1 else "Yes"
        no_label = outcomes[1] if len(outcomes) >= 2 else "No"

        examined += 1

        # Pull 26h of price history on Yes leg (No is symmetric: p_no = 1 - p_yes)
        hist = _fetch_prices_history(yes_token, hours=26.0)
        if len(hist) < 4:
            skipped_history += 1
            continue
        v = _velocity(hist, MOMENTUM_VELOCITY_WINDOW_H)
        if v is None:
            skipped_history += 1
            continue
        p_now, p_then, velocity = v

        # Signal #1: |velocity| ≥ threshold
        if abs(velocity) < MOMENTUM_MIN_VELOCITY_PCT:
            skipped_signal += 1
            continue

        # Signal #2: volume_proxy (last hour |Δp|) ≥ multiplier × avg over 24h
        deltas = _hourly_volume_series(hist)
        if len(deltas) < 4:
            skipped_history += 1
            continue
        last_delta = deltas[-1]
        avg_delta = _avg(deltas[:-1]) if len(deltas) > 1 else 0.0
        vol_multiple = (last_delta / avg_delta) if avg_delta > 0 else 0.0
        if vol_multiple < MOMENTUM_VOL_MULTIPLIER:
            skipped_signal += 1
            continue

        # Decide direction
        if velocity > 0:
            entry_token = yes_token
            entry_outcome = yes_label
            direction = "long_yes"
        else:
            entry_token = no_token
            entry_outcome = no_label
            direction = "long_no"

        # Cooldown check
        if _in_cooldown(entry_token, cooldown, MOMENTUM_COOLDOWN_H):
            skipped_cooldown += 1
            continue

        # Pull live best ask for the side we'd actually buy
        ba = _best_ask(entry_token)
        if not ba:
            skipped_book += 1
            continue
        entry_price, ask_size = ba

        # Signal #3: price in trading range
        if entry_price < MOMENTUM_MIN_PRICE or entry_price > MOMENTUM_MAX_PRICE:
            skipped_filter += 1
            continue

        # Compute exits relative to entry
        tp = round(entry_price * (1 + MOMENTUM_TAKE_PROFIT_PCT), 4)
        sl = round(entry_price * (1 - MOMENTUM_STOP_LOSS_PCT), 4)
        time_stop = (datetime.now(timezone.utc) + timedelta(hours=MOMENTUM_TIME_STOP_H)).isoformat()

        # Sizing
        size_usd = round(min(nav_usd * MOMENTUM_MAX_PCT_NAV, ask_size * entry_price), 2)
        if size_usd < 5:
            continue

        candidates.append(MomentumCandidate(
            market_slug=m.get("slug") or "",
            question=m.get("question") or "",
            condition_id=m.get("conditionId") or m.get("condition_id") or "",
            token_id=entry_token,
            outcome=entry_outcome,
            direction=direction,
            entry_price=entry_price,
            price_1h_ago=round(p_then, 4),
            velocity_pct=round(velocity, 4),
            abs_velocity_pct=round(abs(velocity), 4),
            volume_1h=round(last_delta, 4),
            avg_volume_1h_24h=round(avg_delta, 4),
            volume_multiple=round(vol_multiple, 2),
            days_to_resolution=round(days, 2),
            take_profit_price=tp,
            stop_loss_price=sl,
            time_stop_at=time_stop,
            recommended_size_usd=size_usd,
        ))

    candidates.sort(key=lambda c: c.abs_velocity_pct * c.volume_multiple, reverse=True)
    log.info(
        "Momentum scan: examined=%d → %d candidates; skipped: book=%d history=%d "
        "signal=%d filter=%d cooldown=%d",
        examined, len(candidates),
        skipped_book, skipped_history, skipped_signal, skipped_filter, skipped_cooldown,
    )
    return candidates


def record_exit(token_id: str, cooldown_path: str = "logs/momentum_cooldown.json") -> None:
    """Mark a token as exited so cooldown applies."""
    try:
        try:
            with open(cooldown_path) as f:
                data = json.load(f)
        except Exception:
            data = {}
        data[token_id] = datetime.now(timezone.utc).isoformat()
        with open(cooldown_path, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log.warning("failed to record momentum cooldown: %s", e)

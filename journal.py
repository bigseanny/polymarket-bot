"""Trade journal: log every order and reconcile outcomes.

Every order placed goes to logs/trades.jsonl with a normalized schema.
A weekly digest (Mondays 09:00 HKT = 01:00 UTC) computes win rate, ROI,
edge captured vs. estimated, slippage, and category breakdowns, then
sends to Telegram.

Usage:
    from journal import record_trade
    record_trade(result_dict_from_executor)   # called inside execute()

    python journal.py --digest                # send weekly digest now (cron)
    python journal.py --reconcile             # update outcomes for resolved trades
"""
from __future__ import annotations
import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent))

from notify import notify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

JOURNAL = Path(__file__).parent / "logs" / "trades.jsonl"
PROXY = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").lower()
DATA_API = "https://data-api.polymarket.com"


# ── Recording ──────────────────────────────────────────────────────────
def record_trade(result: dict) -> None:
    """Append a trade to the journal. Called from executor.execute().

    Skips errors and skipped/dry-run rows so the journal contains only
    real submitted fills (status='submitted').
    """
    if result.get("status") != "submitted":
        return
    entry = {
        "ts": result.get("ts") or datetime.now(timezone.utc).isoformat(),
        "market": result.get("market"),
        "outcome": result.get("outcome"),
        "token_id": result.get("token_id"),
        "entry_price": result.get("price"),
        "shares": result.get("shares"),
        "cost_usd": result.get("usd"),
        "edge_estimated": result.get("edge"),
        "days_to_resolution": result.get("days_to_resolution"),
        "annualized_return": result.get("annualized_return"),
        "category": _infer_category(result.get("market", "")),
        "outcome_resolved": None,   # filled later by --reconcile
        "payout_usd": None,
        "pnl_usd": None,
        "resolution_ts": None,
    }
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _infer_category(slug: str) -> str:
    """Best-effort category from slug keywords."""
    s = slug.lower()
    if any(k in s for k in ("nba", "nfl", "nhl", "mlb", "ufc", "soccer",
                             "premier-league", "champions-league", "la-liga",
                             "ligue-1", "serie-a", "bundesliga", "world-cup")):
        return "sports"
    if any(k in s for k in ("trump", "biden", "election", "senate", "house",
                             "president", "vote", "primary")):
        return "politics"
    if any(k in s for k in ("bitcoin", "btc", "ethereum", "eth", "crypto",
                             "solana", "doge", "stablecoin")):
        return "crypto"
    if any(k in s for k in ("musk", "tweet", "elon", "celebrity", "kanye",
                             "taylor")):
        return "celeb"
    if any(k in s for k in ("fed", "rate", "cpi", "inflation", "gdp",
                             "unemployment")):
        return "macro"
    return "other"


def _read_journal() -> list[dict]:
    if not JOURNAL.exists():
        return []
    out = []
    with JOURNAL.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _write_journal(entries: list[dict]) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    tmp = JOURNAL.with_suffix(".jsonl.tmp")
    with tmp.open("w") as f:
        for e in entries:
            f.write(json.dumps(e, default=str) + "\n")
    tmp.replace(JOURNAL)


# ── Reconciliation ─────────────────────────────────────────────────────
def reconcile() -> int:
    """Walk unresolved entries, query Polymarket data API for current state,
    and fill outcome_resolved/payout_usd/pnl_usd when the market has
    resolved. Returns count of newly resolved trades.
    """
    entries = _read_journal()
    if not entries:
        return 0

    # Pull all positions (open + closed) for the proxy.
    positions = _fetch_all_positions()
    by_token: dict[str, dict] = {}
    for p in positions:
        tok = (p.get("asset") or p.get("tokenId") or "").lower()
        if tok:
            by_token[tok] = p

    newly = 0
    for e in entries:
        if e.get("outcome_resolved") is not None:
            continue
        tok = (e.get("token_id") or "").lower()
        p = by_token.get(tok)
        if not p:
            continue
        # A market is resolved when redeemable=True OR currentValue is 0/1
        # AND it's no longer "open". The data API exposes `redeemable` and
        # `endDate`. We treat: redeemable + winner-known as resolved.
        redeemable = bool(p.get("redeemable"))
        cur_price = float(p.get("curPrice") or 0)
        # Strict resolution check: redeemable AND price is 0 or 1 (terminal).
        is_resolved = redeemable and cur_price in (0.0, 1.0)
        if not is_resolved:
            continue
        won = cur_price >= 0.999
        shares = float(e.get("shares") or 0)
        cost = float(e.get("cost_usd") or 0)
        payout = shares if won else 0.0       # winning shares pay $1 each
        pnl = payout - cost
        e["outcome_resolved"] = "won" if won else "lost"
        e["payout_usd"] = round(payout, 2)
        e["pnl_usd"] = round(pnl, 2)
        e["resolution_ts"] = datetime.now(timezone.utc).isoformat()
        newly += 1

    if newly:
        _write_journal(entries)
        log.info("Reconciled %d newly resolved trades", newly)
    return newly


def _fetch_all_positions() -> list[dict]:
    """Pull positions including resolved ones (sizeThreshold=0)."""
    if not PROXY:
        return []
    out: list[dict] = []
    offset = 0
    page = 100
    while True:
        try:
            r = requests.get(
                f"{DATA_API}/positions",
                params={
                    "user": PROXY,
                    "limit": page,
                    "offset": offset,
                    "sizeThreshold": 0,
                },
                timeout=20,
            )
            r.raise_for_status()
            batch = r.json() or []
        except Exception as ex:
            log.warning("positions fetch failed: %s", ex)
            break
        if not batch:
            break
        out.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return out


# ── Weekly digest ──────────────────────────────────────────────────────
def _fmt_pct(x: float) -> str:
    sign = "+" if x >= 0 else ""
    return f"{sign}{x*100:.1f}%"


def _fmt_usd(x: float) -> str:
    sign = "+" if x >= 0 else "-"
    return f"{sign}${abs(x):,.2f}"


def build_digest(days: int = 7) -> str:
    """Build the weekly digest message. Reconciles first."""
    reconcile()
    entries = _read_journal()
    if not entries:
        return "📒 *Weekly Trade Journal*\n\nNo trades recorded yet."

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    recent: list[dict] = []
    for e in entries:
        try:
            ts = datetime.fromisoformat(str(e["ts"]).replace("Z", "+00:00"))
            if ts >= cutoff:
                recent.append(e)
        except Exception:
            continue

    # All-time roll-up for context.
    total_resolved = [e for e in entries if e.get("outcome_resolved")]
    wins = sum(1 for e in total_resolved if e["outcome_resolved"] == "won")
    losses = len(total_resolved) - wins
    total_pnl = sum(float(e.get("pnl_usd") or 0) for e in total_resolved)
    total_cost = sum(float(e.get("cost_usd") or 0) for e in total_resolved)
    all_roi = (total_pnl / total_cost) if total_cost else 0.0
    all_winrate = (wins / len(total_resolved)) if total_resolved else 0.0

    # Last-week activity.
    new_count = len(recent)
    recent_resolved = [e for e in recent if e.get("outcome_resolved")]
    recent_wins = sum(1 for e in recent_resolved if e["outcome_resolved"] == "won")
    recent_pnl = sum(float(e.get("pnl_usd") or 0) for e in recent_resolved)
    recent_cost = sum(float(e.get("cost_usd") or 0) for e in recent_resolved)
    recent_roi = (recent_pnl / recent_cost) if recent_cost else 0.0
    recent_winrate = (recent_wins / len(recent_resolved)) if recent_resolved else 0.0
    recent_invested = sum(float(e.get("cost_usd") or 0) for e in recent)
    avg_edge = (sum(float(e.get("edge_estimated") or 0) for e in recent)
                / new_count) if new_count else 0.0

    # Category breakdown (resolved trades only, all-time).
    cats: dict[str, dict[str, float]] = defaultdict(lambda: {"n": 0, "wins": 0,
                                                              "pnl": 0.0,
                                                              "cost": 0.0})
    for e in total_resolved:
        c = cats[e.get("category") or "other"]
        c["n"] += 1
        if e["outcome_resolved"] == "won":
            c["wins"] += 1
        c["pnl"] += float(e.get("pnl_usd") or 0)
        c["cost"] += float(e.get("cost_usd") or 0)
    cat_lines = []
    for name, c in sorted(cats.items(), key=lambda kv: -kv[1]["n"]):
        wr = (c["wins"] / c["n"]) if c["n"] else 0
        roi = (c["pnl"] / c["cost"]) if c["cost"] else 0
        cat_lines.append(
            f"  • {name:<8} {int(c['n']):>3}t  WR {wr*100:>4.0f}%  "
            f"ROI {_fmt_pct(roi):>7}  PnL {_fmt_usd(c['pnl'])}"
        )
    cats_block = "\n".join(cat_lines) if cat_lines else "  (no resolved yet)"

    # Top 3 winners + losers this week.
    sorted_by_pnl = sorted(recent_resolved, key=lambda e: float(e.get("pnl_usd") or 0))
    losers = sorted_by_pnl[:3]
    winners = sorted_by_pnl[-3:][::-1]

    def _line(e: dict) -> str:
        slug = (e.get("market") or "?")[:42]
        pnl = float(e.get("pnl_usd") or 0)
        return f"  {_fmt_usd(pnl):>10}  {slug}"

    winners_block = "\n".join(_line(e) for e in winners) if winners else "  (none)"
    losers_block = "\n".join(_line(e) for e in losers) if losers else "  (none)"

    open_count = len(entries) - len(total_resolved)

    msg = (
        f"📒 *Weekly Trade Journal* · last {days}d\n\n"
        f"🔹 *This week*\n"
        f"  New trades: {new_count}  ·  Invested: ${recent_invested:,.2f}\n"
        f"  Avg estimated edge: {avg_edge:+.3f}\n"
        f"  Resolved: {len(recent_resolved)} ({recent_wins}W / "
        f"{len(recent_resolved)-recent_wins}L) · "
        f"WR {recent_winrate*100:.0f}%\n"
        f"  Realized PnL: {_fmt_usd(recent_pnl)} · ROI {_fmt_pct(recent_roi)}\n\n"
        f"🔹 *All-time*\n"
        f"  Resolved: {len(total_resolved)} ({wins}W / {losses}L) · "
        f"WR {all_winrate*100:.0f}%\n"
        f"  Realized PnL: {_fmt_usd(total_pnl)} · ROI {_fmt_pct(all_roi)}\n"
        f"  Open: {open_count}\n\n"
        f"🔹 *By category (all-time)*\n{cats_block}\n\n"
        f"🏆 *Top winners (week)*\n{winners_block}\n\n"
        f"🩸 *Top losers (week)*\n{losers_block}"
    )
    return msg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--digest", action="store_true",
                    help="Send weekly digest to Telegram")
    ap.add_argument("--reconcile", action="store_true",
                    help="Reconcile resolved trades and exit")
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    if args.reconcile:
        n = reconcile()
        log.info("Reconciled %d trades", n)
        return 0

    if args.digest:
        msg = build_digest(days=args.days)
        log.info("Sending digest:\n%s", msg)
        notify(msg)
        return 0

    print("Use --digest or --reconcile")
    return 1


if __name__ == "__main__":
    sys.exit(main())

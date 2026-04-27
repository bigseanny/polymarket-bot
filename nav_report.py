"""Daily NAV report → Telegram.

Run once daily via systemd timer / cron. Reads:
  * USDC.e proxy balance (cash on Polymarket)
  * Open positions and their current value
  * Yesterday's snapshot (saved to logs/nav_history.json) for daily delta

Sends a concise Telegram message with NAV, PnL, and top 3 positions.
"""
from __future__ import annotations
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent))

from bankroll import get_usdc_balance
from notify import notify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

PROXY = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").lower()
HISTORY_FILE = Path(__file__).parent / "logs" / "nav_history.json"
DATA_API = "https://data-api.polymarket.com"


def fetch_positions() -> list[dict]:
    """Fetch all open positions from Polymarket data API."""
    try:
        r = requests.get(
            f"{DATA_API}/positions",
            params={"user": PROXY, "limit": 50, "sizeThreshold": 0},
            timeout=15,
        )
        r.raise_for_status()
        return r.json() or []
    except Exception as e:
        log.warning("Failed to fetch positions: %s", e)
        return []


def load_history() -> dict:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_snapshot(nav: float, today: str, history: dict) -> None:
    history[today] = round(nav, 2)
    # Keep last 30 days
    keys = sorted(history.keys())[-30:]
    history = {k: history[k] for k in keys}
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


def fmt_pct(x: float) -> str:
    sign = "+" if x >= 0 else ""
    return f"{sign}{x:.2f}%"


def fmt_usd(x: float) -> str:
    sign = "+" if x >= 0 else "-"
    return f"{sign}${abs(x):,.2f}"


def build_report() -> str:
    cash = get_usdc_balance(PROXY) or 0.0
    positions = fetch_positions()
    pos_value = sum(p.get("currentValue", 0) for p in positions)
    invested = sum(p.get("initialValue", 0) for p in positions)
    unrealized = sum(p.get("cashPnl", 0) for p in positions)
    nav = cash + pos_value

    history = load_history()
    today = datetime.now(timezone.utc).date().isoformat()
    yesterday_keys = [k for k in sorted(history.keys()) if k < today]
    daily_delta_str = ""
    if yesterday_keys:
        prev_nav = history[yesterday_keys[-1]]
        delta = nav - prev_nav
        delta_pct = (delta / prev_nav * 100) if prev_nav else 0
        prev_label = yesterday_keys[-1]
        daily_delta_str = f"\n📊 Since {prev_label}: {fmt_usd(delta)} ({fmt_pct(delta_pct)})"

    # Top 3 positions by current value
    top = sorted(positions, key=lambda p: p.get("currentValue", 0), reverse=True)[:3]
    top_lines = []
    for p in top:
        title = p.get("title", "?")
        # Truncate long titles
        if len(title) > 50:
            title = title[:47] + "..."
        outcome = p.get("outcome", "?")
        val = p.get("currentValue", 0)
        pnl = p.get("cashPnl", 0)
        cur_price = p.get("curPrice", 0)
        top_lines.append(f"  • {outcome} ${val:.2f} ({fmt_usd(pnl)}) @ ${cur_price:.3f}\n    {title}")
    top_block = "\n".join(top_lines) if top_lines else "  (no open positions)"

    redeemable = [p for p in positions if p.get("redeemable")]
    redeem_block = ""
    if redeemable:
        total_redeem = sum(p.get("currentValue", 0) for p in redeemable)
        redeem_block = (f"\n\n🟢 {len(redeemable)} redeemable "
                        f"(${total_redeem:.2f}) — claim on polymarket.com")

    pnl_pct = (unrealized / invested * 100) if invested else 0

    msg = (
        f"📈 *Polymarket Daily Report*\n"
        f"_{today}_\n\n"
        f"💰 NAV: *${nav:,.2f}*\n"
        f"  ├ Cash: ${cash:,.2f}\n"
        f"  └ Positions: ${pos_value:,.2f} ({len(positions)} open)\n\n"
        f"📊 Unrealized: {fmt_usd(unrealized)} ({fmt_pct(pnl_pct)})"
        f"{daily_delta_str}\n\n"
        f"🏆 Top positions:\n{top_block}"
        f"{redeem_block}"
    )

    save_snapshot(nav, today, history)
    return msg


def main() -> int:
    if not PROXY:
        log.error("POLYMARKET_FUNDER_ADDRESS not set")
        return 1
    msg = build_report()
    log.info("Sending NAV report:\n%s", msg)
    notify(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())

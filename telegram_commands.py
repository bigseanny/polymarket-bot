"""Telegram command listener.

Polls getUpdates each bot loop iteration. Currently supports:
  /status   — show running state, NAV, recent stops, last scan summary

Pause/resume commands removed — bot now uses per-position stop loss instead
of a global circuit breaker.
"""
from __future__ import annotations
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

ROOT = Path(__file__).parent
LOG_DIR = ROOT / "logs"
TG_OFFSET_FILE = LOG_DIR / "tg_offset.txt"
EXITS_LOG = LOG_DIR / "stop_exits.jsonl"
JOURNAL = LOG_DIR / "trades.jsonl"

_TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
_TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def _read_offset() -> int:
    if TG_OFFSET_FILE.exists():
        try:
            return int(TG_OFFSET_FILE.read_text().strip())
        except Exception:
            return 0
    return 0


def _write_offset(off: int) -> None:
    TG_OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    TG_OFFSET_FILE.write_text(str(off))


def _tg_send(text: str) -> None:
    if not (_TG_TOKEN and _TG_CHAT):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{_TG_TOKEN}/sendMessage",
            json={"chat_id": _TG_CHAT, "text": text[:4000],
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:
        log.warning("tg send failed: %s", e)


def poll_telegram_commands() -> None:
    """Non-blocking poll for new Telegram messages. Safe to call every loop."""
    if not (_TG_TOKEN and _TG_CHAT):
        return
    try:
        offset = _read_offset()
        params = {"timeout": 0, "limit": 20}
        if offset:
            params["offset"] = offset + 1
        r = requests.get(
            f"https://api.telegram.org/bot{_TG_TOKEN}/getUpdates",
            params=params, timeout=10,
        )
        r.raise_for_status()
        data = r.json() or {}
        updates = data.get("result", []) or []
    except Exception as e:
        log.debug("tg poll failed: %s", e)
        return

    max_seen = offset
    for u in updates:
        max_seen = max(max_seen, int(u.get("update_id", 0)))
        msg = u.get("message") or u.get("channel_post") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if chat_id != _TG_CHAT:
            continue
        text = (msg.get("text") or "").strip().lower()
        if text in ("/status", "status"):
            _send_status()

    if max_seen != offset:
        _write_offset(max_seen)


def _send_status() -> None:
    """Render bot status: NAV, recent stops, journal counts."""
    funder = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
    nav_str = "n/a"
    pos_count = 0
    if funder:
        try:
            from bankroll import get_usdc_balance
            cash = get_usdc_balance(funder) or 0.0
            r = requests.get(
                "https://data-api.polymarket.com/positions",
                params={"user": funder.lower(), "limit": 100, "sizeThreshold": 0.01},
                timeout=10,
            )
            r.raise_for_status()
            positions = r.json() or []
            pos_value = sum(float(p.get("currentValue") or 0) for p in positions)
            pos_count = len(positions)
            nav_str = f"${cash + pos_value:,.2f} (cash ${cash:,.2f} + positions ${pos_value:,.2f})"
        except Exception as e:
            nav_str = f"error: {e}"

    # Recent stops in last 7d.
    recent_stops = 0
    realized_pnl_7d = 0.0
    if EXITS_LOG.exists():
        try:
            cutoff = datetime.now(timezone.utc).timestamp() - 7 * 86400
            with EXITS_LOG.open() as f:
                for line in f:
                    try:
                        e = json.loads(line)
                        ts = datetime.fromisoformat(
                            e["ts"].replace("Z", "+00:00")).timestamp()
                        if ts >= cutoff:
                            recent_stops += 1
                            realized_pnl_7d += float(e.get("realized_pnl_usd") or 0)
                    except Exception:
                        continue
        except Exception:
            pass

    # Total trades in journal.
    total_trades = 0
    if JOURNAL.exists():
        try:
            with JOURNAL.open() as f:
                total_trades = sum(1 for _ in f)
        except Exception:
            pass

    msg = (
        f"🤖 <b>Bot status:</b> RUNNING\n\n"
        f"<b>NAV:</b> {nav_str}\n"
        f"<b>Open positions:</b> {pos_count}\n"
        f"<b>Total trades logged:</b> {total_trades}\n\n"
        f"<b>Last 7d:</b>\n"
        f"  • Stops fired: {recent_stops}\n"
        f"  • Realized PnL from stops: ${realized_pnl_7d:+.2f}"
    )
    _tg_send(msg)

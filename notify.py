"""
Telegram notifier. Silent no-op if TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
aren't set, so the bot still runs fine without notifications.

Usage:
    from notify import notify
    notify("🎯 Order submitted: PSG wins Ligue 1 @ 0.93, $127")
"""
from __future__ import annotations
import logging
import os
import requests

log = logging.getLogger(__name__)

_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()
_ENABLED = bool(_TOKEN and _CHAT)


def notify(text: str, silent: bool = False) -> None:
    """Fire-and-forget Telegram message. Never raises."""
    if not _ENABLED:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{_TOKEN}/sendMessage",
            json={
                "chat_id": _CHAT,
                "text": text[:4000],  # Telegram's hard limit is 4096 chars
                "parse_mode": "HTML",
                "disable_notification": silent,
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except Exception as e:
        log.warning("Telegram notify failed: %s", e)


def fmt_order(result: dict) -> str:
    """Format an executor result dict as a readable Telegram message."""
    status = result.get("status", "?")
    mode = result.get("mode", "?")
    market = result.get("market", "")
    outcome = result.get("outcome", "")
    price = result.get("price")
    shares = result.get("shares")
    usd = result.get("usd")
    edge = result.get("edge")

    emoji = {
        "submitted": "✅",
        "simulated": "🧪",
        "skipped_by_user": "⏭",
        "error": "❌",
    }.get(status, "ℹ️")

    mode_tag = "LIVE" if mode == "LIVE" else "DRY"

    if status == "error":
        return (
            f"{emoji} <b>[{mode_tag}] Order error</b>\n"
            f"Market: <code>{market}</code>\n"
            f"Error: <code>{str(result.get('error', ''))[:300]}</code>"
        )

    return (
        f"{emoji} <b>[{mode_tag}] {outcome} @ {price}</b>\n"
        f"Market: <code>{market}</code>\n"
        f"Size: {shares} shares · <b>${usd}</b> · edge {edge:+.3f}"
    )

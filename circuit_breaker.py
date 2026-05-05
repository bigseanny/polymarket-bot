"""Daily loss circuit breaker.

If NAV drops more than CB_LOSS_PCT (default 5%) versus the most recent
nav_history.json snapshot from at least 12h ago, trip the breaker:

  1. Write logs/paused.flag with timestamp + reason
  2. Send Telegram alert telling user to investigate
  3. Executor honors the flag and skips placing orders
  4. User replies /resume in Telegram to clear it

The breaker is idempotent — if already tripped, it stays tripped until
manually cleared. Telegram listener polls getUpdates each loop.
"""
from __future__ import annotations
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

ROOT = Path(__file__).parent
PAUSE_FLAG = ROOT / "logs" / "paused.flag"
HISTORY_FILE = ROOT / "logs" / "nav_history.json"
TG_OFFSET_FILE = ROOT / "logs" / "tg_offset.txt"

CB_LOSS_PCT = float(os.getenv("CB_LOSS_PCT", "0.05"))   # 5% drawdown trigger
CB_MIN_LOOKBACK_HOURS = float(os.getenv("CB_MIN_LOOKBACK_HOURS", "12"))
CB_ENABLED = os.getenv("CB_ENABLED", "true").lower() in ("1", "true", "yes")

_TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
_TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()


# ── Pause-flag primitives ──────────────────────────────────────────────
def is_paused() -> bool:
    return PAUSE_FLAG.exists()


def trip(reason: str, current_nav: float, prev_nav: float, prev_label: str) -> None:
    """Write the pause flag and send a Telegram alert."""
    PAUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "tripped_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "current_nav": round(current_nav, 2),
        "prev_nav": round(prev_nav, 2),
        "prev_label": prev_label,
    }
    PAUSE_FLAG.write_text(json.dumps(payload, indent=2))
    log.error("CIRCUIT BREAKER TRIPPED: %s", reason)

    drop = prev_nav - current_nav
    drop_pct = (drop / prev_nav * 100) if prev_nav else 0
    msg = (
        f"🚨 <b>CIRCUIT BREAKER TRIPPED</b>\n\n"
        f"NAV dropped <b>${drop:,.2f}</b> ({drop_pct:.2f}%) since {prev_label}.\n"
        f"  Previous NAV: ${prev_nav:,.2f}\n"
        f"  Current NAV:  ${current_nav:,.2f}\n\n"
        f"⏸  Trading paused. Investigate and reply <code>/resume</code> "
        f"to restart.\n\n"
        f"Reason: <code>{reason}</code>"
    )
    _tg_send(msg)


def clear(source: str = "manual") -> None:
    if PAUSE_FLAG.exists():
        PAUSE_FLAG.unlink()
        log.info("Circuit breaker cleared (%s)", source)
        _tg_send(
            f"✅ <b>Circuit breaker cleared</b> ({source}). Trading resumed."
        )


# ── NAV-based check ────────────────────────────────────────────────────
def _current_nav(funder: str) -> float | None:
    """Cash + position market value for the proxy."""
    try:
        from bankroll import get_usdc_balance
    except Exception as e:
        log.warning("bankroll import failed: %s", e)
        return None

    cash = get_usdc_balance(funder) or 0.0
    try:
        r = requests.get(
            "https://data-api.polymarket.com/positions",
            params={"user": funder.lower(), "limit": 100, "sizeThreshold": 0},
            timeout=15,
        )
        r.raise_for_status()
        positions = r.json() or []
    except Exception as e:
        log.warning("positions fetch failed: %s", e)
        return None
    pos_value = sum(float(p.get("currentValue") or 0) for p in positions)
    return float(cash) + float(pos_value)


def _load_history() -> dict[str, float]:
    if not HISTORY_FILE.exists():
        return {}
    try:
        return json.loads(HISTORY_FILE.read_text())
    except Exception:
        return {}


def check_and_maybe_trip(funder: str) -> bool:
    """Returns True if breaker is currently tripped (newly or already)."""
    if not CB_ENABLED:
        return False
    if is_paused():
        return True   # already tripped, no re-evaluation
    if not funder:
        return False

    nav = _current_nav(funder)
    if nav is None:
        return False

    history = _load_history()
    if not history:
        return False

    # Find the most recent snapshot from at least CB_MIN_LOOKBACK_HOURS ago.
    # nav_history.json is keyed by ISO date strings (YYYY-MM-DD).
    today = datetime.now(timezone.utc).date()
    candidate = None
    for label in sorted(history.keys(), reverse=True):
        try:
            d = datetime.fromisoformat(label).date()
        except Exception:
            continue
        age_hours = (datetime.now(timezone.utc)
                     - datetime.combine(d, datetime.min.time(), timezone.utc)
                     ).total_seconds() / 3600
        if age_hours >= CB_MIN_LOOKBACK_HOURS:
            candidate = (label, float(history[label]))
            break

    if not candidate:
        return False

    prev_label, prev_nav = candidate
    if prev_nav <= 0:
        return False
    drop_pct = (prev_nav - nav) / prev_nav
    if drop_pct >= CB_LOSS_PCT:
        trip(
            reason=f"NAV down {drop_pct*100:.2f}% (threshold {CB_LOSS_PCT*100:.1f}%)",
            current_nav=nav, prev_nav=prev_nav, prev_label=prev_label,
        )
        return True
    return False


# ── Telegram listener for /resume ──────────────────────────────────────
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
    """Check Telegram for new /resume or /status messages from the
    authorized chat. Non-blocking, short timeout. Safe to call every loop.
    """
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
        if text in ("/resume", "/resume@", "resume"):
            clear(source="telegram /resume")
        elif text in ("/status", "status"):
            _send_status()
        elif text in ("/pause", "pause"):
            # Manual pause via Telegram.
            PAUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
            PAUSE_FLAG.write_text(json.dumps({
                "tripped_at": datetime.now(timezone.utc).isoformat(),
                "reason": "manual /pause via telegram",
            }, indent=2))
            _tg_send("⏸ <b>Trading paused</b> (manual). Send /resume to restart.")

    if max_seen != offset:
        _write_offset(max_seen)


def _send_status() -> None:
    state = "PAUSED" if is_paused() else "RUNNING"
    extra = ""
    if is_paused():
        try:
            extra = "\n\n<code>" + PAUSE_FLAG.read_text()[:600] + "</code>"
        except Exception:
            pass
    _tg_send(f"🤖 <b>Bot status:</b> {state}{extra}")

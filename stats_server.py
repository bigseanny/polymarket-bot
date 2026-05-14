"""Lightweight read-only stats endpoint for external monitoring.

Exposes a single authenticated endpoint:
    GET /stats   →  JSON with bot health, NAV, positions, filter activity,
                    recent trades, stop-loss firings, and anomalies.

Auth: bearer token via STATS_TOKEN env var (compared in constant time).
Bind: 0.0.0.0:8080 by default (override with STATS_PORT).

Designed to be queried at most once per minute. All data is read from
files on disk and the same data-api the bot uses — no extra DB needed.
"""
from __future__ import annotations
import hmac
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

ROOT = Path(__file__).parent
LOG_DIR = ROOT / "logs"
BOT_LOG = LOG_DIR / "bot.log"
TRADES_JSONL = LOG_DIR / "trades.jsonl"
STOP_EXITS_JSONL = LOG_DIR / "stop_exits.jsonl"
NAV_HISTORY = LOG_DIR / "nav_history.json"
STATE_FILE = LOG_DIR / "state.json"
PAUSED_FLAG = LOG_DIR / "paused.flag"

PROXY = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").lower()
DATA_API = "https://data-api.polymarket.com"
TOKEN = os.environ.get("STATS_TOKEN", "")
SERVICE_NAME = os.environ.get("STATS_SERVICE_NAME", "polymarket-bot.service")
STRATEGY_MODE = os.environ.get("STRATEGY_MODE", "nearcert").strip().lower()

if not TOKEN or len(TOKEN) < 24:
    log.error("STATS_TOKEN env var is missing or too short (need ≥24 chars). Refusing to start.")
    sys.exit(1)

app = FastAPI(title="Polymarket bot stats", docs_url=None, redoc_url=None, openapi_url=None)


# ── Auth ──────────────────────────────────────────────────────────────
def _check_auth(request: Request) -> None:
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    presented = header[len("Bearer "):].strip()
    if not hmac.compare_digest(presented, TOKEN):
        raise HTTPException(status_code=403, detail="bad token")


# ── Helpers ───────────────────────────────────────────────────────────
def _read_jsonl(path: Path, limit: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        with path.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
        if limit:
            return rows[-limit:]
        return rows
    except Exception as e:
        log.warning("failed reading %s: %s", path, e)
        return []


def _parse_log_ts(line: str) -> datetime | None:
    # Format: "2026-05-11 17:17:11,160 INFO ..."
    m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _tail_log(path: Path, max_bytes: int = 4_000_000) -> list[str]:
    """Read the tail of a (potentially huge) log file."""
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # discard partial line
            data = f.read().decode("utf-8", errors="replace")
        return data.splitlines()
    except Exception as e:
        log.warning("failed tailing %s: %s", path, e)
        return []


def _service_status() -> dict:
    """Query systemd for service health."""
    try:
        out = subprocess.run(
            ["systemctl", "show", SERVICE_NAME,
             "--property=ActiveState,SubState,MainPID,ActiveEnterTimestamp,NRestarts"],
            capture_output=True, text=True, timeout=5,
        )
        props: dict[str, str] = {}
        for line in out.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k] = v
        return {
            "active_state": props.get("ActiveState"),
            "sub_state": props.get("SubState"),
            "main_pid": props.get("MainPID"),
            "active_since": props.get("ActiveEnterTimestamp"),
            "restarts": props.get("NRestarts"),
        }
    except Exception as e:
        return {"error": str(e)}


def _scan_health(lines: list[str]) -> dict:
    """Find the most recent scan and how long ago it was."""
    last_scan_ts: datetime | None = None
    last_pull: str | None = None
    last_complete: str | None = None
    for line in reversed(lines):
        if "Scan complete" in line and last_complete is None:
            last_complete = line
            last_scan_ts = _parse_log_ts(line)
        if "unique markets from Gamma" in line and last_pull is None:
            last_pull = line
        if last_complete and last_pull:
            break
    age_seconds = None
    if last_scan_ts:
        age_seconds = (datetime.now(timezone.utc) - last_scan_ts).total_seconds()
    return {
        "last_scan_complete": last_complete,
        "last_pull": last_pull,
        "last_scan_age_seconds": age_seconds,
    }


def _filter_activity(lines: list[str], since: datetime | None = None) -> dict:
    """Count filter drops and other strategy events."""
    counts: Counter[str] = Counter()
    events_dedup: Counter[str] = Counter()
    btc_buffer: list[str] = []
    nav_caps: list[str] = []
    edge_floor: list[str] = []

    pat_filter = re.compile(r"Filter #(\d+\w?) drop")
    pat_event = re.compile(r"event '([^']+)' already at cap")
    pat_btc = re.compile(r"BTC buffer.*spot \$([\d,\.]+).*strike \$([\d,\.]+)")
    pat_nav = re.compile(r"NAV cap.*\$([\d\.]+).*\$([\d\.]+).*\(\d+%")
    pat_edge = re.compile(r"category=(\w+) edge=([\d\.]+) below floor ([\d\.]+)")

    for line in lines:
        ts = _parse_log_ts(line)
        if since and ts and ts < since:
            continue
        if m := pat_filter.search(line):
            counts[f"filter_{m.group(1)}"] += 1
        if m := pat_event.search(line):
            events_dedup[m.group(1)] += 1
        if m := pat_btc.search(line):
            counts["btc_buffer"] += 1
            if len(btc_buffer) < 10:
                btc_buffer.append(line.strip()[-200:])
        if m := pat_nav.search(line):
            counts["nav_cap"] += 1
            if len(nav_caps) < 10:
                nav_caps.append(line.strip()[-200:])
        if m := pat_edge.search(line):
            counts["edge_floor"] += 1
            if len(edge_floor) < 10:
                edge_floor.append(line.strip()[-200:])

    return {
        "counts": dict(counts),
        "top_capped_events": events_dedup.most_common(10),
        "btc_buffer_samples": btc_buffer,
        "nav_cap_samples": nav_caps,
        "edge_floor_samples": edge_floor,
    }


def _errors_recent(lines: list[str], since: datetime | None) -> list[str]:
    out: list[str] = []
    for line in lines[-2000:]:  # only scan recent
        ts = _parse_log_ts(line)
        if since and ts and ts < since:
            continue
        if re.search(r"\b(ERROR|CRITICAL|Traceback|Exception)\b", line):
            out.append(line.strip()[-300:])
    return out[-30:]  # cap to most recent 30


def _candidate_drought(lines: list[str]) -> dict:
    """Time since last >0 qualifying candidate."""
    pat = re.compile(r"Scan complete:\s*(\d+) qualifying candidates")
    last_nonzero: datetime | None = None
    last_scan: datetime | None = None
    nonzero_count_24h = 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    for line in lines:
        m = pat.search(line)
        if not m:
            continue
        ts = _parse_log_ts(line)
        if not ts:
            continue
        last_scan = ts
        n = int(m.group(1))
        if n > 0:
            last_nonzero = ts
            if ts >= cutoff:
                nonzero_count_24h += n
    drought_hours = None
    if last_nonzero:
        drought_hours = (datetime.now(timezone.utc) - last_nonzero).total_seconds() / 3600
    return {
        "last_qualifying_candidate_ts": last_nonzero.isoformat() if last_nonzero else None,
        "drought_hours": drought_hours,
        "qualifying_candidates_last_24h": nonzero_count_24h,
        "last_scan_ts": last_scan.isoformat() if last_scan else None,
    }


def _live_positions() -> dict:
    """Pull positions + cash from public data-api."""
    if not PROXY:
        return {"error": "POLYMARKET_FUNDER_ADDRESS not set"}
    try:
        r = requests.get(
            f"{DATA_API}/positions",
            params={"user": PROXY, "limit": 100, "sizeThreshold": 0},
            timeout=10,
        )
        r.raise_for_status()
        positions = r.json() or []
    except Exception as e:
        return {"error": f"data-api fetch failed: {e}"}

    pos_value = sum(p.get("currentValue", 0) or 0 for p in positions)
    invested = sum(p.get("initialValue", 0) or 0 for p in positions)
    unrealized = sum(p.get("cashPnl", 0) or 0 for p in positions)

    # Concentration / event grouping
    event_counts: Counter[str] = Counter()
    for p in positions:
        key = p.get("eventSlug") or p.get("conditionId") or "?"
        event_counts[key] += 1

    top = sorted(positions, key=lambda p: p.get("currentValue", 0) or 0, reverse=True)[:10]
    top_summaries = [
        {
            "title": (p.get("title") or "")[:80],
            "outcome": p.get("outcome"),
            "shares": p.get("size"),
            "cur_price": p.get("curPrice"),
            "current_value": p.get("currentValue"),
            "pnl": p.get("cashPnl"),
            "event_slug": p.get("eventSlug"),
        }
        for p in top
    ]
    return {
        "open_count": len(positions),
        "position_value": round(pos_value, 2),
        "invested": round(invested, 2),
        "unrealized_pnl": round(unrealized, 2),
        "top_10_positions": top_summaries,
        "events_with_multi_positions": [
            {"event_slug": k, "n": v} for k, v in event_counts.items() if v >= 2
        ],
    }


def _live_cash() -> float | None:
    """Try to read USDC balance via bankroll helper if available."""
    try:
        from bankroll import get_usdc_balance  # type: ignore
        return float(get_usdc_balance(PROXY) or 0.0)
    except Exception as e:
        log.warning("bankroll fetch failed: %s", e)
        return None


def _nav_history() -> dict:
    if not NAV_HISTORY.exists():
        return {}
    try:
        return json.loads(NAV_HISTORY.read_text())
    except Exception:
        return {}


def _recent_trades(hours: int = 24) -> dict:
    """Trades submitted in the last N hours."""
    rows = _read_jsonl(TRADES_JSONL)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out: list[dict] = []
    for r in rows:
        ts_str = r.get("ts", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            continue
        if ts < cutoff:
            continue
        out.append({
            "ts": ts_str,
            "market": r.get("market"),
            "outcome": r.get("outcome"),
            "entry_price": r.get("entry_price"),
            "shares": r.get("shares"),
            "cost_usd": r.get("cost_usd"),
            "edge_estimated": r.get("edge_estimated"),
            "days_to_resolution": r.get("days_to_resolution"),
            "category": r.get("category"),
            "outcome_resolved": r.get("outcome_resolved"),
            "pnl_usd": r.get("pnl_usd"),
        })

    # Aggregate stats over the full journal (not just last N hours) for win rate
    closed = [r for r in rows if r.get("outcome_resolved") is not None]
    wins = [r for r in closed if (r.get("pnl_usd") or 0) > 0]
    losses = [r for r in closed if (r.get("pnl_usd") or 0) < 0]
    biggest_losses = sorted(closed, key=lambda r: r.get("pnl_usd") or 0)[:5]
    return {
        "submitted_last_n_hours": out,
        "submitted_count_last_n_hours": len(out),
        "n_hours": hours,
        "lifetime_total_logged": len(rows),
        "lifetime_closed": len(closed),
        "lifetime_wins": len(wins),
        "lifetime_losses": len(losses),
        "lifetime_win_rate": round(len(wins) / len(closed), 4) if closed else None,
        "lifetime_realized_pnl": round(sum((r.get("pnl_usd") or 0) for r in closed), 2),
        "biggest_5_losses": [
            {
                "market": r.get("market"),
                "pnl_usd": r.get("pnl_usd"),
                "category": r.get("category"),
                "ts": r.get("ts"),
            }
            for r in biggest_losses if (r.get("pnl_usd") or 0) < 0
        ],
    }


def _stop_exits(hours: int = 168) -> dict:
    """Stop-loss firings in the last N hours (default 1 week)."""
    rows = _read_jsonl(STOP_EXITS_JSONL)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    recent: list[dict] = []
    for r in rows:
        ts_str = r.get("ts", "") or r.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            continue
        if ts < cutoff:
            continue
        recent.append(r)
    return {
        "n_hours": hours,
        "lifetime_total": len(rows),
        "recent_count": len(recent),
        "recent_exits": recent[-20:],
    }


def _paused() -> bool:
    return PAUSED_FLAG.exists()


# ── Endpoints ─────────────────────────────────────────────────────────
@app.get("/healthz")
def healthz() -> dict:
    """Unauthenticated liveness probe."""
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/stats")
def stats(request: Request) -> JSONResponse:
    _check_auth(request)
    t0 = time.time()
    now = datetime.now(timezone.utc)
    since_24h = now - timedelta(hours=24)

    lines = _tail_log(BOT_LOG)
    service = _service_status()
    scan = _scan_health(lines)
    filters_24h = _filter_activity(lines, since=since_24h)
    filters_all = _filter_activity(lines, since=None)
    errors_24h = _errors_recent(lines, since=since_24h)
    drought = _candidate_drought(lines)
    positions = _live_positions()
    cash = _live_cash()
    nav_hist = _nav_history()
    trades = _recent_trades(hours=24)
    stops = _stop_exits(hours=168)

    nav = None
    if cash is not None and "position_value" in positions:
        nav = round(cash + positions["position_value"], 2)

    # Daily delta from nav history
    nav_delta_24h = None
    nav_delta_pct = None
    if nav is not None and nav_hist:
        prev_key = sorted(nav_hist.keys())[-1]
        prev_nav = nav_hist.get(prev_key)
        if prev_nav:
            nav_delta_24h = round(nav - prev_nav, 2)
            nav_delta_pct = round((nav - prev_nav) / prev_nav * 100, 2)

    # Anomaly flags — derived signals to drive monitoring alerts
    anomalies: list[str] = []
    if service.get("active_state") != "active":
        anomalies.append(f"service_not_active: {service.get('active_state')}/{service.get('sub_state')}")
    if scan.get("last_scan_age_seconds") is not None and scan["last_scan_age_seconds"] > 300:
        anomalies.append(f"stale_scans: last {scan['last_scan_age_seconds']:.0f}s ago")
    if drought.get("drought_hours") is not None and drought["drought_hours"] > 12:
        anomalies.append(f"candidate_drought_{drought['drought_hours']:.1f}h")
    if errors_24h:
        anomalies.append(f"errors_24h: {len(errors_24h)}")
    if _paused():
        anomalies.append("bot_paused_flag_present")
    if nav_delta_pct is not None and nav_delta_pct < -5:
        anomalies.append(f"nav_drop_24h: {nav_delta_pct}%")
    if isinstance(positions, dict):
        for ev in positions.get("events_with_multi_positions", []):
            if ev.get("n", 0) > 2:
                anomalies.append(f"event_over_cap: {ev['event_slug']} n={ev['n']}")
    for loss in trades.get("biggest_5_losses", []):
        pnl = loss.get("pnl_usd") or 0
        if pnl < -10 and loss.get("ts", "") >= since_24h.isoformat():
            anomalies.append(f"new_loser_over_10: {loss.get('market')} {pnl}")

    body = {
        "timestamp": now.isoformat(),
        "elapsed_s": round(time.time() - t0, 3),
        "strategy_mode": STRATEGY_MODE,
        "service_name": SERVICE_NAME,
        "service": service,
        "paused": _paused(),
        "scan": scan,
        "candidate_drought": drought,
        "nav": {
            "current": nav,
            "cash": cash,
            "positions_value": positions.get("position_value") if isinstance(positions, dict) else None,
            "unrealized_pnl": positions.get("unrealized_pnl") if isinstance(positions, dict) else None,
            "delta_24h_usd": nav_delta_24h,
            "delta_24h_pct": nav_delta_pct,
            "history": nav_hist,
        },
        "positions": positions,
        "filters_24h": filters_24h,
        "filters_lifetime_logbuffer": filters_all,
        "trades": trades,
        "stop_exits": stops,
        "errors_recent": errors_24h,
        "anomalies": anomalies,
    }
    return JSONResponse(body)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("STATS_PORT", "8080"))
    log.info("Starting stats server on 0.0.0.0:%d (service=%s, strategy=%s)", port, SERVICE_NAME, STRATEGY_MODE)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")

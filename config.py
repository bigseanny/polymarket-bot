"""
Configuration for the Polymarket near-certainty bot.

Strategy: Hunt YES/NO outcomes priced ≤ 0.95 with min 4¢ edge after a 1¢
resolution-risk haircut. Spread capital using fractional-Kelly sizing.

All thresholds are tunable via environment variables (see .env.example).
"""
from __future__ import annotations
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _b(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).lower() in ("1", "true", "yes", "y")


@dataclass(frozen=True)
class Config:
    # ── API endpoints ────────────────────────────────────────────────────
    GAMMA_API: str = "https://gamma-api.polymarket.com"
    CLOB_API: str = "https://clob.polymarket.com"
    DATA_API: str = "https://data-api.polymarket.com"
    CHAIN_ID: int = 137  # Polygon mainnet

    # ── Wallet / auth (REQUIRED for live mode) ───────────────────────────
    PRIVATE_KEY: str = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    FUNDER_ADDRESS: str = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
    # 0 = EOA / MetaMask / hardware (default).
    # 1 = Email / Magic-link wallet (most Polymarket UI users → use 1).
    SIGNATURE_TYPE: int = _i("POLYMARKET_SIGNATURE_TYPE", 1)

    # ── Strategy thresholds ──────────────────────────────────────────────
    MAX_ASK: float = _f("MAX_ASK", 0.95)            # only buy if best ask ≤ this
    MIN_BID: float = _f("MIN_BID", 0.90)             # required to prove consensus (anti long-shot filter)
    MAX_SPREAD: float = _f("MAX_SPREAD", 0.05)       # widest book we'll trust
    HAIRCUT: float = _f("HAIRCUT", 0.01)             # subtract from "true" prob
    MIN_EDGE: float = _f("MIN_EDGE", 0.04)           # require (1-haircut-ask) ≥ this
    MIN_VOLUME_USD: float = _f("MIN_VOLUME_USD", 50_000)
    MIN_LIQUIDITY_USD: float = _f("MIN_LIQUIDITY_USD", 5_000)
    MAX_DAYS_TO_RESOLUTION: float = _f("MAX_DAYS_TO_RESOLUTION", 60)
    MIN_DAYS_TO_RESOLUTION: float = _f("MIN_DAYS_TO_RESOLUTION", 0.5)

    # ── Sizing (fractional Kelly) ────────────────────────────────────────
    BANKROLL_USD: float = _f("BANKROLL_USD", 1_000)         # total capital to deploy
    KELLY_FRACTION: float = _f("KELLY_FRACTION", 0.25)      # 0.25 = quarter-Kelly (recommended)
    MAX_POSITIONS: int = _i("MAX_POSITIONS", 20)            # cap concurrent bets
    MAX_PER_MARKET_USD: float = _f("MAX_PER_MARKET_USD", 200)  # hard cap per market
    MAX_PCT_OF_BOOK: float = _f("MAX_PCT_OF_BOOK", 0.20)    # don't eat >20% of best ask depth
    MIN_ORDER_USD: float = _f("MIN_ORDER_USD", 5)           # CLOB min order size

    # ── Safety / mode ────────────────────────────────────────────────────
    DRY_RUN: bool = _b("DRY_RUN", True)              # default safe; flip to False for live
    REQUIRE_CONFIRM: bool = _b("REQUIRE_CONFIRM", True)  # interactive y/n before each order
    POLL_SECONDS: int = _i("POLL_SECONDS", 60)       # scan interval
    CANCEL_UNFILLED_AFTER_SECONDS: int = _i("CANCEL_UNFILLED_AFTER_SECONDS", 300)
    LOG_DIR: str = os.getenv("LOG_DIR", "logs")
    STATE_FILE: str = os.getenv("STATE_FILE", "logs/state.json")


CFG = Config()

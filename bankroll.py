"""
Auto-bankroll: reads the live USDC.e balance of the Polymarket proxy wallet
directly from a Polygon RPC each cycle, and uses it as the working bankroll.

This means the bot automatically adapts when:
  * You deposit more USDC.e into the proxy
  * A winning position gets redeemed (USDC.e returns to the proxy)
  * Orders fill (USDC.e is locked, balance drops)

No `.env` edits needed after initial setup.

A small `RESERVE_USD` is held back so we never try to spend the full balance
(leaves headroom for fees and rounding).
"""
from __future__ import annotations
import logging
import os
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

# USDC.e (bridged) on Polygon — this is what Polymarket uses.
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Public Polygon RPCs — we try them in order until one responds.
# No API key required for light usage (a few calls per minute).
_RPCS = [
    "https://polygon-rpc.com",
    "https://polygon.llamarpc.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon-bor-rpc.publicnode.com",
]

# ERC-20 balanceOf(address) selector = keccak("balanceOf(address)")[:4]
_BALANCE_OF_SELECTOR = "0x70a08231"

# How long to cache a successful balance read before re-querying.
_CACHE_TTL_SECONDS = 20

# Hold back a small USDC reserve so we never attempt to deploy the exact
# full balance (avoids fee/rounding edge cases).
DEFAULT_RESERVE_USD = float(os.getenv("BANKROLL_RESERVE_USD", "2"))


_cache: dict = {"ts": 0.0, "value": None, "addr": ""}


def _eth_call(rpc_url: str, to: str, data: str, timeout: float = 6.0) -> Optional[str]:
    """Raw JSON-RPC eth_call. Returns the hex result or None on failure."""
    try:
        r = requests.post(
            rpc_url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_call",
                "params": [{"to": to, "data": data}, "latest"],
            },
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            log.debug("RPC %s error: %s", rpc_url, data["error"])
            return None
        return data.get("result")
    except Exception as e:
        log.debug("RPC %s failed: %s", rpc_url, e)
        return None


def get_usdc_balance(address: str) -> Optional[float]:
    """Return the USDC.e balance of `address` on Polygon, in dollars.

    Returns None if all RPCs fail. Result is cached for _CACHE_TTL_SECONDS
    to avoid hammering free RPCs on every sizing call.
    """
    if not address:
        return None
    address = address.lower()
    now = time.time()
    if (
        _cache["addr"] == address
        and _cache["value"] is not None
        and (now - _cache["ts"]) < _CACHE_TTL_SECONDS
    ):
        return _cache["value"]

    # Pad the 20-byte address to 32 bytes for ABI encoding.
    padded = address.replace("0x", "").rjust(64, "0")
    call_data = _BALANCE_OF_SELECTOR + padded

    for rpc in _RPCS:
        result = _eth_call(rpc, USDC_E_ADDRESS, call_data)
        if result and result.startswith("0x"):
            try:
                raw = int(result, 16)
                # USDC.e has 6 decimals.
                balance_usd = raw / 1_000_000
                _cache.update(ts=now, value=balance_usd, addr=address)
                log.debug("USDC.e balance of %s = $%.2f (via %s)",
                          address, balance_usd, rpc)
                return balance_usd
            except ValueError:
                continue
    log.warning("All Polygon RPCs failed — could not read USDC.e balance")
    return None


def effective_bankroll(proxy_address: str, fallback: float,
                       reserve: float = DEFAULT_RESERVE_USD) -> float:
    """Compute the bankroll to use this cycle.

    Returns `balance - reserve` if the on-chain read succeeds (floored at 0),
    otherwise falls back to the static .env value. The reserve ensures the bot
    never tries to deploy the last few cents (fees/rounding).
    """
    balance = get_usdc_balance(proxy_address)
    if balance is None:
        log.info("Auto-bankroll: RPC unreachable, using static BANKROLL_USD=%.2f",
                 fallback)
        return fallback
    effective = max(0.0, balance - reserve)
    log.info("Auto-bankroll: proxy USDC.e balance=$%.2f, reserve=$%.2f, "
             "deployable=$%.2f", balance, reserve, effective)
    return effective

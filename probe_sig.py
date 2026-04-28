"""Probe which signature_type Polymarket accepts for this wallet.

Posts a tiny $1 GTC buy order far below market (price=0.01) on a liquid market
under each signature_type. The server's error message tells us which sig is valid:
  - "invalid signature"  -> sig_type is WRONG
  - "not enough balance" -> sig is VALID, but funds aren't in this proxy
  - success              -> sig is VALID and funds are accessible

Then cancels any orders that happened to land.
"""
import os, sys, traceback
from dotenv import load_dotenv
load_dotenv()

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import OrderArgs, OrderType
from py_clob_client_v2.order_builder.constants import BUY
from py_clob_client_v2.constants import POLYGON

PK = os.environ["POLYMARKET_PRIVATE_KEY"]
FUNDER = os.environ["POLYMARKET_FUNDER_ADDRESS"]

# Pick a very liquid YES token that's been around a while — the Bitcoin-above-X
# markets are always liquid. We'll look it up dynamically to avoid stale IDs.
import httpx
r = httpx.get("https://gamma-api.polymarket.com/markets",
              params={"active": "true", "closed": "false", "limit": 50,
                      "order": "volume", "ascending": "false"}, timeout=20)
mkts = r.json()
token_id = None
for m in mkts:
    toks = m.get("clobTokenIds")
    if toks:
        import json as _j
        toks = _j.loads(toks) if isinstance(toks, str) else toks
        if toks and len(toks) >= 1:
            token_id = toks[0]
            print(f"Probing with market: {m.get('question','?')[:60]}")
            print(f"  token_id={token_id}")
            break
assert token_id, "No liquid market found"

for sig in (1, 2):
    print(f"\n{'='*70}\nSIGNATURE_TYPE = {sig}\n{'='*70}")
    try:
        c = ClobClient("https://clob.polymarket.com", key=PK, chain_id=POLYGON,
                       signature_type=sig, funder=FUNDER)
        creds = c.create_or_derive_api_key()
        c.set_api_creds(creds)
        print(f"  api_key={creds.api_key[:12]}...")

        # Tiny bait order: $1 at price 0.01 = 100 shares. Far below market so it
        # won't fill, lets us read the server's validation response.
        args = OrderArgs(price=0.01, size=100.0, side=BUY, token_id=token_id)
        signed = c.create_order(args)
        resp = c.post_order(signed, OrderType.GTC)
        print(f"  RESULT: {resp}")
        # Cancel immediately if it landed
        if resp.get("success") and resp.get("orderID"):
            c.cancel_order(order_id=resp["orderID"])
            print(f"  (cancelled)")
    except Exception as e:
        msg = str(e)
        print(f"  ERROR: {msg}")
        if "invalid signature" in msg:
            print(f"  -> sig_type={sig} is INVALID for this wallet")
        elif "not enough balance" in msg or "allowance" in msg:
            print(f"  -> sig_type={sig} is VALID (balance quoted means sig passed)")

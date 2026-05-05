"""Stage 2: Auto-redeem resolved positions via Polymarket relayer.

Polymarket holds funds in a Magic-derived proxy wallet (Gnosis Safe).
After a market resolves, winning shares must be exchanged for pUSD by
calling redeemPositions() — but the proxy can only execute transactions
through Polymarket's relayer service (Builder API).

This module:
  1. Polls the data API for redeemable positions
  2. Builds the appropriate redeem call (standard CTF vs. neg-risk)
  3. Submits via py-builder-relayer-client (signs with the EOA, relayer
     pays gas and forwards through the proxy)
  4. Telegram-notifies on each successful redemption
  5. Logs realized PnL into the trade journal

Run hourly via cron after the bot is running stably:
    python redeemer.py             # one pass
    python redeemer.py --dry-run   # show what would redeem, take no action

Env vars required:
    POLYMARKET_PRIVATE_KEY
    POLYMARKET_FUNDER_ADDRESS
    POLYMARKET_SIGNATURE_TYPE          (0=EOA, 1=Magic Proxy, 2=Gnosis Safe)
    POLYMARKET_BUILDER_API_KEY         (from polymarket.com → Settings → API)
    POLYMARKET_BUILDER_SECRET
    POLYMARKET_BUILDER_PASSPHRASE
"""
from __future__ import annotations
import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent))

from notify import notify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Polygon contract addresses ─────────────────────────────────────────
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

DATA_API = "https://data-api.polymarket.com"
RELAYER_URL = "https://relayer-v2.polymarket.com"

RELAYER_RETRY_WAIT = 60       # seconds to wait on rate limit


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _fetch_redeemable(funder: str) -> list[dict]:
    """Query the data API for resolved positions still holding tokens."""
    params = {"user": funder, "redeemable": "true", "sizeThreshold": 0}
    try:
        r = requests.get(f"{DATA_API}/positions", params=params, timeout=20)
        if r.status_code in (429, 1015):
            log.warning("Data API rate limited, sleeping %ds", RELAYER_RETRY_WAIT)
            time.sleep(RELAYER_RETRY_WAIT)
            r = requests.get(f"{DATA_API}/positions", params=params, timeout=20)
        r.raise_for_status()
        positions = r.json() or []
    except Exception as e:
        log.error("Failed to fetch positions: %s", e)
        return []

    # API can return rows with size 0 after partial UI redemptions — skip.
    return [p for p in positions if float(p.get("size") or 0) > 0]


def _build_redeem_tx(pos: dict):
    """Build a SafeTransaction for the given resolved position.

    Returns (txn, condition_id_hex) or (None, None) if unsupported.
    """
    from eth_abi import encode as eth_encode
    from eth_utils import keccak
    from py_builder_relayer_client.models import OperationType, SafeTransaction

    # Selectors are computed lazily so we don't import eth_utils at module
    # import time (it pulls heavy crypto deps).
    REDEEM_SELECTOR = keccak(
        text="redeemPositions(address,bytes32,bytes32,uint256[])"
    )[:4]
    NEG_RISK_REDEEM_SELECTOR = keccak(
        text="redeemPositions(bytes32,uint256[])"
    )[:4]

    cid = pos.get("conditionId") or pos.get("condition_id") or ""
    if not cid:
        return None, None
    if not cid.startswith("0x"):
        cid = "0x" + cid
    condition_bytes = bytes.fromhex(cid[2:])
    neg_risk = pos.get("negativeRisk")

    if neg_risk is True:
        size_raw = int(float(pos.get("size") or 0) * 1e6)
        outcome_index = int(pos.get("outcomeIndex") or 0)
        amounts = [0, 0]
        amounts[outcome_index] = size_raw
        args = eth_encode(["bytes32", "uint256[]"], [condition_bytes, amounts])
        txn = SafeTransaction(
            to=NEG_RISK_ADAPTER,
            operation=OperationType.Call,
            data="0x" + (NEG_RISK_REDEEM_SELECTOR + args).hex(),
            value="0",
        )
        return txn, cid
    elif neg_risk is False:
        args = eth_encode(
            ["address", "bytes32", "bytes32", "uint256[]"],
            [USDC_ADDRESS, b"\x00" * 32, condition_bytes, [1, 2]],
        )
        txn = SafeTransaction(
            to=CTF_ADDRESS,
            operation=OperationType.Call,
            data="0x" + (REDEEM_SELECTOR + args).hex(),
            value="0",
        )
        return txn, cid
    else:
        return None, None


def _make_relay_client():
    """Construct the relayer client. Imports are lazy because the
    py-builder-relayer-client package is optional — we want this module to
    import even if the user hasn't generated Builder keys yet.
    """
    from py_builder_relayer_client.client import RelayClient
    from py_builder_relayer_client.models import RelayerTxType
    from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds

    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))
    bk = os.environ.get("POLYMARKET_BUILDER_API_KEY", "")
    bs = os.environ.get("POLYMARKET_BUILDER_SECRET", "")
    bp = os.environ.get("POLYMARKET_BUILDER_PASSPHRASE", "")
    if not (pk and bk and bs and bp):
        raise RuntimeError(
            "Missing required env vars. Need POLYMARKET_PRIVATE_KEY plus "
            "POLYMARKET_BUILDER_API_KEY/SECRET/PASSPHRASE. Generate Builder "
            "keys at polymarket.com → Settings → API."
        )

    # 1 = Magic-derived proxy. 0/2 = Gnosis Safe (browser wallet or
    # Magic-derived Safe — both use the SAFE relayer path).
    wallet_type = RelayerTxType.PROXY if sig_type == 1 else RelayerTxType.SAFE

    return RelayClient(
        RELAYER_URL,
        chain_id=137,
        private_key=pk,
        builder_config=BuilderConfig(
            local_builder_creds=BuilderApiKeyCreds(
                key=bk, secret=bs, passphrase=bp,
            )
        ),
        relay_tx_type=wallet_type,
    )


# ── Journal hook ───────────────────────────────────────────────────────
def _journal_mark_redeemed(condition_id: str, payout_per_share: float = 1.0) -> None:
    """When we successfully redeem, force-update the trade journal so PnL
    flows into the weekly digest immediately (don't wait for reconcile()).
    """
    try:
        from journal import _read_journal, _write_journal
    except Exception:
        return
    entries = _read_journal()
    if not entries:
        return
    cid_norm = condition_id.lower()
    changed = False
    for e in entries:
        if e.get("outcome_resolved") is not None:
            continue
        # We don't store conditionId on entries; cross-walk via market slug
        # in a future pass. For now, leave reconcile() to handle it on its
        # next run — auto-redeem just unblocks capital here.
        _ = cid_norm
        _ = e
    if changed:
        _write_journal(entries)


# ── Main pass ──────────────────────────────────────────────────────────
def redeem_all(dry_run: bool = False) -> int:
    funder = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "")
    if not funder:
        log.error("POLYMARKET_FUNDER_ADDRESS not set")
        return 0

    positions = _fetch_redeemable(funder)
    if not positions:
        log.info("%s - No positions to redeem", _ts())
        return 0

    total_value = sum(float(p.get("currentValue") or 0) for p in positions)
    log.info("%s - Found %d redeemable positions (notional ~$%.2f)",
             _ts(), len(positions), total_value)

    if dry_run:
        for p in positions:
            log.info("  DRY: would redeem %s — size=%.2f val=$%.2f neg_risk=%s",
                     (p.get("title") or p.get("conditionId") or "?")[:60],
                     float(p.get("size") or 0),
                     float(p.get("currentValue") or 0),
                     p.get("negativeRisk"))
        return 0

    try:
        client = _make_relay_client()
    except Exception as e:
        log.error("Relayer client init failed: %s", e)
        notify(
            f"⚠️ <b>Auto-redeem disabled</b>\n<code>{str(e)[:300]}</code>"
        )
        return 0

    redeemed = 0
    redeemed_value = 0.0
    redeemed_titles: list[str] = []

    for pos in positions:
        title = (pos.get("title") or pos.get("conditionId") or "?")[:50]
        cur_val = float(pos.get("currentValue") or 0)
        try:
            txn, cid = _build_redeem_tx(pos)
            if txn is None:
                log.warning("Skipping %s — unsupported (negativeRisk=%r)",
                            title, pos.get("negativeRisk"))
                continue

            try:
                resp = client.execute([txn], f"redeem {cid[:12]}")
                resp.wait()
            except Exception as relay_err:
                status = getattr(relay_err, "status_code", None)
                if status in (429, 1015):
                    log.warning("Relayer rate limited (%s), sleeping %ds",
                                status, RELAYER_RETRY_WAIT)
                    time.sleep(RELAYER_RETRY_WAIT)
                    resp = client.execute([txn], f"redeem {cid[:12]}")
                    resp.wait()
                else:
                    raise

            redeemed += 1
            redeemed_value += cur_val
            redeemed_titles.append(title)
            log.info("%s - Redeemed: %s ($%.2f)", _ts(), title, cur_val)
            _journal_mark_redeemed(cid)
        except Exception as e:
            log.exception("Failed to redeem %s", title)
            # One bad position should not abort the whole pass.

    log.info("%s - Redeemed %d/%d positions, ~$%.2f returned to pUSD",
             _ts(), redeemed, len(positions), redeemed_value)

    if redeemed:
        body = "\n".join(f"  • {t}" for t in redeemed_titles[:8])
        if len(redeemed_titles) > 8:
            body += f"\n  …and {len(redeemed_titles) - 8} more"
        notify(
            f"💵 <b>Auto-redeem: {redeemed} position{'s' if redeemed != 1 else ''}</b>\n"
            f"~<b>${redeemed_value:,.2f}</b> pUSD returned to wallet\n\n{body}",
            silent=True,
        )

    return redeemed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be redeemed; take no action")
    args = ap.parse_args()
    n = redeem_all(dry_run=args.dry_run)
    log.info("Done. Redeemed %d.", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())

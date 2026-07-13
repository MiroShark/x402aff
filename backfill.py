#!/usr/bin/env python3
"""Daily on-chain backfill — recover the facilitator wallet code ("w").

Everything a payment can know at request time (``a``, ``s``) is already stored by
the server. The one field that CAN'T be known then is ``w`` (the facilitator's
wallet code): the facilitator only writes it into the settlement calldata at
settle time, after the request returned. This job reads that calldata over a Base
RPC, decodes the ERC-8021 suffix, and fills ``w`` in (gap-filling ``a``/``s`` too
if either was missed). Run it on a daily cron.

    BACKFILL_RPC_URL=https://mainnet.base.org python backfill.py

Idempotent and safe to run repeatedly. Scope is bounded to a recent window
(BACKFILL_SINCE_HOURS, default 48) so it stays cheap and any settlement that
carries no ``w`` (e.g. a non-CDP facilitator) simply ages out instead of being
retried forever.
"""
from __future__ import annotations

import os
import re

import requests

import tracking
from builder_code import (
    normalize_builder_code,
    normalize_service_codes,
    parse_builder_code_suffix,
)

# EVM tx hash: 0x + 64 hex. Solana settlements use base58 hashes and never carry
# an ERC-8021 suffix, so anything that isn't an EVM hash is skipped.
_EVM_TX = re.compile(r"^0x[0-9a-fA-F]{64}$")


def _fetch_calldata(rpc_url: str, tx_hash: str, timeout: float = 20.0) -> str | None:
    """Return a tx's input calldata via eth_getTransactionByHash, or None."""
    try:
        resp = requests.post(
            rpc_url,
            json={"jsonrpc": "2.0", "id": 1,
                  "method": "eth_getTransactionByHash", "params": [tx_hash]},
            timeout=timeout,
        )
        resp.raise_for_status()
        result = resp.json().get("result")
        return result.get("input") if result else None
    except Exception as exc:
        print(f"  RPC fetch failed for {tx_hash}: {exc}")
        return None


def main() -> int:
    rpc_url = (os.environ.get("BACKFILL_RPC_URL") or "https://mainnet.base.org").strip()
    since_hours = int(os.environ.get("BACKFILL_SINCE_HOURS") or "48")
    limit = int(os.environ.get("BACKFILL_LIMIT") or "500")

    conn = tracking.connect()
    rows = tracking.rows_needing_backfill(conn, since_hours=since_hours, limit=limit)
    print(f"candidates: {len(rows)} payment(s) with a settle tx and no w "
          f"(window={since_hours}h, rpc={rpc_url})")

    updated = skipped = no_suffix = 0
    for row in rows:
        tx = (row["settle_tx_hash"] or "").strip()
        if not _EVM_TX.match(tx):
            skipped += 1  # Solana / malformed — no EVM calldata to read
            continue

        calldata = _fetch_calldata(rpc_url, tx)
        if not calldata:
            skipped += 1
            continue

        parsed = parse_builder_code_suffix(calldata)
        if not parsed:
            no_suffix += 1  # no attribution landed; ages out of the window
            continue

        # w is authoritative + settle-only, so always take it. a/s are gap-filled
        # only when the request-time capture missed them.
        fields: dict[str, str] = {}
        w = normalize_builder_code(parsed.get("w"))
        if w:
            fields["w"] = w
        if not row["builder_code_a"]:
            if a := normalize_builder_code(parsed.get("a")):
                fields["a"] = a
        if not row["builder_code_s"]:
            if s := normalize_service_codes(parsed.get("s")):
                fields["s"] = s

        if not fields:
            no_suffix += 1
            continue

        tracking.set_builder_codes(conn, row["id"], **fields)
        updated += 1
        print(f"  backfilled {row['id']}: {fields}")

    print(f"done: {updated} updated, {no_suffix} no-attribution, {skipped} skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

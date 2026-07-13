#!/usr/bin/env python3
"""Backfill on-chain builder codes via the CDP SQL API (no RPC, no CBOR parsing).

Same job as backfill.py, but instead of fetching raw calldata over a Base RPC and
hand-decoding the ERC-8021 suffix, it asks CDP's already-decoded
`base.transaction_attributions` table for the codes on each settlement tx. One
batched query covers every pending run.

    CDP_API_KEY_ID=... CDP_API_KEY_SECRET=... python backfill_sql.py   # (or CDP_JWT=...)

Trade-off vs backfill.py — read this before choosing:

    backfill.py (RPC + calldata)   preserves the a / w / s ROLES exactly, works
                                   with any Base RPC, but hand-rolls the ERC-8021
                                   decode and needs the tx calldata.
    backfill_sql.py (this file)    no RPC, no hand-decoding, uses Coinbase's
                                   authoritative index — BUT the attribution
                                   table is role-FLAT: it returns the SET of codes
                                   on a tx, not which is a/w/s.

Because we already captured our own `a` and the buyer's `s` at request time, the
role-flat set is still enough: we (1) RECONCILE — confirm the `s` we recorded
actually landed on-chain — and (2) RECOVER `w` — it's whichever attributed code
isn't our `a` and isn't a recorded `s` (the CDP facilitator's code, `cdp_facil`).
"""
from __future__ import annotations

import os

import cdp_sql
import tracking


def _codes_by_tx(tx_hashes: list[str]) -> dict[str, set[str]]:
    """Return {transaction_hash: {builder_code, ...}} for the given settlements.

    `HAVING sum(action) > 0` keeps only codes still active after any re-org
    (attributions carry action = +1 added / -1 removed, like every Base table).
    """
    if not tx_hashes:
        return {}
    sql = (
        "SELECT transaction_hash, builder_code "
        "FROM base.transaction_attributions "
        f"WHERE transaction_hash IN ({cdp_sql.sql_in_list(tx_hashes)}) "
        "GROUP BY transaction_hash, builder_code "
        "HAVING sum(action) > 0"
    )
    out: dict[str, set[str]] = {}
    for row in cdp_sql.run_query(sql):
        tx = (row.get("transaction_hash") or "").lower()
        code = row.get("builder_code")
        if tx and code:
            out.setdefault(tx, set()).add(code)
    return out


def main() -> int:
    since_hours = int(os.environ.get("BACKFILL_SINCE_HOURS") or "48")
    limit = int(os.environ.get("BACKFILL_LIMIT") or "500")

    conn = tracking.connect()
    rows = tracking.rows_needing_backfill(conn, since_hours=since_hours, limit=limit)
    tx_hashes = [(r["settle_tx_hash"] or "").lower() for r in rows if r["settle_tx_hash"]]
    print(f"candidates: {len(rows)} payment(s) with a settle tx and no w "
          f"(window={since_hours}h)")

    codes_by_tx = _codes_by_tx(tx_hashes)

    updated = reconciled = mismatched = no_attribution = 0
    for row in rows:
        tx = (row["settle_tx_hash"] or "").lower()
        onchain = codes_by_tx.get(tx)
        if not onchain:
            no_attribution += 1  # nothing attributed on this tx; ages out of window
            continue

        known_a = row["builder_code_a"]
        known_s = set((row["builder_code_s"] or "").split(",")) - {""}

        # (1) RECONCILE: every `s` we captured off the payment should be on-chain.
        for s in known_s:
            if s in onchain:
                reconciled += 1
            else:
                mismatched += 1
                print(f"  ⚠ {row['id']}: recorded s={s} NOT found on-chain {sorted(onchain)}")

        # (2) RECOVER w: whatever's left after removing our `a` and the referer `s`.
        leftover = onchain - {known_a} - known_s
        fields: dict[str, str] = {}
        if leftover:
            fields["w"] = ",".join(sorted(leftover))  # typically just "cdp_facil"
        # Gap-fill a/s only if we somehow missed them at request time and the chain
        # shows exactly one plausible value (can't role-disambiguate beyond that).
        if not known_a and known_a not in onchain and len(onchain) == 1:
            fields["a"] = next(iter(onchain))

        if fields:
            tracking.set_builder_codes(conn, row["id"], **fields)
            updated += 1
            print(f"  backfilled {row['id']}: {fields}  (on-chain: {sorted(onchain)})")

    print(f"done: {updated} updated, {reconciled} s-codes reconciled, "
          f"{mismatched} mismatched, {no_attribution} no-attribution")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

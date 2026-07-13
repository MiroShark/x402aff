#!/usr/bin/env python3
"""End-to-end demo with no network — see the whole affiliation loop in ~1 second.

Simulates: declaring your code, three paid runs (two driven by builder `bc_alice`,
one direct), settlement, the on-chain backfill recovering `w`, and the payout
rollup. Uses an in-memory SQLite db so it leaves nothing behind.

    pip install cbor2 && python demo.py
"""
from __future__ import annotations

import cbor2

import tracking
from builder_code import ERC_8021_MARKER, declare_builder_code, parse_builder_code_suffix

APP_CODE = "bc_yourcode"       # yours (the resource server)
FACIL_CODE = "cdp_facil1"      # the CDP facilitator's wallet code


def fake_settlement_calldata(*, a=None, w=None, s=None) -> str:
    """Stand in for what the facilitator writes on-chain: a transfer + suffix."""
    codes = {k: v for k, v in (("a", a), ("w", w), ("s", s)) if v}
    cbor = cbor2.dumps(codes)
    suffix = cbor + len(cbor).to_bytes(2, "big") + bytes([2]) + ERC_8021_MARKER
    return "0x" + ("ab" * 100) + suffix.hex()  # ERC-20 transfer calldata + suffix


def main() -> None:
    conn = tracking.connect(":memory:")

    # 1. DECLARE — this dict is what you attach to your paid x402 route.
    print("declared extension:", declare_builder_code(APP_CODE), "\n")

    # 2. Three paid runs. `s` is the buyer's referer code, captured at request time.
    runs = [
        ("pay_1", "bc_alice", 0.20, "completed"),  # Alice's app drove it, cheap run
        ("pay_2", "bc_alice", 0.55, "completed"),  # Alice again, pricier run
        ("pay_3", None,       0.20, "completed"),  # direct buyer, no referer
    ]
    for pid, referer, cost, _ in runs:
        tracking.record_payment(
            conn, payment_id=pid, payer_address="0xBuyer", payment_network="eip155:8453",
            builder_code_a=APP_CODE, builder_code_s=referer, price_usd=1.00, status="queued",
        )

    # 3. SETTLE — the observer records each settlement tx hash.
    for pid, *_ in runs:
        tracking.set_settle_tx(conn, pid, "0x" + "11" * 32)

    # 4. Work finishes — record cost + final status (net profit needs cost).
    for pid, _referer, cost, status in runs:
        tracking.set_result(conn, pid, cost_usd=cost, status=status)

    # 5. BACKFILL — read each settlement's calldata off-chain, recover `w` (+ a/s).
    for row in tracking.rows_needing_backfill(conn):
        calldata = fake_settlement_calldata(a=APP_CODE, w=FACIL_CODE, s=[row["builder_code_s"]] if row["builder_code_s"] else None)
        parsed = parse_builder_code_suffix(calldata) or {}
        tracking.set_builder_codes(conn, row["id"], w=parsed.get("w"))
    print("recovered facilitator code w =", FACIL_CODE, "on all settled runs\n")

    # 6. PAYOUTS — roll completed runs up into $ owed per referer (50% of net).
    print("payouts (50% of net profit = $1.00 − your cost):")
    for p in tracking.compute_payouts(conn, share=0.50):
        print(f"  {p.builder_code}: {p.completed_runs} runs, "
              f"net ${p.net_profit_usd:.2f} → owed ${p.owed_usd:.2f}")
    # bc_alice: 2 runs, net (0.80 + 0.45) = $1.25 → owed $0.625


if __name__ == "__main__":
    main()

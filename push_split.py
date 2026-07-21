"""0xSplits v2 PushSplit primitives — calldata + counterfactual address, no web3.

These are the on-chain building blocks the CDP path uses to turn a `SplitPlan`
into real transactions:

  * ``predict_split_address(plan)`` — the pair's deterministic PushSplit address
    (one ``eth_call`` to the factory) and whether it's deployed yet. This is the
    address the route's ``payTo`` is set to, and the address ``distribute`` runs
    on; USDC can land there BEFORE the split is deployed.
  * ``create_split_calldata`` / ``distribute_calldata`` — the two legs that
    release a funded split (deploy once, then distribute), both permissionless.

Split params are fixed: ``owner = 0`` → the split is IMMUTABLE (recipients and
ratios can never change), ``salt = 0`` → exactly one canonical address per
``(recipients, allocations)`` pair. Verified byte-for-byte against the live Base
factory on a mainnet fork (see ``fork-test/CdpPath.t.sol``). No web3 dependency —
just ``requests`` for the one eth_call, mirroring ``resolver.py``.
"""
from __future__ import annotations

import os
from typing import Optional

import requests

import resolver
import split

# ── Base mainnet addresses ────────────────────────────────────────────────────
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
# 0xSplits PushSplitFactory V2.2 on Base — confirmed by the Splits team
# (2026-07-15) and verified on-chain (Basescan: "PushSplitFactory", 0xSplits).
# Canonical list: https://splits.org/protocol/docs/core/split-v2#addresses
SPLITS_PUSH_FACTORY = os.environ.get(
    "X402_SPLITS_PUSH_FACTORY", "0x8E8eB0cC6AE34A38B67D5Cf91ACa38f60bc3Ecf4"
)
BASE_RPC = os.environ.get("X402_BASE_RPC", resolver.DEFAULT_RPC)
# Cut to the referer builder code, in basis points (1000 = 10%).
BUILDER_SHARE_BPS = int(
    os.environ.get("X402_BUILDER_SHARE_BPS", str(split.DEFAULT_BUILDER_SHARE_BPS))
)

ZERO_ADDRESS = "0x" + "00" * 20
ZERO_SALT = "0x" + "00" * 32

# 4-byte selectors, computed from the verified factory/wallet ABIs:
_SEL_IS_DEPLOYED = "cd6bc121"   # isDeployed(Split,address,bytes32) → (address,bool)
_SEL_CREATE_DET = "f79918b0"    # createSplitDeterministic(Split,address,address,bytes32)
_SEL_DISTRIBUTE = "2d3f5537"    # distribute(Split,address,address)  [on the PushSplit]


def _word(value: int) -> str:
    return f"{value:064x}"


def _addr_word(address: str) -> str:
    return _word(int(address, 16))


def encode_split_params(plan: split.SplitPlan) -> str:
    """ABI-encode the ``SplitV2Lib.Split`` tuple from a plan (hex, no 0x).

    Split = (address[] recipients, uint256[] allocations, uint256 totalAllocation,
    uint16 distributionIncentive). Allocations are the plan's bps (sum = 10000);
    incentive is 0 — whoever calls distribute earns nothing, they just pay gas.
    """
    recips = [addr for addr, _ in plan.recipients]
    allocs = [bps for _, bps in plan.recipients]
    n = len(recips)
    head = [
        _word(0x80),                 # offset of recipients[] within the tuple
        _word(0x80 + 32 * (1 + n)),  # offset of allocations[]
        _word(split.BPS_DENOM),      # totalAllocation
        _word(0),                    # distributionIncentive
    ]
    tail = [_word(n), *(_addr_word(a) for a in recips),
            _word(n), *(_word(v) for v in allocs)]
    return "".join(head + tail)


def is_deployed_calldata(plan: split.SplitPlan) -> str:
    """Calldata for factory.isDeployed(split, owner=0, salt=0) — owner 0 makes the
    split IMMUTABLE (recipients fixed forever), salt 0 makes one canonical address
    per (recipients, allocations) pair. Verified byte-for-byte vs `cast calldata`."""
    return ("0x" + _SEL_IS_DEPLOYED + _word(0x60)
            + _addr_word(ZERO_ADDRESS) + ZERO_SALT[2:] + encode_split_params(plan))


def create_split_calldata(plan: split.SplitPlan, *, creator: str = ZERO_ADDRESS) -> str:
    """Calldata for factory.createSplitDeterministic(split, owner=0, creator, salt=0)
    — the deploy leg. Same params as isDeployed, so it lands ON the predicted
    address. Skip this leg when the pair's split is already deployed."""
    return ("0x" + _SEL_CREATE_DET + _word(0x80) + _addr_word(ZERO_ADDRESS)
            + _addr_word(creator) + ZERO_SALT[2:] + encode_split_params(plan))


def distribute_calldata(plan: split.SplitPlan, *, distributor: str = ZERO_ADDRESS) -> str:
    """Calldata for pushSplit.distribute(split, USDC, distributor) — pays every
    recipient their bps of the split's CURRENT balance (arrival time irrelevant,
    pre-deploy funding supported — confirmed by Splits). Incentive is 0 so
    ``distributor`` is only recorded in the event; any caller address is fine."""
    return ("0x" + _SEL_DISTRIBUTE + _word(0x60) + _addr_word(USDC_BASE)
            + _addr_word(distributor) + encode_split_params(plan))


def predict_split_address(
    plan: split.SplitPlan,
    *,
    rpc_url: Optional[str] = None,
    timeout: float = 20.0,
) -> tuple[str, bool]:
    """The pair's counterfactual PushSplit address + whether it's deployed yet.

    One ``eth_call`` to factory.isDeployed — this is the address ``payTo`` is set
    to and distribute is called on; funds may land there BEFORE deployment.
    """
    resp = requests.post(
        rpc_url or BASE_RPC,
        json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
              "params": [{"to": SPLITS_PUSH_FACTORY,
                          "data": is_deployed_calldata(plan)}, "latest"]},
        timeout=timeout,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("error"):
        raise RuntimeError(f"isDeployed eth_call failed: {body['error']}")
    return _decode_is_deployed(body["result"])


def _decode_is_deployed(result: str) -> tuple[str, bool]:
    """Decode isDeployed's (address split, bool exists) return words."""
    words = result.removeprefix("0x")
    return "0x" + words[24:64], int(words[64:128], 16) != 0


if __name__ == "__main__":
    demo = split.build_split_plan(
        seller_payout="0x2222222222222222222222222222222222222222",
        builder_payout="0x1111111111111111111111111111111111111111",
        builder_code="bc_alice",
        builder_share_bps=BUILDER_SHARE_BPS,
    )
    print("PushSplit params for a 10/90 split:")
    print("  is_deployed:", is_deployed_calldata(demo)[:18], "…")
    print("  create     :", create_split_calldata(demo)[:18], "…")
    print("  distribute :", distribute_calldata(demo)[:18], "…")
    print("\nlive: predict_split_address(plan) → (address, deployed) in one eth_call")

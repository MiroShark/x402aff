"""Fan a funded per-pair split out to its recipients — the CDP-path payout leg.

On the request-time ``payTo`` path (`payto.py`) the CDP facilitator settles a
plain USDC transfer into a per-pair PushSplit. The money is safe there (the split
is ownerless) but sits until someone calls ``distribute``. This builds the two
calls that release it:

    deploy_split   — only the first time this pair is used (createSplitDeterministic)
    distribute     — pay each recipient their bps of the split's current balance

Both are **permissionless**: any address can submit them, so the builder can even
self-serve. Every call here carries ready-to-submit calldata — signing and
broadcasting is yours (a funded Base account; gas is cents, and one distribute
clears every payment that has accumulated in the pair since the last one).
Use ``monitor.py`` to find which splits are holding funds ready to release.

Run ``python3 distribute.py`` for an offline demo against a stubbed builder, or
``distribute_plan(plan, rpc_url=...)`` to check the live on-chain balance first.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import requests

import push_split
import split

USDC_BASE = push_split.USDC_BASE
_SEL_BALANCE_OF = "70a08231"  # balanceOf(address)


@dataclass
class DistributeCall:
    """One leg of releasing a funded split (deploy is skipped once deployed)."""

    step: str  # deploy_split | distribute
    target: str
    summary: str
    data: str


def distribute_calls(
    plan: split.SplitPlan,
    *,
    split_address: str,
    deployed: bool,
    distributor: Optional[str] = None,
) -> list[DistributeCall]:
    """The calls to release a funded pair. Empty when there's no builder leg
    (a builderless payment was a plain transfer straight to the seller — nothing
    ever entered a split, so there's nothing to distribute)."""
    if not plan.has_builder:
        return []

    dist = distributor or push_split.ZERO_ADDRESS
    calls: list[DistributeCall] = []
    if not deployed:
        calls.append(
            DistributeCall(
                "deploy_split",
                push_split.SPLITS_PUSH_FACTORY,
                f"deploy the per-pair PushSplit at {split_address} "
                "(first use of this pair; permissionless)",
                push_split.create_split_calldata(plan),
            )
        )
    recips = ", ".join(f"{a}:{bps}bps" for a, bps in plan.recipients)
    calls.append(
        DistributeCall(
            "distribute",
            split_address,
            f"distribute the split's USDC balance → [{recips}] "
            f"({plan.builder_share_bps}bps to {plan.builder_code})",
            push_split.distribute_calldata(plan, distributor=dist),
        )
    )
    return calls


def split_balance_units(split_address: str, *, rpc_url: Optional[str] = None,
                        timeout: float = 20.0) -> int:
    """USDC balance sitting in the split right now (base units) — how much the
    next distribute will release. One ``eth_call`` to USDC.balanceOf."""
    data = "0x" + _SEL_BALANCE_OF + push_split._addr_word(split_address)
    resp = requests.post(
        rpc_url or push_split.BASE_RPC,
        json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
              "params": [{"to": USDC_BASE, "data": data}, "latest"]},
        timeout=timeout,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("error"):
        raise RuntimeError(f"balanceOf eth_call failed: {body['error']}")
    return int(body["result"], 16)


def distribute_plan(
    plan: split.SplitPlan,
    *,
    rpc_url: Optional[str] = None,
    distributor: Optional[str] = None,
) -> tuple[list[DistributeCall], int]:
    """Live: predict the pair's address, read its balance, and build the release
    calls. Returns ``(calls, balance_units)``; skip submitting when balance is 0.
    """
    if not plan.has_builder:
        return [], 0
    address, deployed = push_split.predict_split_address(plan, rpc_url=rpc_url)
    balance = split_balance_units(address, rpc_url=rpc_url)
    calls = distribute_calls(
        plan, split_address=address, deployed=deployed, distributor=distributor
    )
    return calls, balance


if __name__ == "__main__":
    demo = split.build_split_plan(
        seller_payout=os.environ.get(
            "X402_SELLER_PAYOUT", "0x2222222222222222222222222222222222222222"),
        builder_payout="0x1111111111111111111111111111111111111111",
        builder_code="bc_alice",
        builder_share_bps=push_split.BUILDER_SHARE_BPS,
    )
    print("release a funded per-pair split (CDP path — no settler):\n")
    for c in distribute_calls(
        demo, split_address="0x<predict_split_address(plan)>", deployed=False
    ):
        print(f"  [{c.step}] {c.target}")
        print(f"      {c.summary}")
        print(f"      calldata: {c.data[:10]}… ({(len(c.data) - 2) // 2} bytes)")
    print("\n(live: distribute_plan(plan, rpc_url=...) predicts the address, "
          "reads the balance, and skips the deploy leg once it exists)")

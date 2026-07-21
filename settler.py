"""Reference settler — split a settled x402 payment on-chain, automatically.

This is the piece that turns affiliation from a *manual off-chain payout* into an
*enforced* one: the builder's cut is paid in the same act as the payment —
**automatic, atomic, auditable**. It is config-driven so ANY x402 seller can drop
it in: set your payout wallet + share and point your route's settlement here
instead of the stock facilitator.

The flow, per settled payment:

    read `s` off the payment  ─▶  resolve builder payout (resolver)
        ─▶  build split plan (split)  ─▶  ONE atomic tx:
              pull the buyer's USDC to the settler (EIP-3009)
              → forward it into the per-(seller,builder) PushSplit
              → deploy that split if new
              → distribute → pushes the cut to the builder, the rest to you

SET YOUR ROUTE'S `payTo` TO THE SETTLER ACCOUNT (`X402_SETTLER_ACCOUNT`). This is
load-bearing, not a style choice. EIP-3009 binds the buyer's signature to one
recipient and `receiveWithAuthorization` further requires `msg.sender == to`, so
funds can ONLY enter via the address the 402 advertised. The per-pair split
address is derived from the `s` code, which arrives *inside the payment* — after
`payTo` was fixed — so the buyer can never have signed to it. Pulling straight
into the split reverts on both counts (verified on a Base mainnet fork). Hence
the settler is the payTo and forwards. It holds the money for the space of two
instructions inside one tx; the split still decides the amounts.

That single atomic tx is r0ohafza's (0xSplits) recipe: a 7702 account doing
pull + fund + deploy + distribute in one multicall, using an audited PushSplit so you
write/audit no split math. See INTEGRATION.md.

Why a settler at all: the builder is named by the payment-time `s` code (it rides
INSIDE the payment, not the request), so it isn't known when your 402 sets
`payTo`. Something therefore has to read `s` at settlement and route the split —
and the stock CDP facilitator only ever calls the USDC token, never your split.
You already hold the buyer's signature, so that "something" is this.

TRUST NOTE — be clear-eyed: because you run this, the split is automatic/atomic/
auditable but NOT cryptographically trustless. `s` isn't in the buyer's signature,
so nothing on-chain forces you to run the settler or to pass the real `s`. The
contract enforces the ratio; you're trusted to run it. Fine for an affiliate
program; not for adversarial counterparties.

This module builds the *plan* and the *real calldata* for every leg except the
pull (which needs the buyer's runtime signature). The whole sequence — predicted
address, deploy, forward, distribute — is verified end-to-end against the live
Base factory on a mainnet fork; see `fork-test/`. The piece left to you is
signing + submitting the multicall with your 7702 settler account — every
target/calldata pair below drops straight into its call list.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import requests

import resolver
import split

# ── Config — set these to make the kit yours (env-overridable) ────────────────
# Where your share (the remainder after the builder cut) is paid.
SELLER_PAYOUT = os.environ.get("X402_SELLER_PAYOUT", "")
# Your 7702 settler account. THIS MUST BE THE ROUTE'S `payTo` — see the module
# docstring. The buyer's EIP-3009 signature names its recipient and only that
# recipient may redeem it, so funds can only enter via this address.
SETTLER_ACCOUNT = os.environ.get("X402_SETTLER_ACCOUNT", "")
# Cut to the referer builder code, in basis points (1000 = 10%).
BUILDER_SHARE_BPS = int(
    os.environ.get("X402_BUILDER_SHARE_BPS", str(split.DEFAULT_BUILDER_SHARE_BPS))
)
BASE_RPC = os.environ.get("X402_BASE_RPC", resolver.DEFAULT_RPC)

# Base mainnet addresses.
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
# 0xSplits PushSplitFactory V2.2 on Base — confirmed by the Splits team
# (2026-07-15) and verified on-chain (Basescan: "PushSplitFactory", 0xSplits).
# Canonical list: https://splits.org/protocol/docs/core/split-v2#addresses
SPLITS_PUSH_FACTORY = os.environ.get(
    "X402_SPLITS_PUSH_FACTORY", "0x8E8eB0cC6AE34A38B67D5Cf91ACa38f60bc3Ecf4"
)

ZERO_ADDRESS = "0x" + "00" * 20
ZERO_SALT = "0x" + "00" * 32

# ── Splits v2 calldata (no web3 dependency, mirrors resolver.py) ──────────────
# 4-byte selectors, computed from the verified factory/wallet ABIs:
_SEL_IS_DEPLOYED = "cd6bc121"   # isDeployed(Split,address,bytes32) → (address,bool)
_SEL_CREATE_DET = "f79918b0"    # createSplitDeterministic(Split,address,address,bytes32)
_SEL_DISTRIBUTE = "2d3f5537"    # distribute(Split,address,address)  [on the PushSplit]
_SEL_TRANSFER = "a9059cbb"      # transfer(address,uint256)          [on USDC]


def _word(value: int) -> str:
    return f"{value:064x}"


def _addr_word(address: str) -> str:
    return _word(int(address, 16))


def encode_split_params(plan: split.SplitPlan) -> str:
    """ABI-encode the ``SplitV2Lib.Split`` tuple from a plan (hex, no 0x).

    Split = (address[] recipients, uint256[] allocations, uint256 totalAllocation,
    uint16 distributionIncentive). Allocations are the plan's bps (sum = 10000);
    incentive is 0 — the settler distributes as part of settling, it isn't paid.
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
    ``distributor`` is only recorded in the event; the settler address is fine."""
    return ("0x" + _SEL_DISTRIBUTE + _word(0x60) + _addr_word(USDC_BASE)
            + _addr_word(distributor) + encode_split_params(plan))


def transfer_calldata(to: str, price_usd: float) -> str:
    """Calldata for USDC.transfer(to, amount) — the settler forwarding the pulled
    payment on. Used both to fund the split and to pay a builderless payment
    straight through to the seller."""
    return ("0x" + _SEL_TRANSFER + _addr_word(to)
            + _word(split.to_units(price_usd)))


def predict_split_address(
    plan: split.SplitPlan,
    *,
    rpc_url: Optional[str] = None,
    timeout: float = 20.0,
) -> tuple[str, bool]:
    """The pair's counterfactual PushSplit address + whether it's deployed yet.

    One ``eth_call`` to factory.isDeployed — this is the address USDC is pulled
    into and distribute is called on; funds may land there BEFORE deployment.
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


@dataclass
class SettlementCall:
    """One leg of the atomic multicall the 7702 settler submits in a single tx."""

    step: str  # deploy_split | pull_funds | distribute
    target: str  # the contract this leg calls
    summary: str
    data: Optional[str] = None  # ready-to-submit calldata (None = built at runtime)


def plan_split(
    s_code: str | None,
    *,
    seller_payout: str | None = None,
    builder_share_bps: int | None = None,
    rpc_url: str | None = None,
) -> split.SplitPlan:
    """Resolve `s` and build the split plan for one settled payment."""
    return split.resolve_and_plan(
        s_code,
        seller_payout or SELLER_PAYOUT,
        builder_share_bps=(
            BUILDER_SHARE_BPS if builder_share_bps is None else builder_share_bps
        ),
        rpc_url=rpc_url or BASE_RPC,
    )


def settlement_calls(
    plan: split.SplitPlan,
    *,
    amount_usdc: float,
    buyer_from: str,
    split_address: Optional[str] = None,
    deployed: bool = False,
    settler_account: Optional[str] = None,
) -> list[SettlementCall]:
    """The atomic multicall to submit in ONE tx (pull → fund → deploy → distribute).

    ``split_address``/``deployed`` come from ``predict_split_address(plan)`` (one
    eth_call); when the pair's split already exists the deploy leg is skipped.

    The buyer's EIP-3009 authorization names ``settler_account`` as its recipient
    (that's your route's ``payTo``), so the pull lands on the settler and a
    ``fund_split`` leg forwards it on — see the module docstring for why it can't
    go straight into the split. Only ``pull_funds`` is built at runtime (it needs
    the buyer's signature, which is in the X-PAYMENT payload); every other leg
    below carries ready-to-submit calldata.

    When `s` is unresolved there's no builder and no split — but the money still
    arrives at the settler (``payTo`` is the settler for *every* payment on the
    route), so it's pull + forward the full amount to the seller.
    """
    settler = settler_account or SETTLER_ACCOUNT or "<X402_SETTLER_ACCOUNT>"
    calls: list[SettlementCall] = [
        SettlementCall(
            "pull_funds",
            USDC_BASE,
            f"receiveWithAuthorization {amount_usdc} USDC from {buyer_from} → "
            f"{settler} (the route's payTo; only it can redeem the buyer's sig)",
        )
    ]

    if not plan.has_builder:
        calls.append(
            SettlementCall(
                "payout_seller",
                USDC_BASE,
                f"transfer {amount_usdc} USDC → {plan.seller_payout} "
                "(no `s`/unregistered → 100% to seller, no split needed)",
                data=transfer_calldata(plan.seller_payout, amount_usdc),
            )
        )
        return calls

    split_addr = split_address or "<predict_split_address(plan)>"
    recips = ", ".join(f"{a}:{bps}bps" for a, bps in plan.recipients)
    calls.append(
        SettlementCall(
            "fund_split",
            USDC_BASE,
            f"transfer {amount_usdc} USDC → {split_addr} "
            "(funding before deploy is supported — distribute reads balance)",
            data=transfer_calldata(split_addr, amount_usdc) if split_address else None,
        )
    )
    if not deployed:
        calls.append(
            SettlementCall(
                "deploy_split",
                SPLITS_PUSH_FACTORY,
                f"deploy per-pair PushSplit for [{recips}] at {split_addr}",
                data=create_split_calldata(plan),
            )
        )
    calls.append(
        SettlementCall(
            "distribute",
            split_addr,
            f"distribute → push {plan.builder_share_bps}bps to {plan.builder_payout} "
            f"({plan.builder_code}), remainder to {plan.seller_payout}",
            data=distribute_calldata(plan),
        )
    )
    return calls


if __name__ == "__main__":
    # Offline demo — build a plan + the call sequence from a stubbed builder
    # payout (no network). Use plan_split(...) for the live registry resolve.
    demo_plan = split.build_split_plan(
        seller_payout="0x2222222222222222222222222222222222222222",   # you
        builder_payout="0x1111111111111111111111111111111111111111",  # bc_alice's
        builder_code="bc_alice",
        builder_share_bps=BUILDER_SHARE_BPS,
    )
    price = 1.00
    print(f"split plan for a ${price:.2f} payment (share = {BUILDER_SHARE_BPS} bps):")
    for addr, amt in demo_plan.amounts(price).items():
        print(f"  {addr}  ${amt:.6f}")
    print(f"  ({demo_plan.dust_units(price)} base units stay in the split — "
          "Splits keeps 1 unit warm and floors each share)")
    print("\natomic settle multicall (one tx):")
    for c in settlement_calls(demo_plan, amount_usdc=price, buyer_from="0xBuyer"):
        print(f"  [{c.step}] {c.target}\n      {c.summary}")
        if c.data:
            print(f"      calldata: {c.data[:10]}… ({(len(c.data) - 2) // 2} bytes)")
    print("\n(live: predict_split_address(plan) gives the real per-pair address "
          "+ whether it's deployed — one eth_call)")

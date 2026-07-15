"""Reference settler — split a settled x402 payment on-chain, automatically.

This is the piece that turns affiliation from a *manual off-chain payout* into an
*enforced* one: the builder's cut is paid in the same act as the payment —
**automatic, atomic, auditable**. It is config-driven so ANY x402 seller can drop
it in: set your payout wallet + share and point your route's settlement here
instead of the stock facilitator.

The flow, per settled payment:

    read `s` off the payment  ─▶  resolve builder payout (resolver)
        ─▶  build split plan (split)  ─▶  ONE atomic tx:
              deploy the per-(seller,builder) PushSplit if new
              → pull the buyer's USDC into it (EIP-3009)
              → distribute → pushes the cut to the builder, the rest to you

That single atomic tx is r0ohafza's (0xSplits) recipe: a 7702 account doing
deploy + fund + distribute in one multicall, using an audited PushSplit so you
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

This module builds the *plan* and *call sequence*; the on-chain submission
(7702 tx + Splits factory ABI) is the integration seam you wire — see the open
items in INTEGRATION.md before mainnet.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import resolver
import split

# ── Config — set these to make the kit yours (env-overridable) ────────────────
# Where your share (the remainder after the builder cut) is paid.
SELLER_PAYOUT = os.environ.get("X402_SELLER_PAYOUT", "")
# Cut to the referer builder code, in basis points (1000 = 10%).
BUILDER_SHARE_BPS = int(
    os.environ.get("X402_BUILDER_SHARE_BPS", str(split.DEFAULT_BUILDER_SHARE_BPS))
)
BASE_RPC = os.environ.get("X402_BASE_RPC", resolver.DEFAULT_RPC)

# Base mainnet addresses (verify before mainnet use).
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
# 0xSplits PushSplit factory on Base. CONFIRM the address AND the deterministic
# (CREATE2) address derivation against the Splits SDK before wiring — this is an
# open item in INTEGRATION.md (Splits push variant: distribute() sends on
# distribution, no separate withdraw).
SPLITS_PUSH_FACTORY = os.environ.get("X402_SPLITS_PUSH_FACTORY", "")


@dataclass
class SettlementCall:
    """One leg of the atomic multicall the 7702 settler submits in a single tx."""

    step: str  # deploy_split | pull_funds | distribute
    target: str  # the contract this leg calls
    summary: str


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
) -> list[SettlementCall]:
    """The atomic multicall to submit in ONE tx (deploy → fund → distribute).

    When `s` is unresolved there is no builder leg, so this is just the plain
    transfer to the seller (let the stock facilitator handle those and only route
    the builder-bearing ones through here, if you like). Returns a description of
    the legs; wiring the actual submission (7702 tx + Splits SDK) is the seam.
    """
    calls: list[SettlementCall] = []
    if not plan.has_builder:
        calls.append(
            SettlementCall(
                "pull_funds",
                USDC_BASE,
                f"transfer {amount_usdc} USDC from {buyer_from} → {plan.seller_payout} "
                "(no `s`/unregistered → 100% to seller, no split needed)",
            )
        )
        return calls

    recips = ", ".join(f"{a}:{bps}bps" for a, bps in plan.recipients)
    calls.append(
        SettlementCall(
            "deploy_split",
            SPLITS_PUSH_FACTORY or "<SPLITS_PUSH_FACTORY>",
            f"ensure per-pair PushSplit for [{recips}] (counterfactual; deploy on first funding)",
        )
    )
    calls.append(
        SettlementCall(
            "pull_funds",
            USDC_BASE,
            f"receiveWithAuthorization {amount_usdc} USDC from {buyer_from} → the PushSplit",
        )
    )
    calls.append(
        SettlementCall(
            "distribute",
            "<PushSplit address>",
            f"distribute → push {plan.builder_share_bps}bps to {plan.builder_payout} "
            f"({plan.builder_code}), remainder to {plan.seller_payout}",
        )
    )
    return calls


if __name__ == "__main__":
    # Offline demo — build a plan + the call sequence from a stubbed builder
    # payout (no network). Use plan_split(...) for the live registry resolve.
    demo_plan = split.build_split_plan(
        seller_payout="0xSeller00000000000000000000000000000000",
        builder_payout="0xBuilderAlice000000000000000000000000000",
        builder_code="bc_alice",
        builder_share_bps=BUILDER_SHARE_BPS,
    )
    price = 1.00
    print(f"split plan for a ${price:.2f} payment (share = {BUILDER_SHARE_BPS} bps):")
    for addr, amt in demo_plan.amounts(price).items():
        print(f"  {addr}  ${amt:.2f}")
    print("\natomic settle multicall (one tx):")
    for c in settlement_calls(demo_plan, amount_usdc=price, buyer_from="0xBuyer"):
        print(f"  [{c.step}] {c.target}\n      {c.summary}")

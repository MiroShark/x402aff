"""The split plan for one builder-attributed payment - the ENFORCED payout core.

Given a builder code and the price, this resolves *who* to pay and produces the
*split plan*: the recipient set + basis-point allocations (builder cut + seller
remainder) that a per-(seller, builder) 0xSplits PushSplit encodes.

It's the pure-arithmetic half of the x402aff kit's enforced payout: ``push_split.py``
turns a plan into on-chain calldata, and ``payto.py`` sets the route's ``payTo``
to the plan's split so the CDP facilitator settles straight into it. Address
resolution reuses ``resolver`` (only needs ``requests``); this module itself is
pure arithmetic + dataclasses, so the payout math is trivially testable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import resolver

# Default cut to the referer builder code. 1000 bps = 10% of the payment.
DEFAULT_BUILDER_SHARE_BPS = 1000
BPS_DENOM = 10_000

# USDC is 6-decimal; all on-chain math happens in these base units.
USDC_DECIMALS = 6
_UNITS_PER_USD = 10**USDC_DECIMALS

# A Splits v2 PushSplit never pays out its last base unit - it leaves 1 behind so
# the balance slot stays warm (a gas optimization). Confirmed on a Base mainnet
# fork: a $1.00 payment pays $0.099999 / $0.899999, not $0.10 / $0.90.
SPLITS_RETAINED_UNITS = 1


def to_units(price_usd: float) -> int:
    """Dollars → USDC base units (the integer the chain actually moves)."""
    return int(round(price_usd * _UNITS_PER_USD))


@dataclass
class SplitPlan:
    """How one payment is carved up on-chain.

    ``recipients`` is ``[(address, allocation_bps), ...]`` summing to
    ``BPS_DENOM`` - the exact shape a per-pair PushSplit is created with. When the
    ``s`` code is missing or unregistered there is no builder leg and the whole
    amount goes to the seller (never stranded).
    """

    seller_payout: str
    builder_code: Optional[str]
    builder_payout: Optional[str]
    builder_share_bps: int
    recipients: list[tuple[str, int]]

    @property
    def has_builder(self) -> bool:
        return bool(self.builder_payout) and self.builder_share_bps > 0

    def amounts_units(self, price_usd: float) -> dict[str, int]:
        """What each recipient *actually receives on-chain*, in USDC base units.

        This mirrors Splits v2 exactly (verified on a Base mainnet fork), so the
        ledger reconciles to the penny against the settle tx:

          * A PushSplit retains ``SPLITS_RETAINED_UNITS`` (1 unit) to keep its
            balance slot warm - only ``balance - 1`` is ever distributable.
          * Each recipient's share is **floored**, not rounded.

        Both effects only apply when the money actually flows through a split. A
        builderless plan is a plain USDC transfer, so the seller gets every unit.
        """
        units = to_units(price_usd)
        if not self.has_builder:
            return {self.seller_payout: units}
        distributable = max(0, units - SPLITS_RETAINED_UNITS)
        return {
            addr: distributable * bps // BPS_DENOM for addr, bps in self.recipients
        }

    def dust_units(self, price_usd: float) -> int:
        """Units left behind in the split (retained unit + floor remainders).

        Bounded by ``SPLITS_RETAINED_UNITS + len(recipients) - 1`` - i.e. 2 units
        ($0.000002) for a two-way split, regardless of payment size.
        """
        return to_units(price_usd) - sum(self.amounts_units(price_usd).values())

    def amounts(self, price_usd: float) -> dict[str, float]:
        """``amounts_units`` in dollars - what each recipient really receives."""
        return {
            addr: units / _UNITS_PER_USD
            for addr, units in self.amounts_units(price_usd).items()
        }


def build_split_plan(
    seller_payout: str,
    builder_payout: Optional[str],
    *,
    builder_code: Optional[str] = None,
    builder_share_bps: int = DEFAULT_BUILDER_SHARE_BPS,
) -> SplitPlan:
    """Build a split plan from already-resolved addresses (no network).

    ``builder_payout`` None/empty (unregistered or no ``s``) → seller gets 100%.
    Raises ValueError on a missing seller or an out-of-range share.
    """
    if not seller_payout:
        raise ValueError("seller_payout is required")
    if not (0 <= builder_share_bps <= BPS_DENOM):
        raise ValueError(f"builder_share_bps must be 0..{BPS_DENOM}")

    if builder_payout and builder_share_bps > 0:
        recipients = [
            (builder_payout, builder_share_bps),
            (seller_payout, BPS_DENOM - builder_share_bps),
        ]
    else:
        recipients = [(seller_payout, BPS_DENOM)]
        builder_payout = None

    return SplitPlan(
        seller_payout=seller_payout,
        builder_code=builder_code,
        builder_payout=builder_payout,
        builder_share_bps=builder_share_bps,
        recipients=recipients,
    )


def resolve_and_plan(
    s_code: Optional[str],
    seller_payout: str,
    *,
    builder_share_bps: int = DEFAULT_BUILDER_SHARE_BPS,
    rpc_url: str = resolver.DEFAULT_RPC,
) -> SplitPlan:
    """Resolve the referer code ``s`` → payout via the Base registry, then plan.

    ``s_code`` may be comma-joined (layered clients); the split pays the *primary*
    (first valid) code - the single-``s`` v1 policy. An unregistered or missing
    code yields a seller-only plan, so a bad/absent ``s`` never blocks settlement.
    """
    code = primary_code(s_code)
    payout: Optional[str] = None
    if code:
        info = resolver.resolve(code, rpc_url=rpc_url)
        if info.get("registered"):
            payout = info.get("payout_address")
    return build_split_plan(
        seller_payout, payout, builder_code=code, builder_share_bps=builder_share_bps
    )


def primary_code(s_code: Optional[str]) -> Optional[str]:
    """First valid code from a possibly comma-joined ``s`` (v1 pays one builder)."""
    if not s_code:
        return None
    for part in str(s_code).split(","):
        part = part.strip()
        if part:
            return part
    return None

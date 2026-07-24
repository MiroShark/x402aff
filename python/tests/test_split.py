"""Tests for the on-chain split core (split.py) - pure, no network.

    pytest test_split.py -q
"""
from __future__ import annotations

import pytest

from x402aff import split

SELLER = "0xSeller00000000000000000000000000000000"
BUILDER = "0xBuilderAlice000000000000000000000000000"


def test_default_share_is_ten_percent():
    assert split.DEFAULT_BUILDER_SHARE_BPS == 1000  # 10%


def test_plan_splits_90_10_by_default():
    plan = split.build_split_plan(SELLER, BUILDER, builder_code="bc_alice")
    assert plan.has_builder
    assert dict(plan.recipients) == {BUILDER: 1000, SELLER: 9000}
    # Not 0.10/0.90: a PushSplit retains 1 base unit and floors each share, so
    # $1.00 pays out 99999/899999 units. Verified on a Base fork - see
    # fork-test/EndToEnd.t.sol.
    assert plan.amounts_units(1.00) == {BUILDER: 99_999, SELLER: 899_999}
    assert plan.dust_units(1.00) == 2
    amounts = plan.amounts(1.00)
    assert amounts[BUILDER] == pytest.approx(0.099999)
    assert amounts[SELLER] == pytest.approx(0.899999)


def test_payout_never_exceeds_what_the_chain_pays():
    """The ledger must never over-credit a builder - that's a real payout gap."""
    plan = split.build_split_plan(SELLER, BUILDER, builder_code="bc_alice")
    for price in (0.001, 0.01, 1.00, 4.20, 99.99, 1000.00):
        units = plan.amounts_units(price)
        assert sum(units.values()) <= split.to_units(price)
        assert units[BUILDER] <= split.to_units(price) * 1000 // split.BPS_DENOM
        # dust is bounded regardless of size: retained unit + one floor per leg
        assert 0 <= plan.dust_units(price) <= split.SPLITS_RETAINED_UNITS + 1


def test_builderless_plan_has_no_dust():
    """No split contract in the path → plain transfer → seller gets every unit."""
    plan = split.build_split_plan(SELLER, None)
    assert plan.amounts_units(1.00) == {SELLER: 1_000_000}
    assert plan.dust_units(1.00) == 0


def test_recipient_bps_always_sum_to_denominator():
    for bps in (0, 1, 500, 1000, 5000, 9999, 10000):
        plan = split.build_split_plan(SELLER, BUILDER, builder_share_bps=bps)
        assert sum(b for _, b in plan.recipients) == split.BPS_DENOM


def test_no_builder_payout_means_seller_takes_everything():
    plan = split.build_split_plan(SELLER, None, builder_code="bc_ghost")
    assert not plan.has_builder
    assert plan.recipients == [(SELLER, split.BPS_DENOM)]
    assert plan.amounts(1.00) == {SELLER: pytest.approx(1.00)}


def test_zero_share_means_no_builder_leg():
    plan = split.build_split_plan(SELLER, BUILDER, builder_share_bps=0)
    assert not plan.has_builder
    assert plan.recipients == [(SELLER, split.BPS_DENOM)]


def test_seller_required():
    with pytest.raises(ValueError):
        split.build_split_plan("", BUILDER)


def test_share_out_of_range_rejected():
    for bad in (-1, 10001, 20000):
        with pytest.raises(ValueError):
            split.build_split_plan(SELLER, BUILDER, builder_share_bps=bad)


def test_custom_share():
    plan = split.build_split_plan(SELLER, BUILDER, builder_share_bps=2500)
    assert dict(plan.recipients) == {BUILDER: 2500, SELLER: 7500}
    assert plan.amounts_units(4.00)[BUILDER] == 999_999  # floor(25% of 4.00 - 1 unit)


def test_primary_code_picks_first_valid():
    assert split.primary_code("bc_alice,bc_bob") == "bc_alice"
    assert split.primary_code("  ,bc_bob") == "bc_bob"
    assert split.primary_code("") is None
    assert split.primary_code(None) is None


def test_resolve_and_plan_uses_registry(monkeypatch):
    # Stub the on-chain resolve so the test stays offline.
    monkeypatch.setattr(
        split.resolver,
        "resolve",
        lambda code, **kw: {"registered": True, "payout_address": BUILDER},
    )
    plan = split.resolve_and_plan("bc_alice", SELLER)
    assert plan.builder_payout == BUILDER
    assert dict(plan.recipients) == {BUILDER: 1000, SELLER: 9000}


def test_resolve_and_plan_unregistered_falls_back_to_seller(monkeypatch):
    monkeypatch.setattr(
        split.resolver,
        "resolve",
        lambda code, **kw: {"registered": False, "payout_address": None},
    )
    plan = split.resolve_and_plan("bc_ghost", SELLER)
    assert not plan.has_builder
    assert plan.recipients == [(SELLER, split.BPS_DENOM)]


def test_resolve_and_plan_no_code_is_seller_only(monkeypatch):
    called = False

    def _boom(*a, **k):
        nonlocal called
        called = True
        raise AssertionError("should not resolve when there is no code")

    monkeypatch.setattr(split.resolver, "resolve", _boom)
    plan = split.resolve_and_plan(None, SELLER)
    assert not plan.has_builder
    assert not called

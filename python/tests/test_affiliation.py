"""Tests for the Affiliation facade (affiliation.py) - no network.

Resolution is monkeypatched so these run offline; they check the facade wires the
underlying modules together correctly and keeps its safe-fallback contract.

    pytest test_affiliation.py -q
"""
from __future__ import annotations

import asyncio

import distribute
import push_split
import resolver
from affiliation import Affiliation
from builder_code import declare_builder_code

SELLER = "0x2222222222222222222222222222222222222222"
BUILDER = "0x1111111111111111111111111111111111111111"
SPLIT = "0x3773000000000000000000000000000000002e38"


class _FakeAdapter:
    def __init__(self, value):
        self._value = value

    def get_header(self, name):
        return self._value


class _FakeCtx:
    """Looks like an x402 HTTPRequestContext for header extraction."""

    def __init__(self, value):
        self.adapter = _FakeAdapter(value)


def _aff(**kw) -> Affiliation:
    Affiliation.clear_cache()  # payto._CACHE is process-global; isolate each test
    return Affiliation(app_code="bc_seller", seller_payout=SELLER, **kw)


# ── construction ──────────────────────────────────────────────────────────────

def test_requires_app_code_and_seller():
    import pytest

    with pytest.raises(ValueError):
        Affiliation(app_code="", seller_payout=SELLER)
    with pytest.raises(ValueError):
        Affiliation(app_code="bc_seller", seller_payout="")


def test_extensions_declares_app_code():
    aff = _aff()
    assert aff.extensions == declare_builder_code("bc_seller")
    assert aff.extensions["builder-code"]["info"]["a"] == "bc_seller"


def test_custom_share_sets_effective_share():
    assert _aff()._share == push_split.BUILDER_SHARE_BPS
    assert _aff(builder_share_bps=2500)._share == 2500


# ── code extraction from every source shape ───────────────────────────────────

def test_code_from_raw_string_takes_primary():
    assert _aff()._code_from("bc_alice,bc_bob") == "bc_alice"


def test_code_from_headers_mapping():
    aff = _aff()
    assert aff._code_from({Affiliation.HEADER: "bc_alice"}) == "bc_alice"


def test_code_from_x402_context():
    aff = _aff()
    assert aff._code_from(_FakeCtx("bc_alice")) == "bc_alice"
    assert aff._code_from(_FakeCtx(None)) is None


def test_code_from_none():
    assert _aff()._code_from(None) is None


# ── safe fallback: no/empty code → seller, no network ─────────────────────────

def test_no_code_falls_back_to_seller():
    aff = _aff()
    pt = aff.resolve(None)
    assert pt.address == SELLER
    assert pt.attributed is False
    assert aff.pay_to_for(None) == SELLER
    assert aff.pay_to_for({Affiliation.HEADER: ""}) == SELLER


def test_pay_to_async_callback_no_code():
    aff = _aff()
    addr = asyncio.run(aff.pay_to(_FakeCtx(None)))
    assert addr == SELLER


# ── attributed path (resolution monkeypatched) ────────────────────────────────

def _patch_registered(monkeypatch, *, deployed=False, balance=0):
    monkeypatch.setattr(
        resolver, "resolve",
        lambda code, **kw: {"code": code, "registered": True,
                            "owner": BUILDER, "payout_address": BUILDER},
    )
    monkeypatch.setattr(
        push_split, "predict_split_address",
        lambda plan, **kw: (SPLIT, deployed),
    )
    monkeypatch.setattr(
        distribute, "split_balance_units",
        lambda addr, **kw: balance,
    )


def test_resolve_registered_code_routes_to_split(monkeypatch):
    _patch_registered(monkeypatch)
    aff = _aff()
    pt = aff.resolve(_FakeCtx("bc_alice"))
    assert pt.address == SPLIT
    assert pt.attributed is True
    # 10% builder / 90% seller in the plan
    assert pt.plan.has_builder is True
    assert dict(pt.plan.recipients)[BUILDER] == push_split.BUILDER_SHARE_BPS


def test_pay_to_async_callback_registered(monkeypatch):
    _patch_registered(monkeypatch)
    aff = _aff()
    addr = asyncio.run(aff.pay_to(_FakeCtx("bc_alice")))
    assert addr == SPLIT


def test_balance_reads_split(monkeypatch):
    _patch_registered(monkeypatch, balance=12_400_000)
    assert _aff().balance("bc_alice") == 12_400_000


def test_balance_zero_when_unattributed(monkeypatch):
    monkeypatch.setattr(resolver, "resolve",
                        lambda code, **kw: {"registered": False, "payout_address": None})
    assert _aff().balance("bc_alice") == 0


def test_release_builds_deploy_and_distribute(monkeypatch):
    _patch_registered(monkeypatch, deployed=False, balance=12_400_000)
    calls, balance = _aff().release("bc_alice")
    assert balance == 12_400_000
    steps = [c.step for c in calls]
    assert steps == ["deploy_split", "distribute"]  # not deployed → both legs


def test_release_skips_deploy_when_already_deployed(monkeypatch):
    _patch_registered(monkeypatch, deployed=True, balance=5_000_000)
    calls, _ = _aff().release("bc_alice")
    assert [c.step for c in calls] == ["distribute"]


def test_splits_payload_shapes_rows_filters_marker_and_joins_rollup(monkeypatch):
    import monitor

    _patch_registered(monkeypatch, deployed=False, balance=1_000_000)
    # Discovery + rollup are CDP-backed; stub them. The shared marker rides along
    # as a "builder" and must be dropped; the app code + facilitator are already
    # filtered by discover_builder_codes.
    monkeypatch.setattr(monitor, "discover_builder_codes",
                        lambda app_code, **kw: ["bc_alice", "x402aff"])
    monkeypatch.setattr(monitor, "discover_split_rollup",
                        lambda app_code, **kw: {SPLIT.lower(): (3, 3_000_000)})

    payload = _aff().splits_payload()
    assert payload["configured"] is True
    assert payload["marker"] == "x402aff"
    assert payload["count"] == 1  # the marker row is dropped
    s = payload["splits"][0]
    assert s["payTo"] == SPLIT
    assert s["sellerCode"] == "bc_seller"
    assert s["builderCode"] == "bc_alice"
    assert s["payments"] == 3               # joined from the rollup
    assert s["receivedUnits"] == "3000000"
    assert s["balanceUnits"] == "1000000"
    assert s["deployed"] is False
    assert s["claimable"] is True
    assert [c["step"] for c in s["calls"]] == ["deploy_split", "distribute"]


def test_resolve_failure_falls_back_and_logs(monkeypatch, caplog):
    def _boom(code, **kw):
        raise RuntimeError("RPC 429")

    monkeypatch.setattr(resolver, "resolve", _boom)
    aff = _aff()
    with caplog.at_level("WARNING", logger="affiliation"):
        pt = aff.resolve(_FakeCtx("bc_alice"))
    assert pt.address == SELLER          # never raises - payment still works
    assert pt.attributed is False
    assert pt.error is not None
    assert any("resolve failed" in r.message for r in caplog.records)

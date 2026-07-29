"""Offline tests for the payout-discovery path (monitor.py).

monitor is the `python3 -m x402aff.monitor` CLI and the module Affiliation.scan
mirrors. It had no tests, which is how its `needs_distribution` copy drifted from
the one Affiliation uses - so these pin the claimability rule, the SQL discovery
filter, and the status assembly.

CDP (cdp_sql.run_query) and the three on-chain reads are monkeypatched; nothing
here touches the network.
"""
import pytest

from x402aff import cdp_sql, distribute, monitor, push_split, resolver, split

SELLER = "0x2222222222222222222222222222222222222222"
BUILDER = "0x1111111111111111111111111111111111111111"
SPLIT = "0x3773a1c4bb9dc9d0d0d8d3ba2e5e5f0c2e1a2e38"


# ── discovery: the SQL filter ─────────────────────────────────────────────────

def _rows(*codes):
    return [{"builder_code": c} for c in codes]


def test_discover_drops_our_own_code_and_the_facilitator(monkeypatch):
    monkeypatch.setattr(cdp_sql, "run_query",
                        lambda sql, max_age_ms=None: _rows("bc_alice", "bc_seller",
                                                           "cdp_facil_1", "bc_bob"))
    assert monitor.discover_builder_codes("bc_seller") == ["bc_alice", "bc_bob"]


def test_discover_skips_empty_rows(monkeypatch):
    monkeypatch.setattr(cdp_sql, "run_query",
                        lambda sql, max_age_ms=None: [{"builder_code": "bc_alice"},
                                                      {"builder_code": None}, {}])
    assert monitor.discover_builder_codes("bc_seller") == ["bc_alice"]


def test_discover_strips_quotes_from_the_app_code(monkeypatch):
    """The app code is interpolated into SQL, so quotes must not survive."""
    seen = {}

    def _run(sql, max_age_ms=None):
        seen["sql"] = sql
        return []

    monkeypatch.setattr(cdp_sql, "run_query", _run)
    monitor.discover_builder_codes("bc_x' OR 1=1 --")
    # The code lands inside exactly one pair of quotes, with none of its own.
    assert "'bc_x OR 1=1 --'" in seen["sql"]


def test_discover_days_is_coerced_to_an_int(monkeypatch):
    seen = {}

    def _run(sql, max_age_ms=None):
        seen["sql"] = sql
        return []

    monkeypatch.setattr(cdp_sql, "run_query", _run)
    monitor.discover_builder_codes("bc_seller", days=30)
    assert "INTERVAL 30 DAY" in seen["sql"]


# ── needs_distribution: the claimability rule ─────────────────────────────────
#
# NOT `distributable_units > 0`. A settled two-way split floors at a permanent
# 2 units, so gating on distributable re-reports it as pending forever and a
# keeper looping on it burns gas moving nothing.

def _status(balance, *, payout=BUILDER, share=1000):
    return monitor.SplitStatus("bc_alice", payout, SPLIT, True, balance, share)


def test_settled_split_at_its_permanent_floor_is_not_pending():
    s = _status(2)
    assert s.distributable_units == 1     # there IS a movable unit...
    assert s.needs_distribution is False  # ...but it floors to zero for everyone


def test_a_funded_split_is_pending():
    assert _status(1_000_000).needs_distribution is True


def test_unregistered_builder_is_never_pending():
    assert _status(1_000_000, payout=None).needs_distribution is False


def test_zero_balance_is_not_pending():
    assert _status(0).needs_distribution is False
    assert _status(0).distributable_units == 0


def test_needs_distribution_agrees_with_the_plan_it_describes():
    """monitor reconstructs the allocation vector rather than carrying the plan.

    Pin that the reconstruction matches what the real SplitPlan would say, so the
    two copies of this rule cannot drift again.
    """
    for balance in (0, 1, 2, 3, 11, 1_000_000):
        for share in (1000, 2500, 5000):
            plan = split.build_split_plan(SELLER, BUILDER, builder_code="bc_alice",
                                          builder_share_bps=share)
            assert _status(balance, share=share).needs_distribution is plan.is_claimable(balance)


def test_distributable_units_is_balance_minus_the_warm_unit():
    assert _status(11).distributable_units == 10


# ── status_for: assembly ──────────────────────────────────────────────────────

def _stub_chain(monkeypatch, *, registered=True, payout=BUILDER, deployed=False, balance=0):
    monkeypatch.setattr(resolver, "resolve",
                        lambda code, **kw: {"registered": registered, "payout_address": payout})
    monkeypatch.setattr(push_split, "predict_split_address", lambda plan, **kw: (SPLIT, deployed))
    monkeypatch.setattr(distribute, "split_balance_units", lambda addr, **kw: balance)


def test_status_for_unregistered_code_is_an_empty_row(monkeypatch):
    _stub_chain(monkeypatch, registered=False, payout=None)
    s = monitor.status_for("bc_ghost", seller_payout=SELLER)
    assert (s.builder_payout, s.split_address, s.deployed, s.balance_units) == (None, None, False, 0)
    assert s.needs_distribution is False


def test_status_for_registered_code_carries_the_split(monkeypatch):
    _stub_chain(monkeypatch, deployed=True, balance=5_000_000)
    s = monitor.status_for("bc_alice", seller_payout=SELLER)
    assert s.builder_code == "bc_alice"
    assert s.builder_payout == BUILDER
    assert s.split_address == SPLIT
    assert s.deployed is True
    assert s.balance_units == 5_000_000
    assert s.needs_distribution is True


def test_status_for_never_touches_the_network_for_an_unregistered_code(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("must not read the chain for an unregistered code")

    monkeypatch.setattr(resolver, "resolve", lambda code, **kw: {"registered": False})
    monkeypatch.setattr(push_split, "predict_split_address", _boom)
    monkeypatch.setattr(distribute, "split_balance_units", _boom)
    assert monitor.status_for("bc_ghost", seller_payout=SELLER).split_address is None


# ── scan: ordering ────────────────────────────────────────────────────────────

def test_scan_sorts_fullest_first(monkeypatch):
    monkeypatch.setattr(monitor, "discover_builder_codes",
                        lambda app_code, days=90: ["bc_small", "bc_big", "bc_mid"])
    balances = {"bc_small": 10, "bc_big": 9_000, "bc_mid": 500}
    monkeypatch.setattr(monitor, "status_for",
                        lambda code, **kw: monitor.SplitStatus(code, BUILDER, SPLIT, True,
                                                               balances[code], 1000))
    rows = monitor.scan("bc_seller", SELLER)
    assert [r.builder_code for r in rows] == ["bc_big", "bc_mid", "bc_small"]


def test_scan_with_no_builders_is_empty(monkeypatch):
    monkeypatch.setattr(monitor, "discover_builder_codes", lambda app_code, days=90: [])
    assert monitor.scan("bc_seller", SELLER) == []


# ── the share the module reports ──────────────────────────────────────────────

def test_share_bps_is_the_one_push_split_resolved():
    """Two independent env reads of X402_BUILDER_SHARE_BPS would let the address
    prediction and the claimability check disagree about the ratio."""
    assert monitor.SHARE_BPS == push_split.BUILDER_SHARE_BPS

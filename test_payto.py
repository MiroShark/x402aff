"""Offline tests for the request-time payTo path (payto.py + distribute.py).

Network-touching bits (resolve, predict_split_address, balanceOf) are monkey-
patched — these pin the *routing and safety* logic, not on-chain behaviour
(that's fork-test/CdpPath.t.sol).
"""
import distribute
import payto
import settler
import split

SELLER = "0x2222222222222222222222222222222222222222"
BUILDER = "0x1111111111111111111111111111111111111111"
SPLIT = "0x4d2ed5738370d64afc625362dcb3ae3a4b807817"


def setup_function():
    payto.clear_cache()


# ── header parsing ────────────────────────────────────────────────────────────

class _Headers:
    def __init__(self, d):
        self.d = d

    def get(self, k, default=None):
        return self.d.get(k, default)


def test_header_read_flask_style():
    assert payto.builder_code_from_headers(_Headers({"X-Builder-Code": "bc_alice"})) == "bc_alice"


def test_header_read_wsgi_environ():
    assert payto.builder_code_from_headers({"HTTP_X_BUILDER_CODE": "bc_bob"}) == "bc_bob"


def test_header_missing_is_none():
    assert payto.builder_code_from_headers(_Headers({})) is None
    assert payto.builder_code_from_headers(None) is None


def test_header_layered_codes_take_primary():
    # normalize_service_codes joins layered clients; v1 pays the first valid one.
    assert payto.builder_code_from_headers(_Headers({"X-Builder-Code": "bc_alice,bc_bob"})) == "bc_alice"


# ── payTo resolution ──────────────────────────────────────────────────────────

def test_no_code_is_seller_wallet_no_network(monkeypatch):
    # Must not touch the network at all when there's no code.
    monkeypatch.setattr(settler, "predict_split_address", _boom)
    r = payto.payto_for_request(None, seller_payout=SELLER)
    assert r.address == SELLER and not r.attributed and r.error is None


def test_registered_code_routes_to_split(monkeypatch):
    monkeypatch.setattr(split, "resolve_and_plan", _plan_with_builder)
    monkeypatch.setattr(settler, "predict_split_address", lambda plan, **k: (SPLIT, False))
    r = payto.payto_for_request("bc_alice", seller_payout=SELLER)
    assert r.attributed and r.address == SPLIT and not r.split_deployed
    assert r.plan.has_builder and r.plan.builder_payout == BUILDER


def test_unregistered_code_falls_back_to_seller(monkeypatch):
    monkeypatch.setattr(split, "resolve_and_plan",
                        lambda *a, **k: split.build_split_plan(SELLER, None, builder_code="bc_ghost"))
    monkeypatch.setattr(settler, "predict_split_address", _boom)
    r = payto.payto_for_request("bc_ghost", seller_payout=SELLER)
    assert r.address == SELLER and not r.attributed and r.error is None


def test_resolve_failure_never_breaks_the_paywall(monkeypatch):
    monkeypatch.setattr(split, "resolve_and_plan", _boom)
    r = payto.payto_for_request("bc_alice", seller_payout=SELLER)
    assert r.address == SELLER and not r.attributed
    assert r.error and "RuntimeError" in r.error


def test_predict_failure_falls_back(monkeypatch):
    monkeypatch.setattr(split, "resolve_and_plan", _plan_with_builder)
    monkeypatch.setattr(settler, "predict_split_address", _boom)
    r = payto.payto_for_request("bc_alice", seller_payout=SELLER)
    assert r.address == SELLER and not r.attributed and r.error


# ── caching ───────────────────────────────────────────────────────────────────

def test_success_is_cached_no_second_lookup(monkeypatch):
    calls = {"n": 0}

    def counting_plan(*a, **k):
        calls["n"] += 1
        return _plan_with_builder(*a, **k)

    monkeypatch.setattr(split, "resolve_and_plan", counting_plan)
    monkeypatch.setattr(settler, "predict_split_address", lambda plan, **k: (SPLIT, False))
    payto.payto_for_request("bc_alice", seller_payout=SELLER)
    payto.payto_for_request("bc_alice", seller_payout=SELLER)
    assert calls["n"] == 1, "second request should hit the cache"


def test_failure_is_not_cached(monkeypatch):
    monkeypatch.setattr(split, "resolve_and_plan", _boom)
    payto.payto_for_request("bc_alice", seller_payout=SELLER)
    # A transient RPC failure must not strand the builder for the process life:
    # next call retries (here it succeeds).
    monkeypatch.setattr(split, "resolve_and_plan", _plan_with_builder)
    monkeypatch.setattr(settler, "predict_split_address", lambda plan, **k: (SPLIT, True))
    r = payto.payto_for_request("bc_alice", seller_payout=SELLER)
    assert r.attributed and r.split_deployed


# ── distribute leg selection ──────────────────────────────────────────────────

def test_distribute_calls_include_deploy_when_undeployed():
    plan = split.build_split_plan(SELLER, BUILDER, builder_code="bc_alice")
    calls = distribute.distribute_calls(plan, split_address=SPLIT, deployed=False)
    assert [c.step for c in calls] == ["deploy_split", "distribute"]
    assert all(c.data.startswith("0x") for c in calls)


def test_distribute_calls_skip_deploy_when_deployed():
    plan = split.build_split_plan(SELLER, BUILDER, builder_code="bc_alice")
    calls = distribute.distribute_calls(plan, split_address=SPLIT, deployed=True)
    assert [c.step for c in calls] == ["distribute"]


def test_builderless_plan_has_nothing_to_distribute():
    plan = split.build_split_plan(SELLER, None)
    assert distribute.distribute_calls(plan, split_address=SPLIT, deployed=False) == []


def test_distribute_calldata_matches_settler():
    # distribute.py must emit byte-identical calldata to the audited settler path.
    plan = split.build_split_plan(SELLER, BUILDER, builder_code="bc_alice")
    calls = distribute.distribute_calls(plan, split_address=SPLIT, deployed=True)
    assert calls[0].data == settler.distribute_calldata(plan)


# ── helpers ───────────────────────────────────────────────────────────────────

def _plan_with_builder(*a, **k):
    return split.build_split_plan(
        SELLER, BUILDER, builder_code="bc_alice",
        builder_share_bps=settler.BUILDER_SHARE_BPS,
    )


def _boom(*a, **k):
    raise RuntimeError("simulated RPC failure")

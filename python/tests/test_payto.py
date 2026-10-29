"""Offline tests for the request-time payTo path (payto.py + distribute.py).

Network-touching bits (resolve, predict_split_address, balanceOf) are monkey-
patched - these pin the *routing and safety* logic, not on-chain behaviour
(that's fork-test/CdpPath.t.sol).
"""
from x402aff import distribute, payto, push_split, split

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
    monkeypatch.setattr(push_split, "predict_split_address", _boom)
    r = payto.payto_for_request(None, seller_payout=SELLER)
    assert r.address == SELLER and not r.attributed and r.error is None


def test_registered_code_routes_to_split(monkeypatch):
    monkeypatch.setattr(split, "resolve_and_plan", _plan_with_builder)
    monkeypatch.setattr(push_split, "predict_split_address", lambda plan, **k: (SPLIT, False))
    r = payto.payto_for_request("bc_alice", seller_payout=SELLER)
    assert r.attributed and r.address == SPLIT and not r.split_deployed
    assert r.plan.has_builder and r.plan.builder_payout == BUILDER


def test_unregistered_code_falls_back_to_seller(monkeypatch):
    monkeypatch.setattr(split, "resolve_and_plan",
                        lambda *a, **k: split.build_split_plan(SELLER, None, builder_code="bc_ghost"))
    monkeypatch.setattr(push_split, "predict_split_address", _boom)
    r = payto.payto_for_request("bc_ghost", seller_payout=SELLER)
    assert r.address == SELLER and not r.attributed and r.error is None


def test_resolve_failure_never_breaks_the_paywall(monkeypatch):
    monkeypatch.setattr(split, "resolve_and_plan", _boom)
    r = payto.payto_for_request("bc_alice", seller_payout=SELLER)
    assert r.address == SELLER and not r.attributed
    assert r.error and "RuntimeError" in r.error


def test_predict_failure_falls_back(monkeypatch):
    monkeypatch.setattr(split, "resolve_and_plan", _plan_with_builder)
    monkeypatch.setattr(push_split, "predict_split_address", _boom)
    r = payto.payto_for_request("bc_alice", seller_payout=SELLER)
    assert r.address == SELLER and not r.attributed and r.error


# ── caching ───────────────────────────────────────────────────────────────────

def test_success_is_cached_no_second_lookup(monkeypatch):
    calls = {"n": 0}

    def counting_plan(*a, **k):
        calls["n"] += 1
        return _plan_with_builder(*a, **k)

    monkeypatch.setattr(split, "resolve_and_plan", counting_plan)
    monkeypatch.setattr(push_split, "predict_split_address", lambda plan, **k: (SPLIT, False))
    payto.payto_for_request("bc_alice", seller_payout=SELLER)
    payto.payto_for_request("bc_alice", seller_payout=SELLER)
    assert calls["n"] == 1, "second request should hit the cache"


def test_failure_is_not_cached(monkeypatch):
    monkeypatch.setattr(split, "resolve_and_plan", _boom)
    payto.payto_for_request("bc_alice", seller_payout=SELLER)
    # A transient RPC failure must not strand the builder for the process life:
    # next call retries (here it succeeds).
    monkeypatch.setattr(split, "resolve_and_plan", _plan_with_builder)
    monkeypatch.setattr(push_split, "predict_split_address", lambda plan, **k: (SPLIT, True))
    r = payto.payto_for_request("bc_alice", seller_payout=SELLER)
    assert r.attributed and r.split_deployed


def test_unregistered_repeat_is_served_from_negative_cache(monkeypatch):
    # A repeated unregistered code must not re-hit the registry every request -
    # that flood is the cost-amplification vector. (predict is _boom to prove we
    # never predict an address for an empty plan.)
    calls = {"n": 0}

    def counting_unregistered(*a, **k):
        calls["n"] += 1
        return split.build_split_plan(SELLER, None, builder_code="bc_late")

    monkeypatch.setattr(split, "resolve_and_plan", counting_unregistered)
    monkeypatch.setattr(push_split, "predict_split_address", _boom)
    first = payto.payto_for_request("bc_late", seller_payout=SELLER)
    second = payto.payto_for_request("bc_late", seller_payout=SELLER)
    assert first.address == SELLER and not first.attributed
    assert second.address == SELLER and not second.attributed
    assert calls["n"] == 1, "second request should be served from the negative cache"


def test_unregistered_negative_cache_expires_and_picks_up_registration(monkeypatch):
    # The negative cache is short-lived: after the TTL the miss is re-checked, so a
    # builder who registers later is picked up (bounded by the TTL, not stranded).
    clock = {"t": 1000.0}
    monkeypatch.setattr(payto.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(
        split, "resolve_and_plan",
        lambda *a, **k: split.build_split_plan(SELLER, None, builder_code="bc_late"),
    )
    monkeypatch.setattr(push_split, "predict_split_address", _boom)
    first = payto.payto_for_request("bc_late", seller_payout=SELLER)
    assert first.address == SELLER and not first.attributed

    # TTL elapses; the builder has since registered.
    clock["t"] += payto.NEG_CACHE_TTL_S + 1
    monkeypatch.setattr(split, "resolve_and_plan", _plan_with_builder)
    monkeypatch.setattr(push_split, "predict_split_address", lambda plan, **k: (SPLIT, False))
    second = payto.payto_for_request("bc_late", seller_payout=SELLER)
    assert second.attributed and second.address == SPLIT


# ── payout screen, end to end through a fake Base RPC ─────────────────────────
#
# A PushSplit is ownerless and pays every recipient in one distribute, so a
# builder payout USDC refuses (a blacklisted address, or USDC's own address,
# which Base USDC reports as blacklisted) makes every distribute revert and
# strands the whole payment, the seller's share too. These drive the real
# resolver -> split -> payto path against a fake JSON-RPC (registry, USDC and
# factory) and check such a payout never becomes the advertised split.

USDC = push_split.USDC_BASE
BLACKLISTED = "0x0000000000000000000000000000000000000bad"
GOOD = "0x00000000000000000000000000000000000600d0"
OWNER = "0x00000000000000000000000000000000000a77ac"


def _fake_rpc(monkeypatch, payout, *, blacklisted=(BLACKLISTED, USDC), usdc_error=None):
    """Answer eth_calls like Base would for a code registered to `payout`.
    Returns the list of (to, selector) calls seen."""
    seen = []
    listed = {a.lower() for a in blacklisted}

    class _Resp:
        def __init__(self, body):
            self._body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self._body

    def _word(addr):
        return "0x" + addr[2:].lower().rjust(64, "0")

    def _post(url, json, timeout):
        call = json["params"][0]
        to, data = call["to"].lower(), call["data"]
        seen.append((to, data[:10]))
        if to == payto.resolver.BUILDER_CODES_REGISTRY.lower():
            return _Resp({"result": _word(OWNER if data[2:10] == "6352211e" else payout)})
        if to == USDC.lower():
            if usdc_error:
                return _Resp({"error": usdc_error})
            arg = "0x" + data[-40:]
            return _Resp({"result": "0x" + ("1" if arg in listed else "0").rjust(64, "0")})
        if to == push_split.SPLITS_PUSH_FACTORY.lower():
            return _Resp({"result": _word(SPLIT) + "0" * 64})
        raise AssertionError(f"unexpected eth_call to {to}")

    monkeypatch.setattr(payto.resolver.requests, "post", _post)
    return seen


def test_good_payout_still_routes_to_the_split(monkeypatch):
    seen = _fake_rpc(monkeypatch, GOOD)
    r = payto.payto_for_request("bc_alice", seller_payout=SELLER)
    assert r.status == payto.STATUS_RESOLVED and r.attributed
    assert r.address.lower() == SPLIT
    assert dict(r.plan.recipients) == {GOOD: 1000, SELLER: 9000}
    # the payout was screened against USDC before the split was predicted
    assert (USDC.lower(), "0xfe575a87") in seen


def test_blacklisted_payout_pays_the_seller_in_full(monkeypatch):
    seen = _fake_rpc(monkeypatch, BLACKLISTED)
    r = payto.payto_for_request("bc_alice", seller_payout=SELLER)
    assert r.status == payto.STATUS_BLOCKED
    assert r.address == SELLER and not r.attributed and r.error is None
    assert r.plan.recipients == [(SELLER, split.BPS_DENOM)] and r.plan.payout_blocked
    # no split address is ever predicted for it
    assert not any(to == push_split.SPLITS_PUSH_FACTORY.lower() for to, _ in seen)


def test_usdc_contract_as_payout_pays_the_seller_in_full(monkeypatch):
    # Blocked by the static check alone, so even a node that answered
    # isBlacklisted(USDC) wrongly could not let it through.
    seen = _fake_rpc(monkeypatch, USDC, blacklisted=())
    r = payto.payto_for_request("bc_alice", seller_payout=SELLER)
    assert r.status == payto.STATUS_BLOCKED
    assert r.address == SELLER and not r.attributed
    assert not any(to == USDC.lower() for to, _ in seen), "no RPC needed for USDC itself"


def test_blocked_code_is_served_from_the_negative_cache(monkeypatch):
    seen = _fake_rpc(monkeypatch, BLACKLISTED)
    payto.payto_for_request("bc_alice", seller_payout=SELLER)
    n = len(seen)
    again = payto.payto_for_request("bc_alice", seller_payout=SELLER)
    assert again.status == payto.STATUS_BLOCKED and again.address == SELLER
    assert again.plan.payout_blocked
    assert len(seen) == n, "a repeated blocked code must not re-hit the RPC"


def test_blacklist_read_failure_pays_the_seller_and_reports_it(monkeypatch):
    _fake_rpc(monkeypatch, GOOD, usdc_error={"code": -32005, "message": "limit exceeded"})
    r = payto.payto_for_request("bc_alice", seller_payout=SELLER)
    assert r.status == payto.STATUS_ERROR
    assert r.address == SELLER and not r.attributed
    assert r.error and "USDC isBlacklisted" in r.error


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


def test_distribute_calldata_matches_push_split():
    # distribute.py must emit byte-identical calldata to the audited push_split encoding.
    plan = split.build_split_plan(SELLER, BUILDER, builder_code="bc_alice")
    calls = distribute.distribute_calls(plan, split_address=SPLIT, deployed=True)
    assert calls[0].data == push_split.distribute_calldata(plan)


# ── helpers ───────────────────────────────────────────────────────────────────

def _plan_with_builder(*a, **k):
    return split.build_split_plan(
        SELLER, BUILDER, builder_code="bc_alice",
        builder_share_bps=push_split.BUILDER_SHARE_BPS,
    )


def _boom(*a, **k):
    raise RuntimeError("simulated RPC failure")

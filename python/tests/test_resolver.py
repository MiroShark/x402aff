"""Offline tests for the registry resolve leg (resolver.py).

`requests.post` is monkeypatched - these pin how a JSON-RPC answer is classified,
not on-chain behaviour. The distinction under test is the load-bearing one: a
*revert* means the code is unregistered, anything else means the lookup failed
and must surface rather than masquerade as "no builder".
"""
import pytest

from x402aff import resolver

CODE = "bc_alice"
PAYOUT = "0x" + "1" * 40


def _word(addr_hex: str) -> str:
    """A 32-byte return word carrying a 20-byte address in its low bytes."""
    return "0x" + "0" * 24 + addr_hex[2:]


class _Resp:
    def __init__(self, body, status_ok=True):
        self._body = body
        self._ok = status_ok

    def raise_for_status(self):
        if not self._ok:
            raise RuntimeError("HTTP 500")

    def json(self):
        return self._body


def _post_returning(*bodies):
    """Stub requests.post, answering each successive call from `bodies`."""
    seq = list(bodies)

    def _post(url, json, timeout):
        return _Resp(seq.pop(0) if len(seq) > 1 else seq[0])

    return _post


# ── token id ──────────────────────────────────────────────────────────────────

def test_to_token_id_round_trips():
    assert resolver.to_code(resolver.to_token_id(CODE)) == CODE


def test_to_token_id_rejects_malformed():
    with pytest.raises(ValueError):
        resolver.to_token_id("BAD CODE")


# ── revert vs transport failure ───────────────────────────────────────────────
#
# The whole point of this file. Before this split existed, every JSON-RPC error
# collapsed into "unregistered": a rate-limited RPC routed the payment to the
# seller unsplit with PayTo.error unset, so nothing logged and the builder lost
# their cut silently.

def test_revert_by_eip1474_code_means_unregistered(monkeypatch):
    monkeypatch.setattr(resolver.requests, "post",
                        _post_returning({"error": {"code": 3, "message": "execution reverted"}}))
    assert resolver.resolve(CODE)["registered"] is False


def test_revert_by_message_only_means_unregistered(monkeypatch):
    # Some nodes signal a revert only in the message, with a generic code.
    monkeypatch.setattr(resolver.requests, "post",
                        _post_returning({"error": {"code": -32000, "message": "execution reverted"}}))
    assert resolver.resolve(CODE)["registered"] is False


def test_empty_result_means_unregistered(monkeypatch):
    monkeypatch.setattr(resolver.requests, "post", _post_returning({"result": "0x"}))
    assert resolver.resolve(CODE)["registered"] is False


def test_rate_limit_raises_instead_of_looking_unregistered(monkeypatch):
    monkeypatch.setattr(resolver.requests, "post",
                        _post_returning({"error": {"code": -32005, "message": "limit exceeded"}}))
    with pytest.raises(RuntimeError, match="registry eth_call failed"):
        resolver.resolve(CODE)


def test_node_internal_error_raises(monkeypatch):
    monkeypatch.setattr(resolver.requests, "post",
                        _post_returning({"error": {"code": -32603, "message": "internal error"}}))
    with pytest.raises(RuntimeError, match="registry eth_call failed"):
        resolver.resolve(CODE)


def test_a_raised_lookup_still_never_breaks_the_402(monkeypatch):
    """The 402 contract holds: payto catches the raise and records it."""
    from x402aff import payto

    payto.clear_cache()
    monkeypatch.setattr(resolver.requests, "post",
                        _post_returning({"error": {"code": -32005, "message": "limit exceeded"}}))
    seller = "0x" + "2" * 40
    pt = payto.payto_for_request(CODE, seller_payout=seller, use_cache=False)
    assert pt.address == seller          # still pays, just unsplit
    assert pt.attributed is False
    assert pt.error and "limit exceeded" in pt.error   # and it is no longer silent


# ── happy path ────────────────────────────────────────────────────────────────

def test_registered_code_returns_owner_and_payout(monkeypatch):
    owner_word = _word("0x" + "a" * 40)
    monkeypatch.setattr(resolver.requests, "post",
                        _post_returning({"result": owner_word}, {"result": _word(PAYOUT)}))
    info = resolver.resolve(CODE)
    assert info["registered"] is True
    assert info["payout_address"] == PAYOUT
    assert info["code"] == CODE


def test_zero_owner_word_means_unregistered(monkeypatch):
    monkeypatch.setattr(resolver.requests, "post", _post_returning({"result": _word("0x" + "0" * 40)}))
    assert resolver.resolve(CODE)["registered"] is False


# ── the RPC default ───────────────────────────────────────────────────────────

def test_resolve_defaults_to_the_env_aware_rpc(monkeypatch):
    """X402_BASE_RPC must reach the resolve leg, not just the factory leg.

    This leg used to default to the public endpoint regardless, so a seller who
    set a paid RPC was still rate-limited on the one lookup whose failure costs
    the builder their cut.
    """
    from x402aff import push_split

    seen = {}

    def _post(url, json, timeout):
        seen["url"] = url
        return _Resp({"result": "0x"})

    monkeypatch.setattr(resolver.requests, "post", _post)
    resolver.resolve(CODE)
    assert seen["url"] == resolver.BASE_RPC == push_split.BASE_RPC

"""Tests for the 0xSplits v2 PushSplit calldata layer (push_split.py) - no network.

The expected calldata below was generated with foundry's `cast calldata` and the
predicted address cross-checked LIVE against the Base PushSplitFactory
(factory.isDeployed → 0x4eDe36f2e215A06856612D4B98Afa56c3aFfFA66, not deployed).

    pytest test_push_split.py -q
"""
from __future__ import annotations

from x402aff import push_split, split

BUILDER = "0x1111111111111111111111111111111111111111"
SELLER = "0x2222222222222222222222222222222222222222"

# cast calldata "isDeployed((address[],uint256[],uint256,uint16),address,bytes32)" \
#   "([0x1111…,0x2222…],[1000,9000],10000,0)" 0x0000…0000 0x0000…0000
CAST_IS_DEPLOYED = (
    "0xcd6bc121"
    + "0000000000000000000000000000000000000000000000000000000000000060"
    + "0000000000000000000000000000000000000000000000000000000000000000"
    + "0000000000000000000000000000000000000000000000000000000000000000"
    + "0000000000000000000000000000000000000000000000000000000000000080"
    + "00000000000000000000000000000000000000000000000000000000000000e0"
    + "0000000000000000000000000000000000000000000000000000000000002710"
    + "0000000000000000000000000000000000000000000000000000000000000000"
    + "0000000000000000000000000000000000000000000000000000000000000002"
    + "0000000000000000000000001111111111111111111111111111111111111111"
    + "0000000000000000000000002222222222222222222222222222222222222222"
    + "0000000000000000000000000000000000000000000000000000000000000002"
    + "00000000000000000000000000000000000000000000000000000000000003e8"
    + "0000000000000000000000000000000000000000000000000000000000002328"
)


def _plan() -> split.SplitPlan:
    return split.build_split_plan(SELLER, BUILDER, builder_code="bc_alice")


def test_factory_is_the_confirmed_base_address():
    # PushSplitFactory V2.2 - confirmed by Splits (2026-07-15), verified on Basescan.
    assert push_split.SPLITS_PUSH_FACTORY == "0x8E8eB0cC6AE34A38B67D5Cf91ACa38f60bc3Ecf4"


def test_is_deployed_calldata_matches_cast_byte_for_byte():
    assert push_split.is_deployed_calldata(_plan()) == CAST_IS_DEPLOYED


def test_create_calldata_same_params_different_head():
    data = push_split.create_split_calldata(_plan())
    assert data.startswith("0x" + push_split._SEL_CREATE_DET)
    # same encoded Split tuple as isDeployed (lands on the predicted address)
    assert data.endswith(push_split.encode_split_params(_plan()))


def test_distribute_calldata_targets_usdc():
    data = push_split.distribute_calldata(_plan(), distributor=SELLER)
    assert data.startswith("0x" + push_split._SEL_DISTRIBUTE)
    assert push_split.USDC_BASE.lower().removeprefix("0x") in data.lower()
    assert data.endswith(push_split.encode_split_params(_plan()))


def test_distribute_incentive_is_zero():
    # The caller of distribute earns nothing - the split's distributionIncentive
    # field (last word of the encoded params before the arrays) is 0.
    params = push_split.encode_split_params(_plan())
    # head = [recips_off, allocs_off, totalAllocation, distributionIncentive]
    incentive_word = params[3 * 64:4 * 64]
    assert int(incentive_word, 16) == 0


def test_decode_is_deployed():
    result = ("0x"
              + "0000000000000000000000004ede36f2e215a06856612d4b98afa56c3afffa66"
              + "0000000000000000000000000000000000000000000000000000000000000000")
    addr, exists = push_split._decode_is_deployed(result)
    assert addr == "0x4ede36f2e215a06856612d4b98afa56c3afffa66"
    assert exists is False
    _, exists = push_split._decode_is_deployed(result[:66] + "0" * 63 + "1")
    assert exists is True


def test_predict_split_address_parses_rpc_result(monkeypatch):
    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"result": ("0x"
                     + "0000000000000000000000004ede36f2e215a06856612d4b98afa56c3afffa66"
                     + "0000000000000000000000000000000000000000000000000000000000000000")}

    def _post(url, json, timeout):
        captured["to"] = json["params"][0]["to"]
        captured["data"] = json["params"][0]["data"]
        return _Resp()

    monkeypatch.setattr(push_split.requests, "post", _post)
    addr, exists = push_split.predict_split_address(_plan())
    assert addr == "0x4ede36f2e215a06856612d4b98afa56c3afffa66"
    assert exists is False
    assert captured["to"] == push_split.SPLITS_PUSH_FACTORY

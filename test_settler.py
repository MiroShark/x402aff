"""Tests for the Splits v2 calldata layer (settler.py) — pure, no network.

The expected calldata below was generated with foundry's `cast calldata` and the
predicted address cross-checked LIVE against the Base PushSplitFactory
(factory.isDeployed → 0x4eDe36f2e215A06856612D4B98Afa56c3aFfFA66, not deployed).

    pytest test_settler.py -q
"""
from __future__ import annotations

import settler
import split

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
    # PushSplitFactory V2.2 — confirmed by Splits (2026-07-15), verified on Basescan.
    assert settler.SPLITS_PUSH_FACTORY == "0x8E8eB0cC6AE34A38B67D5Cf91ACa38f60bc3Ecf4"


def test_is_deployed_calldata_matches_cast_byte_for_byte():
    assert settler.is_deployed_calldata(_plan()) == CAST_IS_DEPLOYED


def test_create_calldata_same_params_different_head():
    data = settler.create_split_calldata(_plan())
    assert data.startswith("0x" + settler._SEL_CREATE_DET)
    # createSplitDeterministic(Split, owner, creator, salt): tuple offset 0x80,
    # then owner/creator/salt zero-words, then the SAME Split tuple as isDeployed.
    assert data.endswith(settler.encode_split_params(_plan()))
    head = data[10:]
    assert head[:64] == f"{0x80:064x}"


def test_distribute_calldata_targets_usdc():
    data = settler.distribute_calldata(_plan(), distributor=SELLER)
    assert data.startswith("0x" + settler._SEL_DISTRIBUTE)
    assert settler.USDC_BASE.lower().removeprefix("0x") in data.lower()
    assert data.endswith(settler.encode_split_params(_plan()))


def test_decode_is_deployed():
    # The live return words for the vector above (predicted addr, exists=false).
    result = (
        "0x0000000000000000000000004ede36f2e215a06856612d4b98afa56c3afffa66"
        + "0000000000000000000000000000000000000000000000000000000000000000"
    )
    addr, exists = settler._decode_is_deployed(result)
    assert addr == "0x4ede36f2e215a06856612d4b98afa56c3afffa66"
    assert exists is False
    _, exists = settler._decode_is_deployed(result[:66] + "0" * 63 + "1")
    assert exists is True


def test_settlement_calls_carry_real_calldata():
    calls = settler.settlement_calls(
        _plan(), amount_usdc=1.00, buyer_from="0xBuyer",
        split_address="0x4eDe36f2e215A06856612D4B98Afa56c3aFfFA66", deployed=False,
    )
    assert [c.step for c in calls] == ["deploy_split", "pull_funds", "distribute"]
    deploy, pull, dist = calls
    assert deploy.target == settler.SPLITS_PUSH_FACTORY and deploy.data
    assert pull.target == settler.USDC_BASE and pull.data is None  # buyer-sig leg
    assert dist.target == "0x4eDe36f2e215A06856612D4B98Afa56c3aFfFA66" and dist.data


def test_settlement_calls_skip_deploy_when_split_exists():
    calls = settler.settlement_calls(
        _plan(), amount_usdc=1.00, buyer_from="0xBuyer",
        split_address="0x4eDe36f2e215A06856612D4B98Afa56c3aFfFA66", deployed=True,
    )
    assert [c.step for c in calls] == ["pull_funds", "distribute"]


def test_no_builder_is_still_a_plain_transfer():
    plan = split.build_split_plan(SELLER, None)
    calls = settler.settlement_calls(plan, amount_usdc=1.00, buyer_from="0xBuyer")
    assert [c.step for c in calls] == ["pull_funds"]
    assert calls[0].target == settler.USDC_BASE


def test_predict_split_address_parses_rpc_result(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"result": (
                "0x0000000000000000000000004ede36f2e215a06856612d4b98afa56c3afffa66"
                + "0000000000000000000000000000000000000000000000000000000000000000"
            )}

    captured = {}

    def _post(url, json=None, timeout=None):
        captured["to"] = json["params"][0]["to"]
        captured["data"] = json["params"][0]["data"]
        return _Resp()

    monkeypatch.setattr(settler.requests, "post", _post)
    addr, exists = settler.predict_split_address(_plan())
    assert addr == "0x4ede36f2e215a06856612d4b98afa56c3afffa66"
    assert exists is False
    assert captured["to"] == settler.SPLITS_PUSH_FACTORY
    assert captured["data"] == CAST_IS_DEPLOYED

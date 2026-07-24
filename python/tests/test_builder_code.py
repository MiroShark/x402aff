"""Tests for the declare + decode helpers.

Run:  pip install cbor2 pytest && pytest test_builder_code.py

These prove the wire shape and the on-chain suffix round-trip. They do NOT prove
real on-chain attribution - that needs a real Base-mainnet settlement verified
against the calldata suffix (see README → Verify).
"""
import pytest

from x402aff.builder_code import (
    AFFILIATION_MARKER,
    BUILDER_CODE_KEY,
    ERC_8021_MARKER,
    declare_builder_code,
    marked_service_codes,
    normalize_builder_code,
    normalize_service_codes,
    parse_builder_code_suffix,
)

# --- declare_builder_code: the "declare it" step ------------------------------

def test_declares_app_code_under_correct_key():
    ext = declare_builder_code("bc_yourcode")
    assert set(ext) == {BUILDER_CODE_KEY}
    assert ext[BUILDER_CODE_KEY]["info"] == {"a": "bc_yourcode"}
    schema = ext[BUILDER_CODE_KEY]["schema"]
    assert set(schema["properties"]) == {"a", "w", "s"}
    assert schema["additionalProperties"] is False


def test_strips_whitespace():
    assert declare_builder_code("  bc_yourcode  ")[BUILDER_CODE_KEY]["info"]["a"] == "bc_yourcode"


@pytest.mark.parametrize("bad", ["", "BC_UPPER", "bc-dash", "x" * 33, "bc code"])
def test_rejects_malformed_codes(bad):
    with pytest.raises(ValueError):
        declare_builder_code(bad)


# --- normalizers: sanitize codes read off the wire ----------------------------

def test_normalize_builder_code_accepts_and_trims():
    assert normalize_builder_code("  bc_abc12345  ") == "bc_abc12345"


@pytest.mark.parametrize("bad", ["", "BC_UPPER", "bc-dash", "x" * 33, None, 123, ["bc_x"]])
def test_normalize_builder_code_rejects(bad):
    assert normalize_builder_code(bad) is None  # never raises


def test_normalize_service_codes_list_joins_valid():
    assert normalize_service_codes(["bc_one", "bc_two"]) == "bc_one,bc_two"


def test_normalize_service_codes_accepts_bare_string():
    assert normalize_service_codes("bc_abc12345") == "bc_abc12345"


def test_normalize_service_codes_dedupes_and_drops_invalid():
    assert normalize_service_codes(["bc_one", "BC_BAD", "bc_one", "bc_two"]) == "bc_one,bc_two"


@pytest.mark.parametrize("empty", [None, [], ["BAD!"], "not a code", [""]])
def test_normalize_service_codes_empty_to_none(empty):
    assert normalize_service_codes(empty) is None


# --- marked_service_codes: the buyer-side `s` -------------------------------

def test_marked_service_codes_appends_the_shared_marker():
    assert marked_service_codes("bc_yourcode") == ["bc_yourcode", AFFILIATION_MARKER]


def test_marked_service_codes_keeps_the_real_code_primary():
    # The split pays the FIRST code, so the marker must never lead.
    assert marked_service_codes("bc_yourcode")[0] == "bc_yourcode"


def test_marked_service_codes_never_duplicates_the_marker():
    assert marked_service_codes(AFFILIATION_MARKER) == [AFFILIATION_MARKER]


def test_buyer_extension_stamps_exactly_marked_service_codes():
    """The extension must not re-implement the rule - one source of truth."""
    from x402aff.buyer_client import BuilderCodeClientExtension

    class _Payload:
        extensions = None

        def model_copy(self, *, update):
            return update["extensions"]

    stamped = BuilderCodeClientExtension("bc_yourcode").enrich_payment_payload(_Payload(), None)
    assert stamped["builder-code"]["info"]["s"] == marked_service_codes("bc_yourcode")


# --- parse_builder_code_suffix: the "recover w from chain" step ---------------

def _make_suffix(**codes) -> bytes:
    """Build a Schema 2 suffix the way the facilitator does."""
    import cbor2

    cbor = cbor2.dumps(codes)
    return cbor + len(cbor).to_bytes(2, "big") + bytes([2]) + ERC_8021_MARKER


def test_parse_recovers_app_and_wallet_codes():
    suffix = _make_suffix(a="bc_yourcode", w="cdp_facil1")
    calldata = "0x" + ("ab" * 120) + suffix.hex()
    assert parse_builder_code_suffix(calldata) == {"a": "bc_yourcode", "w": "cdp_facil1"}


def test_parse_recovers_service_codes_array():
    suffix = _make_suffix(s=["bc_client1", "bc_client2"])
    assert parse_builder_code_suffix(("00" * 40) + suffix.hex()) == {"s": ["bc_client1", "bc_client2"]}


def test_parse_all_three_fields():
    suffix = _make_suffix(a="bc_app", w="cdp_facil1", s=["bc_client"])
    assert parse_builder_code_suffix("0x" + suffix.hex()) == {
        "a": "bc_app", "w": "cdp_facil1", "s": ["bc_client"]
    }


@pytest.mark.parametrize("calldata", ["0xa9059cbb" + "00" * 60, "0x", "0xdeadbeef", "not hex", ""])
def test_parse_no_suffix_returns_none(calldata):
    assert parse_builder_code_suffix(calldata) is None


def test_parse_accepts_raw_bytes():
    assert parse_builder_code_suffix(b"\x00\x00" + _make_suffix(w="cdp_facil1")) == {"w": "cdp_facil1"}

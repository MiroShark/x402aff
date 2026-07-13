"""Base Builder Code (ERC-8021 Schema 2) helpers for x402 — declare & decode.

This is the one framework-agnostic, reusable piece of the whole affiliation
system. It does two jobs and has no web framework or database dependency:

  1. declare_builder_code(app_code)  — build the extension dict you attach to
     your paid x402 route so every payment is stamped with YOUR app code ("a").
  2. parse_builder_code_suffix(calldata) — decode the ERC-8021 suffix the
     facilitator appended to a settlement transaction, so you can read back the
     authoritative on-chain codes (most importantly "w", the facilitator wallet
     code, which only exists after settlement).

Plus two normalizers used to sanitize codes read off the wire before they touch
a database.

Why hand-rolled? The x402 *Python* SDK ships no builder-code module (TS/Go
only), so the resource server has to build the declaration and decode the suffix
itself. On TypeScript you'd use `@x402/extensions/builder-code` instead.

Spec:  https://github.com/x402-foundation/x402/blob/main/specs/extensions/builder_code.md
CDP:   https://docs.cdp.coinbase.com/x402/core-concepts/builder-codes

The only third-party dependency is `cbor2`, and only for parse_builder_code_suffix
(the on-chain decode path). Declaring a code needs no dependencies at all.
"""
from __future__ import annotations

import re
from typing import Any

# Extension key, matching the x402 TS SDK (@x402/extensions/builder-code).
BUILDER_CODE_KEY = "builder-code"

# Every builder code (a / w / s) is 1-32 lowercase letters, digits, underscores.
BUILDER_CODE_PATTERN = r"^[a-z0-9_]{1,32}$"

# ERC-8021 Schema 2 trailer (16 bytes) that closes every attribution suffix.
# Read from the END of the settlement calldata, the suffix is laid out as:
#   [cbor_data][suffix_data_length (2 bytes, big-endian)][schema_id = 0x02][marker (16 bytes)]
ERC_8021_MARKER = bytes.fromhex("80218021802180218021802180218021")
_SCHEMA_2_ID = 2

# JSON Schema for the three ERC-8021 Schema 2 fields. Rides in the base64'd 402
# header, so it is kept minimal.
BUILDER_CODE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "a": {"type": "string", "pattern": BUILDER_CODE_PATTERN},  # app code (yours)
        "w": {"type": "string", "pattern": BUILDER_CODE_PATTERN},  # facilitator wallet code
        "s": {                                                     # service/referer code(s)
            "type": "array",
            "items": {"type": "string", "pattern": BUILDER_CODE_PATTERN},
        },
    },
    "additionalProperties": False,
}


def normalize_builder_code(code: Any) -> str | None:
    """Return ``code`` trimmed if it's a single valid builder code, else None.

    Use it to sanitize a code read back off the wire (your declared ``a``, a
    facilitator ``w``) before storing it. Never raises.
    """
    if not isinstance(code, str):
        return None
    code = code.strip()
    return code if re.match(BUILDER_CODE_PATTERN, code) else None


def normalize_service_codes(raw: Any) -> str | None:
    """Normalize a submitted ``s`` field to a comma-joined string of codes.

    The buyer's client extension sends ``s`` as a list (one entry per layered
    client), but it can also arrive as a bare string. Returns the valid,
    de-duplicated codes joined by ``,`` (order preserved), or None when empty —
    ready to drop in a single TEXT/VARCHAR column. Invalid entries are dropped.
    """
    if raw is None:
        return None
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    valid: list[str] = []
    for item in items:
        code = normalize_builder_code(item)
        if code and code not in valid:
            valid.append(code)
    return ",".join(valid) or None


def declare_builder_code(app_code: str) -> dict[str, Any]:
    """Build the builder-code extension dict for your x402 route's `extensions`.

    Returns ``{"builder-code": {"info": {"a": <app_code>}, "schema": ...}}``,
    ready to merge into the route config you pass to the x402 middleware. This is
    all it takes to declare your app code — the facilitator does the rest at
    settlement. Raises ValueError if ``app_code`` is malformed.
    """
    code = (app_code or "").strip()
    if not re.match(BUILDER_CODE_PATTERN, code):
        raise ValueError(
            f"builder code {code!r} must match {BUILDER_CODE_PATTERN} "
            "(1-32 lowercase letters, digits, underscores)"
        )
    return {BUILDER_CODE_KEY: {"info": {"a": code}, "schema": BUILDER_CODE_SCHEMA}}


def parse_builder_code_suffix(calldata: Any) -> dict[str, Any] | None:
    """Decode the ERC-8021 Schema 2 builder-code suffix from settlement calldata.

    The reverse of what the facilitator appends at settle time (the Python
    equivalent of TS ``parseBuilderCodeSuffixFromCalldata``). Returns a dict with
    whichever of ``a`` / ``w`` / ``s`` are present, or ``None`` when there is no
    valid Schema 2 suffix — so it's safe to call on ANY transaction. Used by the
    daily backfill to read the authoritative on-chain attribution, above all
    ``w`` (the facilitator wallet code), which only exists post-settle.

    ``calldata`` may be a hex string (``0x...``) or raw ``bytes``. Never raises.
    """
    try:
        if isinstance(calldata, str):
            data = bytes.fromhex(calldata[2:] if calldata.startswith("0x") else calldata)
        else:
            data = bytes(calldata)
    except (ValueError, TypeError):
        return None

    # Need at least cbor(0+) + length(2) + schema(1) + marker(16); the marker and
    # schema byte gate out any non-attributed transaction.
    if len(data) < 19 or data[-16:] != ERC_8021_MARKER or data[-17] != _SCHEMA_2_ID:
        return None

    cbor_len = int.from_bytes(data[-19:-17], "big")
    start = len(data) - 19 - cbor_len
    if start < 0:
        return None

    # cbor2 is only needed on this decode path — import it lazily so declaring a
    # code stays dependency-free.
    try:
        import cbor2

        decoded = cbor2.loads(data[start : len(data) - 19])
    except Exception:
        return None

    if not isinstance(decoded, dict):
        return None
    out: dict[str, Any] = {}
    for key in ("a", "w", "s"):
        val = decoded.get(key)
        if val not in (None, "", []):
            out[key] = val
    return out or None

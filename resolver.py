"""Resolve a builder code → the wallet that owns it and its payout address.

Base Builder Codes are an **ERC-721 NFT collection**: registering a code mints a
token whose ID is derived from the code string, with an onchain **payout address**
declaring where that code's rewards should be sent. So "which wallet owns which
builder code" is a plain onchain read against the registry — no API key, no
indexer, works for any code anyone has ever registered.

  Registry (Base mainnet): 0x000000BC7E6457e610fe52Dcc0ca5b3ce59C8E80
    - ERC1967 proxy, verified; impl "BuilderCodes" at 0x0000010080e4FE8932638049E7488BB4504BAFfb
    - ERC-721 "Builder Codes" (BUILDERCODE); repo: github.com/base/builder-codes
    - Implements the ERC-8021 ICodesRegistry interface.

Two addresses come back, and they are NOT the same thing:
  - owner          — who holds the code NFT (can transfer it / update its payout)
  - payout_address — where rewards for the code should be sent  ← pay THIS one

IMPORTANT: for the free auto-generated `bc_*` codes, base.dev holds the NFT
custodially, so `owner` is the base.dev REGISTRAR (one wallet holds ~46k codes),
NOT the builder. Only self-custodied / vanity codes have owner == the builder.
Either way, `payout_address` is the meaningful recipient — always pay that.
Verified live: bc_c12702g2 → owner 0x1bD9…C7F1 (registrar), payout 0x95dD…71cc.

This module speaks raw JSON-RPC `eth_call` (only needs `requests`), so it drops
into the affiliation kit with no web3 dependency. Point it at any Base RPC.

Verified live: leap_wallet → owner 0xf9d7…8116, payout 0xa06c…54c8.
"""
from __future__ import annotations

import re

import requests

# Canonical Base-mainnet registry (the ERC1967 proxy). Same address is the
# authoritative source for every builder code.
BUILDER_CODES_REGISTRY = "0x000000BC7E6457e610fe52Dcc0ca5b3ce59C8E80"
DEFAULT_RPC = "https://mainnet.base.org"

# 4-byte function selectors (keccak256(sig)[:4]). Hardcoded so this file needs no
# keccak library; each is verified against the on-chain contract.
_SEL_OWNER_OF = "6352211e"        # ownerOf(uint256)          — standard ERC-721
_SEL_PAYOUT_BY_ID = "9b2c1793"    # payoutAddress(uint256)
_SEL_OWNER_OF_STR = None          # (use the uint256 overloads — no string ABI encoding needed)

_CODE_RE = re.compile(r"^[a-z0-9_]{1,32}$")


def to_token_id(code: str) -> int:
    """Builder code → ERC-721 token ID.

    The contract derives the id by reading the code's ASCII bytes as a big-endian
    integer (verified: matches base.BuilderCodes.toTokenId on-chain exactly). No
    RPC needed. Raises ValueError on a malformed code.
    """
    if not _CODE_RE.match(code or ""):
        raise ValueError(f"invalid builder code {code!r} (must match {_CODE_RE.pattern})")
    return int.from_bytes(code.encode("ascii"), "big")


def to_code(token_id: int) -> str:
    """ERC-721 token ID → builder code (reverse of to_token_id)."""
    length = (token_id.bit_length() + 7) // 8
    return token_id.to_bytes(length, "big").decode("ascii")


def _eth_call(selector: str, token_id: int, rpc_url: str, timeout: float) -> str | None:
    """Call a `(uint256)` view on the registry; return the 32-byte hex word, or
    None if the call reverted (e.g. the code isn't registered)."""
    data = "0x" + selector + f"{token_id:064x}"
    resp = requests.post(
        rpc_url,
        json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
              "params": [{"to": BUILDER_CODES_REGISTRY, "data": data}, "latest"]},
        timeout=timeout,
    )
    resp.raise_for_status()
    body = resp.json()
    result = body.get("result")
    # A revert (unregistered token) comes back as an error, or occasionally "0x".
    if body.get("error") or not result or result == "0x":
        return None
    return result


def _word_to_address(word: str | None) -> str | None:
    """Last 20 bytes of a 32-byte return word → 0x address (lowercase). None if
    the word is missing or the zero address."""
    if not word:
        return None
    addr = "0x" + word[-40:]
    return None if int(addr, 16) == 0 else addr


def resolve(code: str, *, rpc_url: str = DEFAULT_RPC, timeout: float = 20.0) -> dict:
    """Resolve a builder code to its owner + payout address on Base.

    Returns::

        {"code": "leap_wallet",
         "token_id": 131042744964646850211374452,
         "registered": True,
         "owner": "0xf9d7…8116",          # controls the code NFT
         "payout_address": "0xa06c…54c8"} # where rewards go — pay this

    ``registered`` is False (with owner/payout None) when no such code is minted.
    """
    token_id = to_token_id(code)
    owner = _word_to_address(_eth_call(_SEL_OWNER_OF, token_id, rpc_url, timeout))
    if owner is None:
        return {"code": code, "token_id": token_id, "registered": False,
                "owner": None, "payout_address": None}
    payout = _word_to_address(_eth_call(_SEL_PAYOUT_BY_ID, token_id, rpc_url, timeout))
    return {"code": code, "token_id": token_id, "registered": True,
            "owner": owner, "payout_address": payout}


def resolve_many(codes, *, rpc_url: str = DEFAULT_RPC) -> dict[str, dict]:
    """Resolve several codes → {code: resolve(code)}. (Sequential; the registry
    has no batch getter — wrap in a multicall if you need one round-trip.)"""
    return {c: resolve(c, rpc_url=rpc_url) for c in dict.fromkeys(codes)}


if __name__ == "__main__":
    # Live demo against Base mainnet — resolves real registered codes.
    for code in ["leap_wallet", "bitcoin_com", "myetherwallet_mew", "definitely_not_a_real_code_xyz"]:
        r = resolve(code)
        if r["registered"]:
            print(f"{code:20} owner={r['owner']}  payout={r['payout_address']}")
        else:
            print(f"{code:20} (not registered)")

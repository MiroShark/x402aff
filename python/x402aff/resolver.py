"""Resolve a builder code → the wallet that owns it and its payout address.

Base Builder Codes are an **ERC-721 NFT collection**: registering a code mints a
token whose ID is derived from the code string, with an onchain **payout address**
declaring where that code's rewards should be sent. So "which wallet owns which
builder code" is a plain onchain read against the registry - no API key, no
indexer, works for any code anyone has ever registered.

  Registry (Base mainnet): 0x000000BC7E6457e610fe52Dcc0ca5b3ce59C8E80
    - ERC1967 proxy, verified; impl "BuilderCodes" at 0x0000010080e4FE8932638049E7488BB4504BAFfb
    - ERC-721 "Builder Codes" (BUILDERCODE); repo: github.com/base/builder-codes
    - Implements the ERC-8021 ICodesRegistry interface.

Two addresses come back, and they are NOT the same thing:
  - owner          - who holds the code NFT (can transfer it / update its payout)
  - payout_address - where rewards for the code should be sent  ← pay THIS one

IMPORTANT: for the free auto-generated `bc_*` codes, base.dev holds the NFT
custodially, so `owner` is the base.dev REGISTRAR (one wallet holds tens of
thousands of codes), NOT the builder. Only self-custodied / vanity codes have
owner == the builder. Either way, `payout_address` is the meaningful recipient -
always pay that.

This module speaks raw JSON-RPC `eth_call` (only needs `requests`), so it drops
into the x402aff kit with no web3 dependency. Point it at any Base RPC.

Verified live: leap_wallet → owner 0xf9d7…8116, payout 0xa06c…54c8.
"""
from __future__ import annotations

import os
import re

import requests

from .builder_code import BUILDER_CODE_PATTERN

# Canonical Base-mainnet registry (the ERC1967 proxy). Same address is the
# authoritative source for every builder code.
BUILDER_CODES_REGISTRY = "0x000000BC7E6457e610fe52Dcc0ca5b3ce59C8E80"
# The public Base endpoint. Rate-limits after a few calls in a row.
DEFAULT_RPC = "https://mainnet.base.org"

# The RPC every kit path falls back to when the caller passes none. Resolved
# here, in the leaf module, because `split` cannot import `push_split` (that
# would cycle) - so this is the only place all four entry points can share. The
# registry-resolve leg used to default to DEFAULT_RPC directly and so ignored
# X402_BASE_RPC entirely: a seller who set a paid RPC still hit the public one
# for the lookup whose failure silently costs the builder their cut.
BASE_RPC = os.environ.get("X402_BASE_RPC", DEFAULT_RPC)

# USDC on Base, the token every split is paid in. Defined here, not in
# `push_split`, for the same cycle reason as BASE_RPC: the payout screen below
# needs it. `push_split.USDC_BASE` re-exports this one.
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

# 4-byte function selectors (keccak256(sig)[:4]). Hardcoded so this file needs no
# keccak library; each is verified against the on-chain contract.
_SEL_OWNER_OF = "6352211e"        # ownerOf(uint256)          - standard ERC-721
_SEL_PAYOUT_BY_ID = "9b2c1793"    # payoutAddress(uint256)
_SEL_IS_BLACKLISTED = "fe575a87"  # isBlacklisted(address)    - on USDC (Circle FiatToken)

_CODE_RE = re.compile(BUILDER_CODE_PATTERN)


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


# EIP-1474: an eth_call that ran and reverted. Distinct from the -32000/-32005/
# -32603 family a node returns when it never executed the call at all.
_REVERT_ERROR_CODE = 3


def _is_revert(error: dict) -> bool:
    """Did the call execute and revert (code unregistered), or did the node fail?

    Only a revert means "no such code". Nodes disagree on how they say it - some
    use the EIP-1474 code, others only put it in the message - so check both.
    """
    if error.get("code") == _REVERT_ERROR_CODE:
        return True
    return "revert" in str(error.get("message", "")).lower()


def _eth_call(selector: str, arg: int, rpc_url: str, timeout: float, *,
              to: str = BUILDER_CODES_REGISTRY, what: str = "registry") -> str | None:
    """Call a one-word-argument view (the registry by default); return the
    32-byte hex word.

    Returns None only when the call *reverted* - i.e. the code is not registered.
    A transport or node failure raises instead, and must: collapsing an RPC 429
    into "unregistered" routes the payment to the seller unsplit while leaving
    ``PayTo.error`` unset, so nothing logs and the builder loses their cut in
    silence - exactly the signal ``payto.PayTo.error`` exists to catch. The 402
    still never breaks: ``payto.payto_for_request`` catches this and falls back
    to the seller, but records the error. Mirrors the TS port's isUnregistered.
    """
    data = "0x" + selector + f"{arg:064x}"
    resp = requests.post(
        rpc_url,
        json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
              "params": [{"to": to, "data": data}, "latest"]},
        timeout=timeout,
    )
    resp.raise_for_status()
    body = resp.json()
    error = body.get("error")
    if error:
        if _is_revert(error):
            return None
        raise RuntimeError(f"{what} eth_call failed: {error}")
    result = body.get("result")
    # Some nodes answer a reverting view with an empty result instead of an error.
    if not result or result == "0x":
        return None
    return result


def _word_to_address(word: str | None) -> str | None:
    """Last 20 bytes of a 32-byte return word → 0x address (lowercase). None if
    the word is missing or the zero address."""
    if not word:
        return None
    addr = "0x" + word[-40:]
    return None if int(addr, 16) == 0 else addr


def payout_blocked(address: str, *, rpc_url: str = BASE_RPC, timeout: float = 20.0) -> bool:
    """Would USDC refuse to pay ``address``? True means keep it out of a split.

    A PushSplit pays every recipient in one ``distribute``, so one recipient that
    USDC refuses makes every distribute revert. The split is ownerless, so nothing
    can swap that recipient out: each payment in it, the seller's share included,
    is stuck for good. The registry lets a code's owner set any non-zero payout,
    so screen it before it goes into a split: the zero address, the USDC contract
    itself, and any address USDC reports as blacklisted.

    Raises on an RPC failure, like the registry reads, so the caller pays the
    seller unsplit and records the error rather than guessing either way.
    """
    if int(address, 16) == 0 or address.lower() == USDC_BASE.lower():
        return True
    word = _eth_call(_SEL_IS_BLACKLISTED, int(address, 16), rpc_url, timeout,
                     to=USDC_BASE, what="USDC isBlacklisted")
    if word is None:
        raise RuntimeError("USDC isBlacklisted returned no data")
    return int(word, 16) != 0


def resolve(code: str, *, rpc_url: str = BASE_RPC, timeout: float = 20.0) -> dict:
    """Resolve a builder code to its owner + payout address on Base.

    Returns::

        {"code": "leap_wallet",
         "token_id": 131042744964646850211374452,
         "registered": True,
         "owner": "0xf9d7…8116",          # controls the code NFT
         "payout_address": "0xa06c…54c8", # where rewards go - pay this
         "payout_blocked": False}         # True: USDC refuses it, never split to it

    ``registered`` is False (with owner/payout None) when no such code is minted.
    ``payout_blocked`` is :func:`payout_blocked` for the payout (one more read).
    """
    token_id = to_token_id(code)
    owner = _word_to_address(_eth_call(_SEL_OWNER_OF, token_id, rpc_url, timeout))
    if owner is None:
        return {"code": code, "token_id": token_id, "registered": False,
                "owner": None, "payout_address": None, "payout_blocked": False}
    payout = _word_to_address(_eth_call(_SEL_PAYOUT_BY_ID, token_id, rpc_url, timeout))
    blocked = payout is not None and payout_blocked(payout, rpc_url=rpc_url, timeout=timeout)
    return {"code": code, "token_id": token_id, "registered": True,
            "owner": owner, "payout_address": payout, "payout_blocked": blocked}


def resolve_many(codes, *, rpc_url: str = BASE_RPC) -> dict[str, dict]:
    """Resolve several codes → {code: resolve(code)}. (Sequential; the registry
    has no batch getter - wrap in a multicall if you need one round-trip.)"""
    return {c: resolve(c, rpc_url=rpc_url) for c in dict.fromkeys(codes)}


if __name__ == "__main__":
    # Live demo against Base mainnet - resolves real registered codes.
    for code in ["leap_wallet", "bitcoin_com", "myetherwallet_mew", "definitely_not_a_real_code_xyz"]:
        r = resolve(code)
        if r["registered"]:
            print(f"{code:20} owner={r['owner']}  payout={r['payout_address']}")
        else:
            print(f"{code:20} (not registered)")

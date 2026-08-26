"""Request-time ``payTo`` - the enforced split, no facilitator of your own.

If the buyer's app names the builder *at 402 time* (a header on the unpaid
request), then ``payTo`` can simply BE the per-pair split, and the stock CDP
facilitator settles into it with sponsored gas - no settler, no 7702, no key
handling on your side.

    unpaid request (carries X-Builder-Code)
        └─▶ payto_for_request()  → per-pair PushSplit address
              └─▶ 402 advertises payTo = that address
                    └─▶ CDP settles a plain USDC transfer into it (writes a/s/w)
                          └─▶ distribute.py fans it out later, permissionlessly

Why this is *enforced*: the split is created ownerless (``owner = 0``), so once
funds land there the ratio is fixed and nobody - including you - can claw the
builder's cut back. ``distribute`` is permissionless, so the builder can even
call it themselves. Verified on a Base mainnet fork in ``fork-test/CdpPath.t.sol``
and end-to-end on Base mainnet.

The tradeoff is non-atomicity: funds sit in the split until someone calls
``distribute`` (safe while they wait, and batching many payments into one
distribute is cheaper than splitting per payment - see ``monitor.py``).

What it costs the buyer: their client must send the code on the **unpaid**
request, not only inside the payment. That is one header (``X-Builder-Code``)
beyond the standard ``s`` extension - see ``buyer_client.py`` (the buyer-side
extension). Buyers who don't send it still pay normally; they just route to the
seller with no split.

SAFETY: a resolve failure must never break the paywall. Every entry point here
falls back to the seller's own wallet, so the worst case is an unsplit payment,
never a failed one.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional

from . import push_split, resolver, split

# The header a buyer's client sets on the unpaid request. Same grammar as `s`.
BUILDER_CODE_HEADER = "X-Builder-Code"

# Where the unsplit remainder - and every unattributed payment - is paid.
SELLER_PAYOUT = os.environ.get("X402_SELLER_PAYOUT", "")


@dataclass(frozen=True)
class PayTo:
    """The address to advertise in the 402, plus why it is that address."""

    address: str
    plan: split.SplitPlan
    split_deployed: bool
    #: False when the code was missing, unregistered, or could not be resolved -
    #: i.e. ``address`` is the seller's own wallet and no split will happen.
    attributed: bool
    #: Set when a lookup failed rather than simply finding no builder. Log it;
    #: a spike means the RPC is rate-limiting and builders are silently losing
    #: their cut (the public Base RPC 429s after a few calls in a row).
    error: Optional[str] = None


#: (code, seller, share_bps) → PayTo. Only *positive* (attributed) resolutions
#: are cached here: a pair's split address is deterministic (CREATE2, salt=0) and
#: a registered payout effectively never changes, so it's safe to hold for the
#: process lifetime. An unregistered code goes in the short-TTL negative cache
#: below instead (a lookup error is not cached at all); keeps the 402 path to
#: zero RPC round-trips once a builder is resolved.
_CACHE: dict[tuple[str, str, int], PayTo] = {}

#: (code, seller, share_bps) → monotonic expiry, for UNREGISTERED codes only (a
#: clean "not registered" answer from the registry). A repeated bogus code would
#: otherwise re-hit the registry on every unpaid request - one RPC call each, no
#: rate limit - which trips the public RPC's 429s and makes legitimate builders
#: silently lose their cut. This bounds the *repeated-code* case; a flood of
#: *distinct* bogus codes still costs one read each (every key is new) and is
#: left to the paid-RPC recommendation (its own limits) plus fail-open. A lookup
#: ERROR is deliberately NOT cached, so a real builder hit by a transient blip
#: retries next request instead of being stranded for the TTL. The TTL is short
#: so a builder who registers just after their first request is stranded for at
#: most that window; the cap keeps the cache itself from being an amp vector.
_NEG_CACHE: dict[tuple[str, str, int], float] = {}
NEG_CACHE_TTL_S = 60.0
NEG_CACHE_MAX = 1024


def _neg_hit(key: tuple[str, str, int]) -> bool:
    """True if `key` has a live negative entry (expired entries swept lazily)."""
    exp = _NEG_CACHE.get(key)
    if exp is None:
        return False
    if exp <= time.monotonic():
        _NEG_CACHE.pop(key, None)
        return False
    return True


def _neg_set(key: tuple[str, str, int]) -> None:
    """Record a short-TTL miss, evicting the oldest when full. Refresh moves the
    key to the end (dicts keep insertion order), so eviction drops the oldest."""
    _NEG_CACHE.pop(key, None)
    if len(_NEG_CACHE) >= NEG_CACHE_MAX:
        oldest = next(iter(_NEG_CACHE), None)
        if oldest is not None:
            _NEG_CACHE.pop(oldest, None)
    _NEG_CACHE[key] = time.monotonic() + NEG_CACHE_TTL_S


def builder_code_from_headers(headers) -> Optional[str]:
    """Read the builder code off the unpaid request's headers.

    Accepts anything dict-like with a case-insensitive ``get`` (Flask/Django/
    Starlette request headers all qualify). Returns a normalized code or None.
    """
    if headers is None:
        return None
    raw = None
    try:
        raw = headers.get(BUILDER_CODE_HEADER)
        if raw is None:
            # Plain dicts are case-sensitive; WSGI environs use another spelling.
            raw = headers.get(BUILDER_CODE_HEADER.lower()) or headers.get(
                "HTTP_X_BUILDER_CODE"
            )
    except Exception:
        return None
    # A header value is a bare string; split any comma-joined layering into the
    # list normalize_service_codes expects (it only splits lists, not strings).
    from .builder_code import normalize_service_codes

    items = raw.split(",") if isinstance(raw, str) else raw
    return split.primary_code(normalize_service_codes(items))


def payto_for_request(
    builder_code: Optional[str],
    *,
    seller_payout: Optional[str] = None,
    builder_share_bps: Optional[int] = None,
    rpc_url: Optional[str] = None,
    use_cache: bool = True,
) -> PayTo:
    """Resolve the ``payTo`` for one 402. Never raises.

    No code, an unregistered code, or a failed lookup all yield the seller's own
    wallet - the payment still works, it just isn't split.
    """
    seller = seller_payout or SELLER_PAYOUT
    if not seller:
        raise ValueError("seller_payout (or X402_SELLER_PAYOUT) is required")
    share = (
        push_split.BUILDER_SHARE_BPS if builder_share_bps is None else builder_share_bps
    )
    code = split.primary_code(builder_code)

    if not code:
        return PayTo(seller, split.build_split_plan(seller, None), False, False)

    key = (code, seller, share)
    if use_cache and key in _CACHE:
        return _CACHE[key]
    if use_cache and _neg_hit(key):
        # A recent UNREGISTERED miss short-circuits without another registry read,
        # so a repeated bogus code can't re-hit the RPC on every request.
        return PayTo(
            seller, split.build_split_plan(seller, None, builder_code=code), False, False
        )

    try:
        plan = split.resolve_and_plan(
            code,
            seller,
            builder_share_bps=share,
            rpc_url=rpc_url or resolver.BASE_RPC,
        )
        if not plan.has_builder:
            # Resolved fine, but this code isn't registered *yet*. Held only in the
            # short-TTL negative cache (not `_CACHE`): the builder may register
            # later, and a lasting miss would strand their cut for the whole
            # process life. Only positive resolutions are memoized permanently.
            if use_cache:
                _neg_set(key)
            return PayTo(seller, plan, False, False)

        address, deployed = push_split.predict_split_address(plan, rpc_url=rpc_url)
        result = PayTo(address, plan, deployed, True)
        if use_cache:
            _NEG_CACHE.pop(key, None)  # a code that now resolves is no longer a miss
            _CACHE[key] = result
        return result
    except Exception as exc:  # noqa: BLE001 - a bad lookup must not break the 402
        # Deliberately NOT cached: this is transient (RPC 429s, timeouts) and a
        # cached failure would strand that builder for the TTL after the RPC
        # recovers (and it doesn't help a varied-code flood - every key is new).
        return PayTo(
            seller,
            split.build_split_plan(seller, None, builder_code=code),
            False,
            False,
            error=f"{type(exc).__name__}: {exc}",
        )


def clear_cache() -> None:
    """Drop the memoized pair→address map + the short-TTL negative cache (tests,
    or after a share change / to retry a resolve that failed on a busy RPC)."""
    _CACHE.clear()
    _NEG_CACHE.clear()


if __name__ == "__main__":
    seller = SELLER_PAYOUT or "0x2222222222222222222222222222222222222222"
    for code in ["leap_wallet", "definitely_not_a_real_code_xyz", None]:
        r = payto_for_request(code, seller_payout=seller)
        label = code or "(no header)"
        print(f"{label:32} payTo={r.address}  attributed={r.attributed}"
              + (f"  deployed={r.split_deployed}" if r.attributed else "")
              + (f"  ERROR {r.error}" if r.error else ""))

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
and end-to-end on Base mainnet (see ``RUNBOOK-live-test.md``).

The tradeoff is non-atomicity: funds sit in the split until someone calls
``distribute`` (safe while they wait, and batching many payments into one
distribute is cheaper than splitting per payment - see ``monitor.py``).

What it costs the buyer: their client must send the code on the **unpaid**
request, not only inside the payment. That is one header (``X-Builder-Code``)
beyond the standard ``s`` extension - see ``buyer_client.py`` and the browser
``test_endpoint/try.html``. Buyers who don't send it still pay normally; they
just route to the seller with no split.

SAFETY: a resolve failure must never break the paywall. Every entry point here
falls back to the seller's own wallet, so the worst case is an unsplit payment,
never a failed one.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import resolver
import push_split
import split

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


#: (code, seller, share_bps) → PayTo. A pair's split address is deterministic
#: (CREATE2, salt=0) and a registered payout address effectively never changes,
#: so this is safe to hold for the process lifetime. Keeps the 402 path to zero
#: RPC round-trips after the first request from each builder.
_CACHE: dict[tuple[str, str, int], PayTo] = {}


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
    from builder_code import normalize_service_codes

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

    try:
        plan = split.resolve_and_plan(
            code,
            seller,
            builder_share_bps=share,
            rpc_url=rpc_url or resolver.DEFAULT_RPC,
        )
        if not plan.has_builder:
            # Resolved fine, just nobody home. Cacheable.
            result = PayTo(seller, plan, False, False)
            if use_cache:
                _CACHE[key] = result
            return result

        address, deployed = push_split.predict_split_address(plan, rpc_url=rpc_url)
        result = PayTo(address, plan, deployed, True)
        if use_cache:
            _CACHE[key] = result
        return result
    except Exception as exc:  # noqa: BLE001 - a bad lookup must not break the 402
        # Deliberately NOT cached: this is transient (RPC 429s, timeouts) and a
        # cached failure would strand that builder for the whole process life.
        return PayTo(
            seller,
            split.build_split_plan(seller, None, builder_code=code),
            False,
            False,
            error=f"{type(exc).__name__}: {exc}",
        )


def clear_cache() -> None:
    """Drop the memoized pair→address map (tests, or after a share change)."""
    _CACHE.clear()


if __name__ == "__main__":
    seller = SELLER_PAYOUT or "0x2222222222222222222222222222222222222222"
    for code in ["leap_wallet", "definitely_not_a_real_code_xyz", None]:
        r = payto_for_request(code, seller_payout=seller)
        label = code or "(no header)"
        print(f"{label:32} payTo={r.address}  attributed={r.attributed}"
              + (f"  deployed={r.split_deployed}" if r.attributed else "")
              + (f"  ERROR {r.error}" if r.error else ""))

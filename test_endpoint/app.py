"""Live x402 test endpoint — the CDP path from the affiliation kit, deployed.

One paid route (`POST /run`) that exercises the whole request-time-payTo design
against the REAL Coinbase CDP facilitator on Base mainnet:

  * declares the seller app code ``a`` (X402_BUILDER_CODE) on the route
  * sets ``payTo`` PER REQUEST from the ``X-Builder-Code`` header, via
    ``payto.payto_for_request`` → the per-pair PushSplit address (or the seller's
    own wallet when there's no/unknown code)
  * lets the stock CDP facilitator settle a plain USDC transfer into that split,
    with sponsored gas, still writing ``a``/``s``/``w`` on-chain

The money then sits in the ownerless split until ``distribute.py`` releases it —
no settler, no 7702, no facilitator of our own. Proven on a fork in
``fork-test/CdpPath.t.sol``; this is the same path with real USDC.

Env (set in Railway, never committed):
  X402_BUILDER_CODE   seller app code `a`         e.g. bc_c12702g2
  X402_SELLER_PAYOUT  where the 90% lands         e.g. 0x95dd…71cc
  CDP_API_KEY_ID      CDP API key id
  CDP_API_KEY_SECRET  CDP API key secret
  X402_PRICE          optional, default $0.02
  X402_BASE_RPC       optional, a paid Base RPC (recommended)
  X402_WARM_CODE      optional, builder code to pre-resolve at boot (default leap_wallet)
"""
from __future__ import annotations

import logging
import os
import sys
import time

# Import the kit modules from the repo root (one level up).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI  # noqa: E402
from cdp.x402 import create_facilitator_config  # noqa: E402
from x402.http import (  # noqa: E402
    CreateHeadersAuthProvider,
    FacilitatorConfig,
    HTTPFacilitatorClient,
    PaymentOption,
)
from x402.http.types import HTTPRequestContext, RouteConfig  # noqa: E402
from x402.http.middleware.fastapi import PaymentMiddlewareASGI  # noqa: E402
from x402.mechanisms.evm.exact import ExactEvmServerScheme  # noqa: E402
from x402.server import x402ResourceServer  # noqa: E402

import payto  # noqa: E402
from builder_code import declare_builder_code  # noqa: E402

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("x402-test-endpoint")

NETWORK = "eip155:8453"  # Base mainnet
APP_CODE = os.environ["X402_BUILDER_CODE"]
SELLER_PAYOUT = os.environ["X402_SELLER_PAYOUT"]
PRICE = os.environ.get("X402_PRICE", "$0.02")
WARM_CODE = os.environ.get("X402_WARM_CODE", "leap_wallet")

# ── CDP facilitator (mainnet) ────────────────────────────────────────────────
# create_facilitator_config reads CDP_API_KEY_ID / CDP_API_KEY_SECRET and returns
# a dict of url + a create_headers callable that mints the per-request CDP auth
# JWTs (it's a TypedDict, so access by key).
_cdp = create_facilitator_config()
FACILITATOR_URL = _cdp["url"]
facilitator = HTTPFacilitatorClient(
    FacilitatorConfig(
        url=FACILITATOR_URL,
        auth_provider=CreateHeadersAuthProvider(_cdp["create_headers"]),
    )
)
server = x402ResourceServer(facilitator)
server.register(NETWORK, ExactEvmServerScheme())


async def dynamic_pay_to(ctx: HTTPRequestContext) -> str:
    """Resolve payTo for THIS request from the X-Builder-Code header.

    Deterministic + cached, so the 402 and the paid retry produce the SAME
    address (the buyer signs to it, so they must match). Any failure falls back
    to the seller wallet — the payment still works, it just isn't split.
    """
    raw = ctx.adapter.get_header(payto.BUILDER_CODE_HEADER)
    code = payto.builder_code_from_headers({payto.BUILDER_CODE_HEADER: raw} if raw else {})
    pt = payto.payto_for_request(code, seller_payout=SELLER_PAYOUT)
    if pt.error:
        log.warning("payTo resolve failed for %r, unsplit: %s", code, pt.error)
    else:
        log.info("payTo for code=%r → %s (attributed=%s, deployed=%s)",
                 code, pt.address, pt.attributed, pt.split_deployed)
    return pt.address


routes: dict[str, RouteConfig] = {
    "POST /run": RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                pay_to=dynamic_pay_to,   # per-request, from the header
                price=PRICE,
                network=NETWORK,
            )
        ],
        extensions=declare_builder_code(APP_CODE),  # declares `a` on-chain
        description="x402 affiliation kit — live CDP-path test route",
        mime_type="application/json",
    )
}

app = FastAPI(title="x402 affiliation kit — live test endpoint")
app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=server)


@app.post("/run")
async def run() -> dict:
    """The 'paid work'. Only reached after a verified, settled payment."""
    return {"ok": True, "ran": "the paid work", "app_code": APP_CODE}


@app.get("/")
async def health() -> dict:
    """Unpaid health/info — confirms config without moving money."""
    warm = payto.payto_for_request(WARM_CODE, seller_payout=SELLER_PAYOUT)
    return {
        "status": "up",
        "network": NETWORK,
        "app_code": APP_CODE,
        "seller_payout": SELLER_PAYOUT,
        "price": PRICE,
        "facilitator": FACILITATOR_URL,
        "example": {
            "builder_code": WARM_CODE,
            "pay_to_split": warm.address,
            "attributed": warm.attributed,
            "split_deployed": warm.split_deployed,
            "error": warm.error,
        },
    }


@app.on_event("startup")
async def _warm_cache() -> None:
    """Pre-resolve the expected test builder so the first 402 needs no live RPC
    call (the public Base RPC 429s, which would strand that first payment)."""
    for attempt in range(5):
        pt = payto.payto_for_request(WARM_CODE, seller_payout=SELLER_PAYOUT)
        if pt.attributed or not pt.error:
            log.info("warmed %s → %s (attributed=%s)", WARM_CODE, pt.address, pt.attributed)
            return
        log.warning("warm attempt %d failed: %s", attempt + 1, pt.error)
        payto.clear_cache()
        time.sleep(3)
    log.error("could not warm %s — first request may fall back to seller", WARM_CODE)

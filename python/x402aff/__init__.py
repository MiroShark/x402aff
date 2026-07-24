"""x402aff - builder-code affiliation for x402 sellers.

Route each payment to a per-(seller, builder) 0xSplits split so the builder who
drove it earns an enforced on-chain cut, with no ledger and no custody:

    from x402aff import Affiliation

    aff = Affiliation(app_code="bc_yourcode", seller_payout="0x…")

    PaymentOption(..., pay_to=aff.pay_to)          # per-request split address
    RouteConfig(..., extensions=aff.extensions)    # declares your `a`

`Affiliation` is the whole integration; the submodules below are the pieces it
sits on, importable directly if you need them (``from x402aff import payto``).

Buyers stamping their own builder code want `BuilderCodeClientExtension` from
``x402aff.buyer_client``; it is not re-exported here because it pulls in the
x402 client SDK, which a seller does not need.

Discovery (``monitor``, ``Affiliation.scan``/``pending``/``splits_payload``)
needs the optional CDP extra: ``pip install 'x402aff[cdp]'``.
"""
from __future__ import annotations

from .affiliation import Affiliation
from .builder_code import (
    AFFILIATION_MARKER,
    declare_builder_code,
    normalize_service_codes,
    parse_builder_code_suffix,
)
from .push_split import BUILDER_SHARE_BPS, USDC_BASE, predict_split_address
from .resolver import BUILDER_CODES_REGISTRY

__all__ = [
    "Affiliation",
    "AFFILIATION_MARKER",
    "BUILDER_CODES_REGISTRY",
    "BUILDER_SHARE_BPS",
    "USDC_BASE",
    "declare_builder_code",
    "normalize_service_codes",
    "parse_builder_code_suffix",
    "predict_split_address",
]

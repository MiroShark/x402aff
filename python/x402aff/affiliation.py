"""One object that is the whole integration - declare, payTo, distribute.

The x402aff kit's pieces (`builder_code`, `payto`, `split`, `push_split`, `distribute`,
`monitor`) each do one job well, but wiring an x402 route means importing several
and hand-rolling a ``payTo`` callback. This folds all of that behind a single
configured object so the integration is a couple of lines.

    from x402aff import Affiliation

    aff = Affiliation(app_code="bc_yourcode", seller_payout="0x…")

    # ── the x402 route: two attributes, no boilerplate ──
    PaymentOption(scheme="exact", price="$0.02", network="eip155:8453",
                  pay_to=aff.pay_to)        # ← was a ~15-line DynamicPayTo function
    RouteConfig(..., extensions=aff.extensions)   # declares your `a`

    # ── framework-agnostic (Flask/Django/etc.) if you're not using the callback ──
    addr = aff.pay_to_for(request.headers)   # sync → the split address (or your wallet)

    # ── the payout side ──
    for s in aff.pending():                  # splits holding distributable funds
        print(s.builder_code, s.distributable_units)
    calls, balance = aff.release("bc_alice") # deploy (first use) + distribute calldata

``aff.pay_to`` is a drop-in x402 ``DynamicPayTo`` callback: it pulls the builder
code off the request, resolves the per-pair split, and - like everything here -
**never raises**; any failure falls back to the seller wallet (unsplit, never a
failed payment). The same object accepts a headers mapping or a raw code, so it
works outside the x402 SDK too.

Import is dependency-light (only the request-path modules). The payout helpers
(`pending`/`scan`) import `monitor` lazily, so you only need `cdp-sdk` if you
actually use CDP-index discovery.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from . import distribute, payto, push_split, resolver, split
from .builder_code import AFFILIATION_MARKER, declare_builder_code

if TYPE_CHECKING:  # annotations only - keeps `monitor` (and cdp-sdk) lazy at runtime
    from . import monitor

log = logging.getLogger("affiliation")

__all__ = ["Affiliation"]


class Affiliation:
    """Configured entry point for one seller: declare ``a``, route ``payTo`` to
    the per-builder split, and release funds.

    Parameters
    ----------
    app_code
        Your app / resource-server code (the ``a`` you declare on the route).
    seller_payout
        Where your 90% (and every unattributed payment) is paid.
    builder_share_bps
        The builder's cut in basis points (``1000`` = 10%). ``None`` uses
        ``X402_BUILDER_SHARE_BPS`` / the x402aff kit default.
    rpc_url
        Base RPC for the address/balance reads. ``None`` uses ``X402_BASE_RPC`` /
        the public endpoint (which rate-limits - set a paid one in production).
    """

    #: The request header a buyer's client sets to name its builder.
    HEADER = payto.BUILDER_CODE_HEADER

    def __init__(
        self,
        app_code: str,
        seller_payout: str,
        *,
        builder_share_bps: Optional[int] = None,
        rpc_url: Optional[str] = None,
    ) -> None:
        if not app_code:
            raise ValueError("app_code is required (your `a` code)")
        if not seller_payout:
            raise ValueError("seller_payout is required")
        self.app_code = app_code
        self.seller_payout = seller_payout
        self.builder_share_bps = builder_share_bps
        self.rpc_url = rpc_url
        # Effective share, resolved once so payTo and monitoring never disagree.
        self._share = (
            push_split.BUILDER_SHARE_BPS
            if builder_share_bps is None
            else builder_share_bps
        )
        self._extensions: Optional[dict[str, Any]] = None

    # ── request path ─────────────────────────────────────────────────────────

    @property
    def extensions(self) -> dict[str, Any]:
        """The route ``extensions`` that declare your app code ``a``.

        Merge into any extensions you already advertise:
        ``{**my_extensions, **aff.extensions}``.
        """
        if self._extensions is None:
            self._extensions = declare_builder_code(self.app_code)
        return self._extensions

    async def pay_to(self, ctx) -> str:
        """Drop-in x402 ``DynamicPayTo`` callback → the ``payTo`` for this request.

        Pass as ``PaymentOption(pay_to=aff.pay_to)``. Reads ``X-Builder-Code`` off
        the request context and returns the per-pair split address, or the seller
        wallet when there's no/unknown/​unresolvable code.
        """
        return self.resolve(ctx).address

    def pay_to_for(self, source=None, *, code: Optional[str] = None) -> str:
        """Sync ``payTo`` for a request - for stacks where you set it yourself.

        ``source`` may be an x402 request context, a headers mapping (Flask/
        Starlette/dict), or a raw code string. Or pass ``code=`` directly.
        """
        return self.resolve(source, code=code).address

    def resolve(self, source=None, *, code: Optional[str] = None) -> payto.PayTo:
        """Full ``PayTo`` (address + why) for a request. Never raises.

        Logs a warning if resolution *failed* (vs. simply finding no builder) -
        a spike there means the RPC is rate-limiting and builders are silently
        losing their cut.
        """
        if code is None:
            code = self._code_from(source)
        pt = payto.payto_for_request(
            code,
            seller_payout=self.seller_payout,
            builder_share_bps=self.builder_share_bps,
            rpc_url=self.rpc_url,
        )
        if pt.error:
            log.warning("payTo resolve failed for %r, unsplit: %s", code, pt.error)
        return pt

    def _code_from(self, source) -> Optional[str]:
        """Pull a normalized builder code out of whatever the framework hands us."""
        if source is None:
            return None
        if isinstance(source, str):
            return split.primary_code(source)
        # x402 request context: ctx.adapter.get_header(name)
        adapter = getattr(source, "adapter", None)
        if adapter is not None and hasattr(adapter, "get_header"):
            raw = adapter.get_header(self.HEADER)
            return payto.builder_code_from_headers({self.HEADER: raw} if raw else {})
        # headers-like mapping (has .get): Flask/Starlette request.headers, dict, …
        if hasattr(source, "get"):
            return payto.builder_code_from_headers(source)
        return None

    @staticmethod
    def clear_cache() -> None:
        """Drop the memoized code→split-address cache (after a share change, or
        to retry a resolve that failed on a rate-limited RPC)."""
        payto.clear_cache()

    # ── payout path ──────────────────────────────────────────────────────────

    def balance(self, code: str) -> int:
        """USDC base units currently sitting in this builder's split (0 if none)."""
        pt = self.resolve(code=code)
        if not pt.attributed:
            return 0
        return distribute.split_balance_units(pt.address, rpc_url=self.rpc_url)

    def release(
        self, code: str, *, distributor: Optional[str] = None
    ) -> tuple[list[distribute.DistributeCall], int]:
        """Build the release calls + live balance for one builder's split.

        Returns ``(calls, balance_units)`` - submit each ``(target, data)`` from
        any funded Base account (skip when balance is 0). Permissionless.
        """
        pt = self.resolve(code=code)
        return distribute.distribute_plan(
            pt.plan, rpc_url=self.rpc_url, distributor=distributor
        )

    def scan(self, *, days: int = 90) -> list["monitor.SplitStatus"]:
        """Every builder who paid you and their split's balance (fullest first).

        Needs ``cdp-sdk`` - discovery uses CDP's attribution index (no local
        ledger). Returns ``monitor.SplitStatus`` rows.
        """
        from . import monitor  # lazy: only this path needs cdp_sql / cdp-sdk

        codes = monitor.discover_builder_codes(self.app_code, days=days)
        rows = [self._status_for(c) for c in codes]
        rows.sort(key=lambda s: s.balance_units, reverse=True)
        return rows

    def pending(self, *, days: int = 90) -> list["monitor.SplitStatus"]:
        """The subset of :meth:`scan` whose splits hold distributable funds."""
        return [s for s in self.scan(days=days) if s.needs_distribution]

    def _status_for(self, code: str) -> "monitor.SplitStatus":
        """One builder's split status at THIS facade's share (keeps the predicted
        address consistent with what :meth:`pay_to` advertises)."""
        from . import monitor

        info = resolver.resolve(code, rpc_url=self.rpc_url or push_split.BASE_RPC)
        payout = info.get("payout_address") if info.get("registered") else None
        if not payout:
            return monitor.SplitStatus(code, None, None, False, 0, self._share)
        plan = split.build_split_plan(
            self.seller_payout, payout, builder_code=code, builder_share_bps=self._share
        )
        addr, deployed = push_split.predict_split_address(plan, rpc_url=self.rpc_url)
        bal = distribute.split_balance_units(addr, rpc_url=self.rpc_url)
        return monitor.SplitStatus(code, payout, addr, deployed, bal, self._share)

    def splits_payload(self, *, days: int = 90) -> dict:
        """The claims-dashboard payload — every per-builder split for this seller,
        ready to serialize to JSON. One reusable call behind a ``/splits`` route.

        Each row carries the split address, its codes + share, live balance,
        deployed state, and a permissionless ``[deploy?, distribute]`` claim.
        Discovery is by our app code ``a`` (CDP); the claim is reconstructed from
        the seller wallet THIS facade holds, so even undeployed splits build with
        no guessing. The shared marker and any unregistered/​unresolvable builder
        code are skipped.

        No per-split payment count: the query that would produce one 400s on
        the CDP SQL API (queries.sql #5b). #5c has a cheap count-only alternative.

        Needs ``cdp-sdk`` (discovery) + a Base RPC (balances). Returns a dict:
        ``{"configured": True, "marker": ..., "count": N, "splits": [...]}``.
        """
        from . import monitor  # lazy: only this path needs cdp_sql / cdp-sdk

        codes = [
            c for c in monitor.discover_builder_codes(self.app_code, days=days)
            if c != AFFILIATION_MARKER
        ]
        splits = []
        for code in codes:
            pt = self.resolve(code=code)
            if not pt.attributed:  # unregistered builder / no split for this pair
                continue
            # resolve() is memoized, so release() reuses it (one set of reads/code).
            calls, balance = self.release(code)
            splits.append({
                "payTo": pt.address,
                "sellerCode": self.app_code,
                "builderCode": code,
                "builderShareBps": self._share,
                "balanceUnits": str(balance),
                "distributableUnits": str(split.distributable_units(balance)),
                # deployed = no deploy leg needed (the split contract already exists).
                "deployed": not any(c.step == "deploy_split" for c in calls),
                # Not `len(calls) > 0` - that is true for every attributed split,
                # so a settled one (parked at its permanent 2-unit floor forever)
                # rendered an eternal claim button that burned gas moving nothing.
                "claimable": bool(calls) and pt.plan.is_claimable(balance),
                "calls": [{"step": c.step, "target": c.target, "data": c.data} for c in calls],
            })

        splits.sort(key=lambda s: int(s["balanceUnits"]), reverse=True)
        return {
            "configured": True,
            "marker": AFFILIATION_MARKER,
            "count": len(splits),
            "splits": splits,
        }


if __name__ == "__main__":
    # Offline-ish demo: no code → falls straight back to the seller (no network).
    aff = Affiliation(app_code="bc_demo", seller_payout="0x2222222222222222222222222222222222222222")
    print("extensions :", aff.extensions)
    print("no code    → payTo", aff.pay_to_for(None), "(seller, unsplit)")
    print("header dict → payTo", aff.pay_to_for({Affiliation.HEADER: ""}), "(empty, unsplit)")
    print("\nlive: aff.pay_to_for('leap_wallet') resolves the split; "
          "aff.pending() lists what's owed; aff.release(code) builds the payout.")

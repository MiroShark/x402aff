"""Resource-server side: declare your app code + capture referer codes.

A minimal, self-contained Flask app that shows the THREE server-side touch points
of the affiliation system. It's illustrative — wire the same three pieces into
your own x402 route:

  1. DECLARE  — attach your app code ("a") to the paid route's extensions, once
                at startup. That's the whole "declare it" step.
  2. CAPTURE  — at request time, read the buyer's referer code ("s") + your own
                "a" straight off the verified payment payload, and store them.
  3. OBSERVE  — a WSGI wrapper records the settlement tx hash after settle, so the
                daily backfill can later read "w" from the chain.

Run:  X402_PAY_TO=0xYourWallet X402_BUILDER_CODE=bc_yourcode python server_example.py
(Without the x402 SDK installed the route just runs open — the wiring is the point.)
"""
from __future__ import annotations

import os
import threading
import uuid

from flask import Flask, current_app, g, jsonify, request

from builder_code import (
    BUILDER_CODE_KEY,
    declare_builder_code,
    normalize_builder_code,
    normalize_service_codes,
)
import tracking


# ────────────────────────────────────────────────────────────────────────────
# 2. CAPTURE — pull the builder codes off the verified payment, at request time.
# ────────────────────────────────────────────────────────────────────────────
def _dig(obj, key):
    """Read ``key`` whether ``obj`` is a dict or a pydantic model. The x402
    payload mixes the two (PaymentPayload is a model; its .payload is a dict)."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def builder_codes_from_payment() -> tuple[str | None, str | None]:
    """Return ``(app_code, service_codes)`` for THIS payment, no chain read.

      * service_codes ("s") — the buyer/client referer code(s), submitted at
        payment.extensions["builder-code"]["info"]["s"]. This is byte-for-byte
        what the facilitator writes on-chain, so capturing it here is
        authoritative. Comma-joined for layered clients; None if the buyer didn't
        integrate builder codes.
      * app_code ("a") — the code YOU declared (X402_BUILDER_CODE). Gated on a
        real payment so non-paid requests don't get mislabelled with it.

    ``w`` (facilitator wallet code) is NOT here — it only exists post-settle.
    """
    pp = getattr(g, "payment_payload", None)  # set by the x402 middleware
    if pp is None:
        return None, None  # not a paid request — nothing declared/submitted
    info = _dig(_dig(_dig(pp, "extensions"), BUILDER_CODE_KEY), "info")
    service_codes = normalize_service_codes(_dig(info, "s"))
    app_code = normalize_builder_code(os.environ.get("X402_BUILDER_CODE"))
    return app_code, service_codes


def payer_from_payment() -> str | None:
    """Best-effort buyer wallet from the EVM 'exact' payload (for the record)."""
    pp = getattr(g, "payment_payload", None)
    inner = _dig(pp, "payload")
    for auth_key in ("authorization", "permit2Authorization"):
        val = _dig(_dig(inner, auth_key), "from")
        if isinstance(val, str) and val:
            return val
    return None


def network_from_payment() -> str | None:
    req = getattr(g, "payment_requirements", None)
    net = getattr(req, "network", None)
    return net if isinstance(net, str) and net else None


def register_run_route(app: Flask) -> None:
    """The paid route. On a settled payment, capture attribution + store it."""

    @app.route("/run", methods=["POST"])
    def run():
        payment_id = f"pay_{uuid.uuid4().hex[:12]}"

        # ← the capture step. One call, no chain round-trip.
        code_a, code_s = builder_codes_from_payment()

        tracking.record_payment(
            current_app.config["DB"],
            payment_id=payment_id,
            payer_address=payer_from_payment(),
            payment_network=network_from_payment(),
            builder_code_a=code_a,   # yours
            builder_code_s=code_s,   # the buyer's referer — the affiliation
            price_usd=1.00,
            status="queued",
        )
        # ... kick off your actual work here, then set_result(...) when done.

        resp = jsonify({"payment_id": payment_id, "status": "queued"})
        # Tag the response so the settle observer (below) can tie the settlement
        # tx — which appears only in a header the middleware adds AFTER this
        # returns — back to this payment.
        resp.headers["X-Payment-Id"] = payment_id
        return resp, 202


# ────────────────────────────────────────────────────────────────────────────
# 3. OBSERVE — record the settlement tx hash after settle (for on-chain w later).
# ────────────────────────────────────────────────────────────────────────────
class SettleTxCaptureMiddleware:
    """Sits OUTSIDE the x402 middleware and reads the final response headers.

    The x402 middleware settles AFTER the view returns and only then adds the
    ``PAYMENT-RESPONSE`` header carrying the settlement tx hash — so the view
    can't see it. This wrapper reads it off the outgoing response (never the
    body) and records it, off the response path. Purely observational and fully
    guarded: it can never break a payment.
    """

    def __init__(self, wsgi_app, db_path: str):
        self.wsgi_app = wsgi_app
        self.db_path = db_path

    def __call__(self, environ, start_response):
        def _capturing_start_response(status, headers, exc_info=None):
            try:
                self._maybe_record(status, headers)
            except Exception:
                pass  # never let observation break the response
            return start_response(status, headers, exc_info)

        return self.wsgi_app(environ, _capturing_start_response)

    def _maybe_record(self, status, headers):
        if status[:1] != "2":  # only a settled 2xx carries a settlement
            return
        lower = {k.lower(): v for k, v in headers}
        payment_id = lower.get("x-payment-id")
        payment_response = lower.get("payment-response") or lower.get("x-payment-response")
        if not payment_id or not payment_response:
            return
        threading.Thread(
            target=self._record, args=(payment_id, payment_response), daemon=True
        ).start()

    def _record(self, payment_id, payment_response_header):
        try:
            from x402.http import decode_payment_response_header  # x402 SDK

            settle = decode_payment_response_header(payment_response_header)
            tx = getattr(settle, "transaction", None)
            if getattr(settle, "success", True) and isinstance(tx, str) and tx:
                tracking.set_settle_tx(tracking.connect(self.db_path), payment_id, tx)
        except Exception:
            pass


# ────────────────────────────────────────────────────────────────────────────
# 1. DECLARE — attach your app code to the paid route, once at startup.
# ────────────────────────────────────────────────────────────────────────────
def create_app() -> Flask:
    app = Flask(__name__)
    app.config["DB"] = tracking.connect()

    register_run_route(app)
    app.wsgi_app = SettleTxCaptureMiddleware(app.wsgi_app, tracking.DB_PATH)

    pay_to = (os.environ.get("X402_PAY_TO") or "").strip()
    builder_code = (os.environ.get("X402_BUILDER_CODE") or "").strip()

    if not pay_to:
        app.logger.warning("X402_PAY_TO unset — /run runs OPEN (no payment).")
        return app

    # Build the route's extensions. This is where declaring your code happens:
    # merge declare_builder_code(...) into whatever extensions you already pass
    # (e.g. the Bazaar discovery extension). A malformed code must NOT disable
    # the paywall — log and skip attribution instead.
    route_extensions: dict = {}
    if builder_code:
        try:
            route_extensions.update(declare_builder_code(builder_code))
            app.logger.info("declared builder code a=%s on /run", builder_code)
        except ValueError as exc:
            app.logger.warning("X402_BUILDER_CODE invalid (%s); attribution off", exc)

    # Then install the x402 middleware with these extensions on the route. Sketch
    # (real imports depend on your x402 SDK version):
    #
    #   from x402.http.middleware.flask import payment_middleware
    #   from x402.http.types import PaymentOption, RouteConfig
    #   routes = {"POST /run": RouteConfig(
    #       accepts=[PaymentOption(scheme="exact", pay_to=pay_to,
    #                              price="$1.00", network="eip155:8453")],
    #       extensions=route_extensions,      # ← your declared "a" rides here
    #   )}
    #   payment_middleware(app, routes=routes, server=server)  # uses CDP facilitator
    app.config["ROUTE_EXTENSIONS"] = route_extensions  # kept for inspection/demo
    return app


if __name__ == "__main__":
    create_app().run(port=5001)

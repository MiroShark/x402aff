"""Buyer side: how a builder attaches their code to EARN on the runs they drive.

This is the ONLY thing a builder (an app/agent that pays you on behalf of its
users) has to do to get attributed and paid. They register a tiny client
extension on the x402 client they pay with, and every payment then carries their
code as ``s`` (the referer). No contract changes; the facilitator writes it
on-chain at settlement.

Get a code (format ^[a-z0-9_]{1,32}$) at base.dev → Settings → Builder Codes,
then register the payout wallet you want the share sent to.

--------------------------------------------------------------------------------
Python - the x402 Python SDK has no builder-code module, but its client takes
generic extensions and the resource server declares the "builder-code" key in
its 402, so this matching extension fires and tags the code as `s`:

Every payment also carries a fixed, SHARED kit marker as a second `s` (see
`builder_code.AFFILIATION_MARKER`) so kit-routed payments self-identify on-chain:
discovery across all sellers is then one query, `WHERE builder_code = <marker>`,
with no reconstruction and undeployed splits included. The marker never affects a
payout (the split pays the primary code, i.e. yours) and is hardcoded and shared
across every install - not configurable, so kit payments always stay discoverable.
"""
from __future__ import annotations

from .builder_code import marked_service_codes


class BuilderCodeClientExtension:
    """Stamp every payment from this client with your builder code as ``s``.

    A hardcoded, shared kit marker (:data:`builder_code.AFFILIATION_MARKER`,
    ``x402aff``) rides along as a SECOND ``s`` code so kit-routed payments are
    discoverable on-chain with one query (``WHERE builder_code = 'x402aff'``). It
    never changes a payout - the split always pays the PRIMARY (first) code, i.e.
    *your* ``code`` - and is deliberately not configurable, so every kit payment
    stays discoverable ecosystem-wide.
    """

    key = "builder-code"

    def __init__(self, code: str):
        self.code = code

    def enrich_payment_payload(self, payload, payment_required):
        # Always append the shared, hardcoded marker as a second `s` (never a
        # duplicate of the real code), so the payment self-identifies on-chain.
        codes = marked_service_codes(self.code)
        exts = dict(payload.extensions or {})
        exts["builder-code"] = {"info": {"s": codes}}
        return payload.model_copy(update={"extensions": exts})


# Usage - on the same x402ClientSync() you registered the payment scheme on:
#
#   client.register_extension(BuilderCodeClientExtension("bc_yourcode"))
#   client.fetch("https://api.example.com/run", ...)   # now carries your code
#
# --------------------------------------------------------------------------------
# TypeScript - use the official extension instead (npm install @x402/extensions):
#
#   import { x402Client } from "@x402/fetch";
#   import { registerExactEvmScheme } from "@x402/evm/exact/client";
#   import { BuilderCodeClientExtension } from "@x402/extensions/builder-code";
#   import { privateKeyToAccount } from "viem/accounts";
#
#   const client = new x402Client();
#   registerExactEvmScheme(client, {
#     signer: privateKeyToAccount(process.env.X402_BUYER_PRIVATE_KEY),
#   });
#   client.registerExtension(new BuilderCodeClientExtension("bc_yourcode"));
#   // every client.fetch(...) payment now carries your code as `s`
#
# --------------------------------------------------------------------------------
# Verify (after a real Base-mainnet settlement): take the settle tx hash (from the
# PAYMENT-RESPONSE header) and paste it into buildercode-checker.vercel.app - you
# should see the resource server's `a`, the CDP facilitator's `w`, and your `s`.

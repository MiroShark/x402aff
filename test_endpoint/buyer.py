"""Buyer side of the live test - pay the endpoint once, attributing to a builder.

Run this with YOUR OWN funded Base wallet to drive one real payment through the
deployed endpoint. It:

  1. sends ``X-Builder-Code: <code>`` on the request, so the server's payTo
     resolves to the per-pair split (see test_endpoint/app.py)
  2. attaches the same code as ``s`` inside the payment (BuilderCodeClientExtension),
     so the CDP facilitator writes it on-chain
  3. signs the EIP-3009 authorization to that payTo and lets the x402 client
     auto-retry - CDP settles it (sponsored gas) into the split

Needs: a Base wallet holding ≥ the price in USDC (default $0.02). Gas is
sponsored by CDP, so no ETH is required.

    export BUYER_PRIVATE_KEY=0x...            # a funded Base wallet, YOURS
    export ENDPOINT_URL=https://x402-endpoint-production.up.railway.app
    export BUILDER_CODE=leap_wallet           # who earns the 10%
    python3 test_endpoint/buyer.py

On success it prints the settle tx hash - paste that back for verification, then
run distribute.py to release the split.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eth_account import Account
from x402.client import x402ClientSync
from x402.http.clients.requests import x402_requests
from x402.mechanisms.evm.exact import ExactEvmClientScheme
from x402.mechanisms.evm.signers import EthAccountSigner

import buyer_client

NETWORK = "eip155:8453"  # Base mainnet
URL = os.environ.get("ENDPOINT_URL", "https://x402-endpoint-production.up.railway.app")
CODE = os.environ.get("BUILDER_CODE", "leap_wallet")


def main() -> int:
    pk = os.environ.get("BUYER_PRIVATE_KEY")
    if not pk:
        print("Set BUYER_PRIVATE_KEY to a funded Base wallet (holds ≥ price in USDC).")
        return 2

    account = Account.from_key(pk)
    print(f"buyer     : {account.address}")
    print(f"endpoint  : {URL}/run")
    print(f"builder s : {CODE}")

    client = x402ClientSync()
    client.register(NETWORK, ExactEvmClientScheme(EthAccountSigner(account)))
    client.register_extension(buyer_client.BuilderCodeClientExtension(CODE))

    session = x402_requests(client)
    # The header must ride on the request so the server's payTo = the split; the
    # session replays it on the paid retry, so first 402 and retry agree on payTo.
    resp = session.post(f"{URL}/run", headers={"X-Builder-Code": CODE}, timeout=60)
    print(f"status    : {resp.status_code}")
    print(f"body      : {resp.text[:300]}")

    settle = resp.headers.get("PAYMENT-RESPONSE") or resp.headers.get("X-PAYMENT-RESPONSE")
    if settle:
        import base64, json
        try:
            decoded = json.loads(base64.b64decode(settle + "=" * (-len(settle) % 4)))
            print(f"settle tx : {decoded.get('transaction')}")
            print(f"payer     : {decoded.get('payer')}")
            print(f"network   : {decoded.get('network')}")
        except Exception:
            print(f"PAYMENT-RESPONSE (raw): {settle}")
    else:
        print("no PAYMENT-RESPONSE header - payment may not have settled.")
    return 0 if resp.status_code == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())

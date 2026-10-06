---
name: x402aff
description: Add builder-code affiliation to an x402 seller with the x402aff kit (TypeScript or Python), so the apps that send it paying users get a cut enforced on-chain at settlement through an ownerless 0xSplits contract on Base. Also covers the buyer side, payouts, and the claims dashboard.
---

# x402aff: builder-code affiliation for x402

Use this skill when a user runs (or is building) a paid API behind x402 and wants to give the apps, agents, or builders that send them paying users a share of each payment, enforced on-chain. Also use it when a user builds an x402 client and wants to earn from the payments it drives, or needs to release builder payouts.

Do not use it for networks other than Base mainnet, facilitators other than the Coinbase Developer Platform (CDP) facilitator, or custodial payout schemes.

## How it works

Base Builder Codes put three tags on a paid request: `a` (the seller's API), `s` (the builder that drove the payment), `w` (the facilitator). x402aff sets the route's `payTo` to a per-(seller, builder) 0xSplits PushSplit address, derived deterministically with CREATE2. The stock CDP facilitator settles the buyer's gasless USDC payment straight into that split. Anyone can later call `distribute`: by default 10% goes to the builder and 90% to the seller. Nobody, including the seller, can change the recipients or ratio of a funded split.

No private key, custom facilitator, or contract of your own is involved.

## Seller integration

1. Get an app code at https://base.dev under Settings, Builder Codes (it looks like `bc_yourcode`).
2. Install the SDK.
3. Wire `Affiliation` into the x402 route: `payTo` from the facade, and its `extensions` to declare your `a` code.
4. Set `X402_BASE_RPC` to a paid Base RPC. The public RPC rate-limits, and a failed resolve silently falls back to the seller's wallet, unsplit.
5. Echo `resolve(...).status` in the `X-Builder-Code-Status` response header (`STATUS_HEADER`). Values: `resolved`, `unregistered`, `invalid`, `error`, `none`. Without it a builder cannot tell a working code from an unminted or mistyped one.

### TypeScript (Node, with viem as a peer dependency)

```bash
npm install x402aff viem
```

```ts
import { Affiliation } from "x402aff";

const aff = new Affiliation({ appCode: "bc_yourcode", sellerPayout: "0x..." });

// drop-in x402 DynamicPayTo for express, hono, next
app.use(paymentMiddleware({ payTo: aff.payTo, extensions: aff.extensions }));

// or resolve it yourself per request
const payTo = await aff.payToFor(req.headers);
```

### Python

```bash
pip install x402aff
```

```python
from x402aff import Affiliation

aff = Affiliation(app_code="bc_yourcode", seller_payout=YOUR_WALLET)

PaymentOption(..., pay_to=aff.pay_to)        # per-request split
RouteConfig(..., extensions=aff.extensions)  # declares your code a
```

`aff.pay_to_for(headers)` is the synchronous form for other frameworks. Neither SDK throws: an unknown or unresolvable builder code falls back to the seller's wallet, so the payment still succeeds, just unsplit.

## Buyer side (one line)

The buyer's app sends its builder code in an `X-Builder-Code` header and attaches the builder-code extension:

```ts
extensions: [builderCode("bc_alice")]
```

TypeScript uses the official `@x402/extensions/builder-code`. Python ships `BuilderCodeClientExtension` in `x402aff.buyer_client`.

Builders: check that `X-Builder-Code-Status` on the seller's 402 says `resolved`. A code shown on base.dev earns only once it is minted on the registry; check with `cast call 0x000000BC7E6457e610fe52Dcc0ca5b3ce59C8E80 'isRegistered(string)(bool)' bc_alice`.

## Options

- Builder share: `X402_BUILDER_SHARE_BPS` (both SDKs), or `builderShareBps` (TypeScript) / `builder_share_bps` (Python). Basis points, default 1000 (10%), range 0 to 10000. The ratio is baked into the split address, so a new ratio opens new splits and old funds stay at the old ratio.
- More than two recipients: build a `SplitPlan` whose allocations sum to 10000.

## Payouts

- `aff.release("bc_alice")` returns the calls that deploy (if needed) and distribute one builder's split. Distribute is permissionless and costs a few cents of gas.
- `aff.pending()` (or `python3 -m x402aff.monitor`) finds every builder who paid the seller from CDP's index and shows which splits are ready.
- `aff.splits_payload()` (Python) / `aff.splitsPayload(cdpQuery)` (TypeScript) returns every split, balance, deployed state, and a permissionless claim for a `GET /splits` route. Live example: https://www.miroshark.xyz/x402aff
- Payouts land about 2 base units light: a split keeps 1 unit warm and floors each share. Do not gate a claim button on `balance > 0`; check whether any recipient nets at least 1 unit.

## Checks before you finish

- The route declares the seller's app code through `extensions`.
- `payTo` is deterministic per request, so the 402 and the buyer's signed retry agree.
- The facilitator is CDP on Base mainnet.
- `X402_BASE_RPC` points at a paid RPC.
- The 402 echoes `X-Builder-Code-Status`, and a request with a known registered code returns `resolved`.

## References

- Developer docs: https://www.x402aff.xyz/developers.md
- Integration guide: https://raw.githubusercontent.com/MiroShark/x402aff/main/docs/INTEGRATION.md
- TypeScript guide: https://raw.githubusercontent.com/MiroShark/x402aff/main/ts/README.md
- Repository: https://github.com/MiroShark/x402aff
- Read-only MCP server with these docs as tools: https://www.x402aff.xyz/api/mcp

# x402aff (TypeScript)

[![npm](https://img.shields.io/npm/v/x402aff?logo=npm&logoColor=white&label=npm)](https://www.npmjs.com/package/x402aff)
[![install size](https://img.shields.io/bundlephobia/minzip/x402aff?label=minzipped)](https://bundlephobia.com/package/x402aff)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/MiroShark/x402aff/blob/main/LICENSE)

The `Affiliation` facade for x402 sellers on the **Node / TypeScript** stack - a
port of the repo's Python [`affiliation.py`](../python/x402aff/affiliation.py). Same three
on-chain reads, same safe-fallback contract, and it resolves the **identical
split address** as the Python kit for a given `(seller, builder)` pair (the
encoding is asserted byte-for-byte against the Python/`cast` bytes in the tests).

Only dependency: [`viem`](https://viem.sh).

## Install

```bash
npm install x402aff viem   # viem is a peer dependency - you bring your own
```

Or vendor it: the package ships `src/affiliation.ts` alongside `dist/`, so you
can copy that one file into your project and depend on nothing but `viem`.

Working on the kit itself:

```bash
npm install        # from this dir
npm test           # needs Node >= 22.18 (runs the .ts tests via type stripping)
npm run build      # emits dist/ via tsconfig.build.json
```

## Use

```ts
import { Affiliation } from "x402aff";

const aff = new Affiliation({
  appCode: "bc_yourcode",
  sellerPayout: "0xYourWallet",
  // builderShareBps: 1000,           // 10% (default); also read from X402_BUILDER_SHARE_BPS
  // rpcUrl: "https://…",             // a paid Base RPC for production; also read
  //                                  // from X402_BASE_RPC if you set no rpcUrl.
});

// ── on your x402 route ──
// `aff.payTo` is a drop-in x402 `DynamicPayTo` callback - hand it straight to
// express / hono / next x402 middleware (it reads X-Builder-Code off the request
// via ctx.adapter.getHeader). It's a bound field, so pass it by reference:
const middleware = paymentMiddleware(facilitator, {
  "/api/data": { price: "$0.02", network: "base", payTo: aff.payTo, extensions: aff.extensions },
});

// Or resolve it yourself and wire the result in:
const extensions = aff.extensions;                 // declares your app code `a`
const payTo = await aff.payToFor(req.headers);     // the split, or your wallet

// ── the payout side (permissionless) ──
const { calls, balanceUnits } = await aff.release("bc_alice");
for (const c of calls) {
  // c.step ("deploy_split" | "distribute"), c.target, c.data - submit from any funded Base account
}
```

`payToFor` / `resolve` **never throw**: a missing, unknown, or unresolvable code
falls back to `sellerPayout` (unsplit, never a failed payment). Inject a viem
`PublicClient` via `{ client }` to share a transport or for tests.

## Buyer side

Builders attach their code with the official extension - no port needed:

```ts
import { BuilderCodeClientExtension } from "@x402/extensions/builder-code";
client.registerExtension(new BuilderCodeClientExtension("bc_yourcode"));
// …and send header  X-Builder-Code: bc_yourcode  on the request.
```

To make your kit-routed payments **discoverable on-chain** (across all sellers, one
query), attach the shared kit marker as a second `s` code - it never changes a
payout, since the split pays the primary code:

```ts
import { markedServiceCodes, AFFILIATION_MARKER } from "./src/affiliation.ts";
// s becomes ["bc_yourcode", AFFILIATION_MARKER]; the split still pays bc_yourcode.
const info = { "builder-code": { info: { s: markedServiceCodes("bc_yourcode") } } };
// …register an extension that sets this `info` (or pass both codes to one that
//   accepts multiple). Discovery: settlements whose `s` includes AFFILIATION_MARKER.
```

See [`../docs/INTEGRATION.md`](../docs/INTEGRATION.md) and [`../python/x402aff/queries.sql`](../python/x402aff/queries.sql) #5.

## Develop

```bash
npm run typecheck   # tsc --noEmit
npm test            # node --test (offline, mock viem transport)
```

Runs on Node ≥ 22.18 via built-in TypeScript type-stripping; no build step.

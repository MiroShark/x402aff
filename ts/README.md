# @x402-affiliation/kit (TypeScript)

The `Affiliation` facade for x402 sellers on the **Node / TypeScript** stack - a
port of the repo's Python [`affiliation.py`](../python/affiliation.py). Same three
on-chain reads, same safe-fallback contract, and it resolves the **identical
split address** as the Python kit for a given `(seller, builder)` pair (the
encoding is asserted byte-for-byte against the Python/`cast` bytes in the tests).

Only dependency: [`viem`](https://viem.sh).

## Install

```bash
npm install viem   # then drop src/affiliation.ts into your project, or:
npm install        # from this dir, to run the tests
```

## Use

```ts
import { Affiliation } from "./src/affiliation.ts";

const aff = new Affiliation({
  appCode: "bc_yourcode",
  sellerPayout: "0xYourWallet",
  // builderShareBps: 1000,          // 10% (default)
  // rpcUrl: process.env.BASE_RPC,   // use a paid RPC in production
});

// ── on your x402 route ──
const extensions = aff.extensions;                 // declares your app code `a`
const payTo = await aff.payToFor(req.headers);     // the split, or your wallet
// → wire `payTo` into however your x402 server middleware sets a per-request payTo,
//   and merge `extensions` into the route's extensions.

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

See [`../docs/INTEGRATION.md`](../docs/INTEGRATION.md) and [`../python/queries.sql`](../python/queries.sql) #5.

## Develop

```bash
npm run typecheck   # tsc --noEmit
npm test            # node --test (offline, mock viem transport) - 17 tests
```

Runs on Node ≥ 22 via built-in TypeScript type-stripping; no build step.

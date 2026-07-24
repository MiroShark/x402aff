# x402aff

Builder-code affiliation for x402 sellers.

**Give the apps that send you paying users a cut - enforced on-chain, at
settlement, with no facilitator of your own to run.**

If you sell an API behind an [x402](https://x402.org) paywall, this splits each
payment so the builder whose app drove it earns a share (default **10%**) -
automatically, verifiably, on Base.

> Validated on **Base mainnet** and reproducible on a mainnet fork: the Coinbase
> CDP facilitator settles into a per-builder [0xSplits](https://splits.org) split
> with `a`/`s`/`w` written on-chain, and anyone can `distribute` it. See the
> Foundry proof in [`fork-test/`](./fork-test).

---

## What it enables

Turn your x402 endpoint into an **affiliate program** with no backend for it:

- **Referral revenue-share, on autopilot.** A wallet, agent, or app that drives a
  payment to your API earns a cut. You keep the rest.
- **Enforced, not promised.** The cut lands in an ownerless, immutable 0xSplits
  contract at settlement - once it's there, **nobody (including you) can redirect
  the builder's share.** That's what makes it a credible offer.
- **No new infrastructure.** No settler, no self-run facilitator, no contract you
  write or audit. The stock **CDP facilitator** settles (gas sponsored); an audited
  0xSplits contract splits.
- **Zero-friction for the builder.** They add *one line* to their x402 client to
  attach their code - they never touch your repo.

---

## How it works

Base [Builder Codes](https://docs.cdp.coinbase.com/x402/core-concepts/builder-codes)
put three tags on a paid request: **`a`** (you, the API), **`s`** (the builder that
drove it), **`w`** (the facilitator). The x402aff kit turns `s` into money with one move:

**Set the route's `payTo` to a per-`(you, builder)` 0xSplits split.** The buyer's
app names its builder on the request; the CDP facilitator settles straight into
that split; anyone later calls `distribute` to release the two shares.

```
  buyer's app          YOUR /run                    CDP facilitator        Base
  (sends its code) ──▶ 402: payTo = the split ──▶ settles USDC into it ──▶ split
                                                    (writes a/s/w)          holds funds
                                                                              │
                              distribute (anyone) ◀───────────────────────────┘
                                     │
                        ┌────────────┴────────────┐
                     10% builder                90% you
```

Payouts aren't atomic: the money settles into the split on its own, but
**releasing** it (`distribute`) is a separate, permissionless call - you, a keeper,
or the builder can trigger it.

---

## Get started - pick your language

Both ship as a one-object `Affiliation` facade, and both resolve the **identical**
split address (the TS encoding is asserted byte-for-byte against Python). Get an app
code at [base.dev](https://base.dev) → *Settings → Builder Codes*, then:

### TypeScript / Node → [`ts/`](./ts) · guide: [`ts/README.md`](./ts/README.md)

```bash
npm install ./x402aff/ts     # from a clone; not published to npm
```
```ts
import { Affiliation } from "x402aff";

const aff = new Affiliation({ appCode: "bc_yourcode", sellerPayout: "0x…" });

const payTo = await aff.payToFor(req.headers);   // the split, or your wallet
const extensions = aff.extensions;               // declares your `a`
// payouts: const { calls, balanceUnits } = await aff.release("bc_alice");
```

Only dependency: `viem`. Or just vendor `ts/src/affiliation.ts` - it is one file.

### Python → [`python/`](./python) · guide: [`INTEGRATION.md`](./docs/INTEGRATION.md)

```bash
pip install ./x402aff/python     # from a clone; not published to PyPI
                                 # add [cdp] for the CDP discovery path
```
```python
from x402aff import Affiliation

aff = Affiliation(app_code="bc_yourcode", seller_payout=YOUR_WALLET)

# on your x402 route:
PaymentOption(..., pay_to=aff.pay_to)         # per-request split, from the header
RouteConfig(..., extensions=aff.extensions)   # declares your app code `a`

# release payouts, later (permissionless):
calls, balance = aff.release("bc_alice")
```

`aff.pay_to` is a drop-in x402 `DynamicPayTo` callback; `aff.pay_to_for(headers)`
is the sync form for other frameworks. Full wiring notes: [`docs/INTEGRATION.md`](./docs/INTEGRATION.md).

Both **never throw**: an unknown or unresolvable code falls back to your wallet -
the payment always works, it just isn't split. And the **buyer side is one line**:
Python ships `BuilderCodeClientExtension` (`x402aff.buyer_client`), TS uses the
official `@x402/extensions/builder-code` - no work on your users. Stamping `s`
by hand instead? `marked_service_codes()` / `markedServiceCodes()` build the
array for you.

---

## Options

### Change the cut

Basis points, default `1000` (10%). Set it once - env var or constructor:

```bash
export X402_BUILDER_SHARE_BPS=1500     # 15% builder / 85% you
```
```python
Affiliation(app_code="bc_yourcode", seller_payout=YOUR_WALLET, builder_share_bps=1500)
```

Range `0..10000` (`0` = no split, attribution only). **Heads-up:** the ratio is
baked into the split address, so changing it opens a *new* split per builder - old
funds stay safe and distribute at the old ratio.

### More than two recipients (platform fee, partner, …)

A split takes any recipients whose allocations sum to `10000`. Build a `SplitPlan`
directly (Python) and address-prediction + distribute just work - see the
**Customize** notes in [`INTEGRATION.md`](./docs/INTEGRATION.md).

### Release payouts

`distribute` is permissionless. `aff.pending()` (or [`monitor.py`](./python/x402aff/monitor.py) as a
CLI) discovers every builder who paid you - straight from CDP's index, no local
ledger - and shows which splits are ready to release, with the `cast` commands.

### Claims dashboard (`aff.splits_payload()`)

One call returns every per-builder split for your seller, ready to serialize
behind a `GET /splits` route: each row has the split address, codes + share, live
balance, deployed state, and a permissionless `[deploy?, distribute]` claim.
Because it reconstructs from the seller wallet the facade already holds, it
covers **undeployed** splits too (a counterfactual address can't be resolved
seller-side from the chain otherwise). Discovery is by your app code `a` via CDP
(Python bundles it; TS takes an injected `query` runner so the kit stays
viem-only). Mount it in one line and render the rows however you like - the claim
calls are permissionless, so anyone can trigger them.

There is deliberately **no per-split payment count**: producing one alongside the
received USDC amount means joining `base.events`, which trips the CDP SQL API's
leaf-scan limit (measured 94.44 GiB against a 93.13 GiB cap) and 400s. If you
want just the count, `python/x402aff/queries.sql` #5c gets it from the attribution table
alone - no join, and confirmed working. #5b documents why the amount can't come
cheaply.

```python
@app.get("/splits")                       # Python (Flask)
def splits(): return aff.splits_payload()
```
```ts
app.get("/splits", async (_req, res) =>   // TS (Express) — pass your CDP SQL runner
  res.json(await aff.splitsPayload(cdpQuery)));
```

### Discover every kit payment (any seller)

The buyer extension stamps a hardcoded, shared marker (`x402aff`) as a second `s`,
so **one** CDP query finds every kit-routed payment across all sellers - undeployed
splits included, no address reconstruction. Stamping `s` yourself instead of using
the kit's extension? Build the array with `marked_service_codes("bc_yourcode")`
(Python) / `markedServiceCodes("bc_yourcode")` (TS) so your payments stay
discoverable:

```sql
SELECT DISTINCT transaction_hash
FROM base.transaction_attributions
WHERE builder_code = 'x402aff' AND action = 1;
```

Run it in the [CDP SQL Playground](https://portal.cdp.coinbase.com/onchain-tools/sql-api),
or `POST https://api.cdp.coinbase.com/platform/v2/data/query/run` with a CDP JWT
(`Bearer`). Join those txs to the USDC `Transfer` to recover each payment's split
and read its claimable balance - see [`queries.sql`](./python/x402aff/queries.sql) #5 / #5b.

### Any other language (Go, Rust, …)

It's just three view-calls against two contracts - port
[`ts/src/affiliation.ts`](./ts/src/affiliation.ts) or [`python/x402aff/affiliation.py`](./python/x402aff/affiliation.py):

1. **code → payout** - `payoutAddress(uint256)` on the registry
   `0x000000BC7E6457e610fe52Dcc0ca5b3ce59C8E80` (token id = the code's ASCII bytes
   as a big-endian int).
2. **build the Split tuple** - `recipients = [builderPayout, seller]`,
   `allocations = [bps, 10000 - bps]`, `totalAllocation = 10000`, `incentive = 0`.
3. **tuple → address** - `isDeployed(Split, 0x0, 0x0)` on the factory
   `0x8E8eB0cC6AE34A38B67D5Cf91ACa38f60bc3Ecf4` returns your `payTo`.

---

## Good to know

- **Mainnet + CDP only.** Attribution is written on Base mainnet via the CDP
  facilitator; the registry/factory the x402aff kit reads only exist there.
- **Use a paid RPC.** Set `X402_BASE_RPC` - the public one `429`s, and a failed
  resolve silently falls back to your wallet (unsplit).
- **Distribute costs a few cents of gas** (the buyer's payment is gasless). Payouts
  land ~2 base units light: a split keeps 1 unit warm and floors each share.
- **`s` is a self-asserted tag** - resolving it says *where* the money goes (the
  code's registered payout), not who's *entitled* to it. Right level for an
  affiliate program.
- **Kit payments self-identify on-chain.** The buyer extension stamps a hardcoded,
  shared marker code (`x402aff`) as a second `s`, so every kit-routed payment is
  discoverable with **one** query - across all sellers, undeployed splits included -
  and it never changes a payout. See [`INTEGRATION.md`](./docs/INTEGRATION.md).

Full trust model, edge cases, and caveats: [`INTEGRATION.md`](./docs/INTEGRATION.md).

---

## Security

**The kit deploys no contracts of its own.** It authors nothing on-chain - it only
*reads* two canonical, third-party contracts and lets the stock CDP facilitator
settle into them. There's no new on-chain attack surface to audit here; the money
is held and split by code that already exists and is already audited.

- **0xSplits PushSplit** (`SplitFactoryV2` / `SplitWalletV2`, factory
  `0x8E8e…Ecf4`) holds each payment and splits it. Splits V2 was audited by **Zach
  Obront** (independent security researcher) ahead of its **May 2024** mainnet
  launch - report:
  [`splits-contracts-monorepo/audits/splits-v2.md`](https://github.com/0xSplits/splits-contracts-monorepo/blob/main/audits/splits-v2.md).
  The kit creates every split **ownerless** (`owner = 0`) and immutable, so once
  USDC lands, the ratio is fixed and nobody - not even the seller - can redirect,
  pause, or claw back the builder's cut.
- **Base Builder Codes registry** (`0x000000BC…C8E80`) maps each builder code to
  the payout address its owner registered. The kit only reads it, so a builder's
  cut can only ever go to the address that builder registered.

### What the split guarantees - proven against live mainnet

Money in a split can only ever be paid to the two baked-in recipients (builder +
seller) at the baked ratio, no matter who calls anything. This isn't asserted,
it's tested: the Foundry suite in [`fork-test/`](./fork-test) forks Base mainnet
and runs **7 tests** - including **5 adversarial** ones in
[`SplitAbuse.t.sol`](./fork-test/test/SplitAbuse.t.sol) that actively try to break
it and fail:

| Attack tried against a funded split | Result |
|---|---|
| Trigger `distribute` yourself, name yourself the distributor | builder 10% / seller 90%; you get **0** (`distributionIncentive = 0`) |
| `distribute` with a tampered struct (swap recipient / flip ratio / add a skim) | **reverts** - the wallet hash-checks the struct; nothing moves |
| Deploy a hijacking split that pays you, at the funded address | **impossible** - different params → different CREATE2 address, no funds there |
| `updateSplit` / `setPaused` to redirect or freeze (seller *or* attacker) | **reverts** - the split is ownerless |
| Front-run the deterministic deploy | same address, same recipients; still pays builder/seller |

The two-layer lock behind this: the split **address is CREATE2 over its params**
(funds only ever accrue at the address for exactly those recipients), and
`distribute` **re-validates the passed struct against the hash stored at creation**
(so even at that address the money can't go anywhere else).

### What this is *not*

`s` is a self-asserted tag, not a signed proof of who drove a payment - resolving
it says *where* the money goes (the code's registered payout), not who is
*entitled* to it. That's the right level for an affiliate program, but it's a
routing opt-in, not trustless attribution. And a resolve failure (or an unknown
code) always falls back to the seller wallet - the payment never fails, it just
isn't split. Full trust model: [`INTEGRATION.md` §6](./docs/INTEGRATION.md).

**No keys, no custody.** The kit never holds funds or signs anything: `payTo` is
just an address in the 402, settlement is the buyer's gasless CDP payment, and
`distribute` is a permissionless call anyone pays gas for. Nothing here needs a
private key.

---

## What's in the box

| Path | What it is |
|------|------------|
| [`ts/`](./ts) · [`python/`](./python) | **Start here** - the `Affiliation` facades (TypeScript + Python). |
| [`docs/INTEGRATION.md`](./docs/INTEGRATION.md) | Python integration deep-dive (money path, trust model, wiring, caveats). |
| [`fork-test/`](./fork-test) | Foundry proof: CDP-settle → split → distribute against live Base contracts on a mainnet fork. |
| `python/x402aff/{resolver,split,push_split,payto,distribute,monitor}.py` | The primitives the facades wrap (code→payout, split plan, 0xSplits calldata, request-time payTo, release, monitoring). |

```bash
cd python && pytest            # Python unit tests
cd ts && npm test              # TypeScript tests
cd fork-test && forge test     # mainnet-fork proof
```

---

## References

- [ERC-8021 builder-code spec](https://github.com/x402-foundation/x402/blob/main/specs/extensions/builder_code.md) · [CDP Builder Codes](https://docs.cdp.coinbase.com/x402/core-concepts/builder-codes)
- [Base Builder Codes](https://docs.base.org/apps/builder-codes/builder-codes) · registry [`github.com/base/builder-codes`](https://github.com/base/builder-codes)
- [0xSplits PushSplit V2](https://splits.org/protocol/docs/core/split-v2) · factory `0x8E8eB0cC6AE34A38B67D5Cf91ACa38f60bc3Ecf4`

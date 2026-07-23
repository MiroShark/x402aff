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

```ts
import { Affiliation } from "x402aff";

const aff = new Affiliation({ appCode: "bc_yourcode", sellerPayout: "0x…" });

const payTo = await aff.payToFor(req.headers);   // the split, or your wallet
const extensions = aff.extensions;               // declares your `a`
// payouts: const { calls, balanceUnits } = await aff.release("bc_alice");
```

Only dependency: `viem`.

### Python → [`python/`](./python) · guide: [`INTEGRATION.md`](./docs/INTEGRATION.md)

```python
from affiliation import Affiliation

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
the payment always works, it just isn't split. And the **buyer side is one line** in
the official TS extension (`BuilderCodeClientExtension`) - no work on your users.

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

`distribute` is permissionless. `aff.pending()` (or [`monitor.py`](./python/monitor.py) as a
CLI) discovers every builder who paid you - straight from CDP's index, no local
ledger - and shows which splits are ready to release, with the `cast` commands.

### Discover every kit payment (any seller)

The buyer extension stamps a hardcoded, shared marker (`x402aff`) as a second `s`,
so **one** CDP query finds every kit-routed payment across all sellers - undeployed
splits included, no address reconstruction:

```sql
SELECT DISTINCT transaction_hash
FROM base.transaction_attributions
WHERE builder_code = 'x402aff' AND action = 1;
```

Run it in the [CDP SQL Playground](https://portal.cdp.coinbase.com/onchain-tools/sql-api),
or `POST https://api.cdp.coinbase.com/platform/v2/data/query/run` with a CDP JWT
(`Bearer`). Join those txs to the USDC `Transfer` to recover each payment's split
and read its claimable balance - see [`queries.sql`](./python/queries.sql) #5 / #5b.

### Any other language (Go, Rust, …)

It's just three view-calls against two contracts - port
[`ts/src/affiliation.ts`](./ts/src/affiliation.ts) or [`python/affiliation.py`](./python/affiliation.py):

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

## What's in the box

| Path | What it is |
|------|------------|
| [`ts/`](./ts) · [`python/`](./python) | **Start here** - the `Affiliation` facades (TypeScript + Python). |
| [`docs/INTEGRATION.md`](./docs/INTEGRATION.md) | Python integration deep-dive (money path, trust model, wiring, caveats). |
| [`fork-test/`](./fork-test) | Foundry proof: CDP-settle → split → distribute against live Base contracts on a mainnet fork. |
| `python/{resolver,split,push_split,payto,distribute,monitor}.py` | The primitives the facades wrap (code→payout, split plan, 0xSplits calldata, request-time payTo, release, monitoring). |

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

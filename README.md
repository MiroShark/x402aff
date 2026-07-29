# X402aff - Builder-code affiliation for x402 sellers

[![PyPI](https://img.shields.io/pypi/v/x402aff?logo=pypi&logoColor=white&label=pypi)](https://pypi.org/project/x402aff/)
[![npm](https://img.shields.io/npm/v/x402aff?logo=npm&logoColor=white&label=npm)](https://www.npmjs.com/package/x402aff)
[![CI](https://github.com/MiroShark/x402aff/actions/workflows/ci.yml/badge.svg)](https://github.com/MiroShark/x402aff/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](./LICENSE)
[![Dashboard](https://img.shields.io/badge/dashboard-miroshark.xyz%2Fx402aff-blue)](https://www.miroshark.xyz/x402aff)

**Give the apps that send you paying users a cut - enforced on-chain, at
settlement, with no facilitator of your own to run.**

If you sell an API behind an [x402](https://x402.org) paywall, this splits each
payment so the builder whose app drove it earns a share (default **10%**). The cut
lands in an ownerless, immutable [0xSplits](https://splits.org) contract at
settlement, so once it's there **nobody - including you - can redirect it**. That's
what makes it a credible offer rather than a promise.

No settler, no self-run facilitator, no contract you write or audit: the stock CDP
facilitator settles (gas sponsored), an audited 0xSplits contract splits, and the
builder adds *one line* to their x402 client.

> Validated on **Base mainnet** and reproducible on a mainnet fork - see the
> Foundry proof in [`fork-test/`](./fork-test), and a live seller's claims
> dashboard at [miroshark.xyz/x402aff](https://miroshark.xyz/x402aff).

---

## How it works

Base [Builder Codes](https://docs.cdp.coinbase.com/x402/core-concepts/builder-codes)
put three tags on a paid request: **`a`** (you, the API), **`s`** (the builder that
drove it), **`w`** (the facilitator). The kit turns `s` into money with one move:
**set the route's `payTo` to a per-`(you, builder)` 0xSplits split.**

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

Payouts aren't atomic: money settles into the split on its own, but **releasing**
it (`distribute`) is a separate permissionless call - you, a keeper, or the builder
can trigger it.

---

## Get started - pick your language

Both ship a one-object `Affiliation` facade and resolve the **identical** split
address (the TS encoding is asserted byte-for-byte against Python). Get an app code
at [base.dev](https://base.dev) → *Settings → Builder Codes*, then:

### TypeScript / Node → [`ts/`](./ts) · guide: [`ts/README.md`](./ts/README.md)

```bash
npm install x402aff viem     # viem is a peer dependency
```
```ts
import { Affiliation } from "x402aff";

const aff = new Affiliation({ appCode: "bc_yourcode", sellerPayout: "0x…" });

const payTo = await aff.payToFor(req.headers);   // the split, or your wallet
const extensions = aff.extensions;               // declares your `a`
// payouts: const { calls, balanceUnits } = await aff.release("bc_alice");
```

Only dependency: `viem`, kept as a peer so you share the one your app already
has. Or just vendor `ts/src/affiliation.ts` - it is one file, and it ships in the
published tarball too.

### Python → [`python/`](./python) · guide: [`INTEGRATION.md`](./docs/INTEGRATION.md)

```bash
pip install x402aff              # add 'x402aff[cdp]' for the CDP discovery path
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
is the sync form for other frameworks.

Neither ever throws: an unknown or unresolvable code falls back to your wallet, so
the payment always works, it just isn't split. **The buyer side is one line** -
Python ships `BuilderCodeClientExtension` (`x402aff.buyer_client`), TS uses the
official `@x402/extensions/builder-code`. Stamping `s` by hand instead?
`marked_service_codes()` / `markedServiceCodes()` build the array for you.

---

## Options

**Change the cut.** Basis points, default `1000` (10%), range `0..10000` (`0` =
attribution only). Set `X402_BUILDER_SHARE_BPS=1500` or pass
`builder_share_bps=1500`. The ratio is baked into the split address, so changing it
opens a *new* split per builder - old funds stay safe at the old ratio.

**More than two recipients** (platform fee, partner, …). Any recipients whose
allocations sum to `10000`: build a `SplitPlan` directly and address-prediction +
distribute just work.

**Release payouts.** `aff.pending()` (or `python3 -m x402aff.monitor` as a CLI)
discovers every builder who paid you straight from CDP's index - no local ledger -
and shows which splits are ready, with the `cast` commands.

### Claims dashboard (`aff.splits_payload()`)

One call returns every per-builder split for your seller, ready to serialize behind
a `GET /splits` route: split address, codes + share, live balance, deployed state,
and a permissionless `[deploy?, distribute]` claim. It reconstructs from the seller
wallet the facade already holds, so it covers **undeployed** splits too - a
counterfactual address can't be recovered seller-side from the chain otherwise.

**See one running:** [miroshark.xyz/x402aff](https://miroshark.xyz/x402aff) serves
this payload live from a Base-mainnet seller. Every split, balance, and claim on
that page comes from `aff.splits_payload()`, and the claim button is the
permissionless `distribute` - anyone can trigger it, and the funds still go only to
the split's baked-in recipients.

```python
@app.get("/splits")                       # Python (Flask)
def splits(): return aff.splits_payload()
```
```ts
app.get("/splits", async (_req, res) =>   // TS (Express) - pass your CDP SQL runner
  res.json(await aff.splitsPayload(cdpQuery)));
```

Deliberately **no per-split payment count**: getting one alongside the received
USDC amount means joining `base.events`, which trips the CDP SQL API's leaf-scan
limit (measured 94.44 GiB against a 93.13 GiB cap) and 400s. For just the count,
[`queries.sql`](./python/x402aff/queries.sql) #5c reads the attribution table alone
- no join, confirmed working. #5b records why the amount can't come cheaply.

### Discover every kit payment (any seller)

The buyer extension stamps a hardcoded, shared marker (`x402aff`) as a second `s`,
so **one** query finds every kit-routed payment across all sellers - undeployed
splits included, no address reconstruction, and it never changes a payout (the
split pays the primary code):

```sql
SELECT DISTINCT transaction_hash
FROM base.transaction_attributions
WHERE builder_code = 'x402aff' AND action = 1;
```

Run it in the [CDP SQL Playground](https://portal.cdp.coinbase.com/onchain-tools/sql-api)
(no auth) or `POST https://api.cdp.coinbase.com/platform/v2/data/query/run` with a
CDP JWT. Join those txs to the USDC `Transfer` to recover each split and read its
claimable balance - [`queries.sql`](./python/x402aff/queries.sql) #5 / #5b.

### Any other language (Go, Rust, …)

Three view-calls against two contracts - port
[`ts/src/affiliation.ts`](./ts/src/affiliation.ts) or
[`python/x402aff/affiliation.py`](./python/x402aff/affiliation.py):

1. **code → payout** - `payoutAddress(uint256)` on the registry
   `0x000000BC7E6457e610fe52Dcc0ca5b3ce59C8E80` (token id = the code's ASCII bytes
   as a big-endian int).
2. **build the Split tuple** - `recipients = [builderPayout, seller]`,
   `allocations = [bps, 10000 - bps]`, `totalAllocation = 10000`, `incentive = 0`.
3. **tuple → address** - `isDeployed(Split, 0x0, 0x0)` on the factory
   `0x8E8eB0cC6AE34A38B67D5Cf91ACa38f60bc3Ecf4` returns your `payTo`.

---

## Good to know

- **Mainnet + CDP only.** The registry and factory the kit reads exist only on Base
  mainnet, and attribution is written by the CDP facilitator.
- **Use a paid RPC.** Set `X402_BASE_RPC` - the public one `429`s, and a failed
  resolve silently falls back to your wallet (unsplit).
- **Distribute costs a few cents of gas** (the buyer's payment is gasless). Payouts
  land ~2 base units light: a split keeps 1 unit warm and floors each share.
- **`s` is a self-asserted tag**, not signed proof of who drove a payment. Resolving
  it says *where* money goes (the code's registered payout), not who is *entitled*
  to it - a routing opt-in, the right level for an affiliate program.
- **No keys, no custody.** `payTo` is just an address in the 402, settlement is the
  buyer's gasless CDP payment, and `distribute` is permissionless. Nothing here
  needs a private key.

Full trust model, edge cases, and caveats: [`INTEGRATION.md`](./docs/INTEGRATION.md).

---

## Security

**The kit deploys no contracts of its own.** It only *reads* two canonical
third-party contracts and lets the stock CDP facilitator settle into them - no new
on-chain attack surface, and the money is held by code that is already audited.

- **0xSplits PushSplit** (factory `0x8E8e…Ecf4`) holds and splits each payment.
  Splits V2 was audited by **Zach Obront** ahead of its **May 2024** launch
  ([report](https://github.com/0xSplits/splits-contracts-monorepo/blob/main/audits/splits-v2.md)).
  Every split is created **ownerless** (`owner = 0`) and immutable.
- **Base Builder Codes registry** (`0x000000BC…C8E80`) maps a code to the payout its
  owner registered. Read-only, so a builder's cut can only go where that builder said.

### What the split guarantees - proven against live mainnet

Money in a split can only ever reach the two baked-in recipients at the baked ratio,
no matter who calls what. Not asserted - tested: [`fork-test/`](./fork-test) forks
Base mainnet and runs **7 tests**, including **5 adversarial** ones in
[`SplitAbuse.t.sol`](./fork-test/test/SplitAbuse.t.sol):

| Attack tried against a funded split | Result |
|---|---|
| Trigger `distribute` yourself, name yourself the distributor | builder 10% / seller 90%; you get **0** (`distributionIncentive = 0`) |
| `distribute` with a tampered struct (swap recipient / flip ratio / add a skim) | **reverts** - the wallet hash-checks the struct |
| Deploy a hijacking split that pays you, at the funded address | **impossible** - different params → different CREATE2 address |
| `updateSplit` / `setPaused` to redirect or freeze (seller *or* attacker) | **reverts** - the split is ownerless |
| Front-run the deterministic deploy | same address, same recipients; still pays builder/seller |

Two locks make that hold: the address is **CREATE2 over its params**, so funds only
accrue at the address for exactly those recipients; and `distribute` **re-validates
the struct against the hash stored at creation**, so even there the money can't move
elsewhere.

---

## What's in the box

| Path | What it is |
|------|------------|
| [`ts/`](./ts) · [`python/`](./python) | **Start here** - the `Affiliation` facades. |
| [`docs/INTEGRATION.md`](./docs/INTEGRATION.md) | Integration deep-dive (money path, trust model, wiring, caveats). |
| [`fork-test/`](./fork-test) | Foundry proof against live Base contracts on a mainnet fork. |
| `python/x402aff/{resolver,split,push_split,payto,distribute,monitor}.py` | The primitives the facades wrap. |

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

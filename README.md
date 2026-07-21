# x402 Builder-Code Affiliation Kit

**Give the apps that send you paying users a cut — enforced on-chain, at
settlement, with no facilitator of your own to run.**

If you sell an API behind an [x402](https://x402.org) paywall, this is the whole
machinery for: *"when someone's app pays me on behalf of their user, split the
payment so that app's builder earns a share — automatically, verifiably, on
Base."*

> Validated end-to-end on **Base mainnet**: a real browser payment settled
> through the Coinbase CDP facilitator into a per-builder [0xSplits](https://splits.org)
> split, with `a`/`s`/`w` attribution written on-chain (and decoded back out by
> this kit's own parser). See [`RUNBOOK-live-test.md`](./RUNBOOK-live-test.md) and
> the live endpoint in [`test_endpoint/`](./test_endpoint).

---

## What it enables

Turn your x402 endpoint into an **affiliate program** with no backend for it:

- **Referral revenue-share, on autopilot.** A wallet, agent, or app that drives a
  payment to your API earns a cut (default **10%**). You keep the rest.
- **Enforced, not promised.** The cut lands in an ownerless, immutable
  [0xSplits](https://splits.org) contract at settlement. Once the money's there,
  **nobody — including you — can redirect the builder's share.** That's what makes
  it a credible offer to builders.
- **No new infrastructure.** No settler, no self-run facilitator, no smart
  contract you write or audit. The stock **Coinbase CDP facilitator** does the
  settling (gas sponsored); an audited 0xSplits contract does the splitting.
- **Stacks with Base's builder rewards** on the volume you drive.
- **Zero-friction for the builder.** They add *one line* to their x402 client to
  attach their code. They never touch your code or your repo.

The catch, stated plainly: payouts aren't atomic. Money settles into the split
correctly on its own, but **releasing** it (`distribute`) is a separate,
permissionless call — you, a keeper, or the builder can trigger it.
[`monitor.py`](./monitor.py) shows which splits are holding funds.

---

## How it works (the one idea)

[Base Builder Codes](https://docs.cdp.coinbase.com/x402/core-concepts/builder-codes)
are the **ERC-8021 Schema 2** on-chain attribution standard for x402. Three codes
ride on a paid request:

| Code | Who it names | Who sets it |
|------|--------------|-------------|
| **`a`** app code | **you** (the API / resource server) | you declare it on the route |
| **`s`** service / referer | the **buyer's app** that drove the payment | the buyer's client |
| **`w`** wallet code | the **CDP facilitator** | the facilitator, at settlement |

`s` is the affiliation you care about — it names *which builder* to pay. The kit
turns that into money with one move:

**Set the route's `payTo` to a per-`(you, builder)` 0xSplits PushSplit.** The
buyer's app names its builder on the request, your server points `payTo` at that
pair's split, and the **CDP facilitator settles straight into it** — sponsored
gas, `a`/`s`/`w` still written on-chain.

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

---

## Integrate it (one object)

Already run an x402 route? The whole integration is one configured object —
[`affiliation.py`](./affiliation.py) — that folds the pieces below behind a couple
of lines.

Get an app code at [base.dev](https://base.dev) → *Settings → Builder Codes*, then:

```python
from affiliation import Affiliation

aff = Affiliation(app_code="bc_yourcode", seller_payout=YOUR_WALLET)
```

### 1 + 2. Declare `a` and route `payTo` — two attributes on your route

```python
PaymentOption(scheme="exact", price="$0.02", network="eip155:8453",
              pay_to=aff.pay_to)            # ← per-request split, from the header
RouteConfig(..., extensions=aff.extensions) # ← declares your app code `a`
```

- **`aff.extensions`** declares `a`. It rides inside the base64'd `402` header;
  the CDP facilitator appends the on-chain `a`/`s`/`w` suffix at settlement — your
  server builds no transaction.
- **`aff.pay_to`** is a drop-in x402 `DynamicPayTo` callback. When the buyer's app
  names its builder (an `X-Builder-Code` header), it returns that pair's split
  address; otherwise your own wallet. It resolves + caches in one `eth_call` and
  **never raises** — a missing, unknown, or unresolvable code falls back to your
  wallet, so the payment always works; it just isn't split.

Not on the Python x402 SDK? `aff.pay_to_for(request.headers)` is the sync
equivalent (works with any headers mapping), and the whole thing is portable — see
**Any x402 stack** below. See
[`test_endpoint/app.py`](./test_endpoint/app.py) for the full FastAPI wiring.

> **Why a header?** `payTo` is locked when the 402 goes out, but the standard `s`
> code only arrives *inside* the payment. So the buyer's app opts in by naming its
> builder at request time (one header, alongside the usual `s` extension). Buyers
> that don't send it pay you normally, unsplit.

Then point the route at the **CDP facilitator** (mainnet) and you're done on the
request path — CDP settles a plain USDC transfer into the split and writes
`a`/`s`/`w`. Nothing else to do here; the split just fills.

### 3. Distribute (release the cut)

Funds accumulate in each pair's split until someone calls `distribute`. It's
permissionless — you, a keeper, or the builder can trigger it:

```python
calls, balance = aff.release("bc_alice")   # deploy (first use) + distribute calldata
# submit each (target, data) from any funded Base account — gas is cents

for s in aff.pending():                     # every split holding distributable funds
    print(s.builder_code, s.distributable_units)
```

`aff.pending()` discovers every builder who paid you (straight from CDP's
attribution index — no local ledger); [`monitor.py`](./monitor.py) is the same
scan as a CLI, and prints which splits are ready:

```
$ X402_BUILDER_CODE=bc_yourcode X402_SELLER_PAYOUT=0x… python3 monitor.py

builder code                balance  distributable  deployed  status
──────────────────────────────────────────────────────────────────
bc_alice                  $12.400000     $12.399999     True   ◀ DISTRIBUTE
bc_bob                     $0.000000      $0.000000     False   —
```

That's the whole integration. Everything below is customization, other-language
ports, and the fine print.

---

## Customize the payout split

### Change the cut

The builder's share is basis points (`10000` = 100%). Default is `1000` (10%).
Set it globally with an env var, or per-request in code:

```bash
export X402_BUILDER_SHARE_BPS=1500        # 15% to the builder, 85% to you
```

```python
# per seller — overrides the env default
aff = Affiliation(app_code="bc_yourcode", seller_payout=YOUR_WALLET, builder_share_bps=2500)
```

Range is `0..10000`. `0` disables the split (100% to you); the code still gets
`a`/`s`/`w` attribution on-chain, just no revenue share.

### Heads-up: the ratio is part of the address

Each split's address is derived (CREATE2) from **its recipients *and* their
allocations**. So changing `builder_share_bps` produces a *different* split
address for the same builder:

- Payments made **before** the change stay in the old split and distribute at the
  **old** ratio (it's immutable — that's the guarantee).
- Payments **after** the change route to a new address at the new ratio.

Pick your default and keep it stable per builder. Changing it is safe (no money is
lost) but effectively opens a second split for that builder.

### More than two recipients (a platform fee, a partner, …)

The default plan is two-way (builder, seller), but a PushSplit takes any number of
recipients whose allocations sum to `10000`. Build a `SplitPlan` directly and the
rest of the kit — address prediction, distribute — just works:

```python
import split, push_split, distribute

plan = split.SplitPlan(
    seller_payout=YOUR_WALLET,
    builder_code="bc_alice",
    builder_payout="0xBuilder…",
    builder_share_bps=1000,
    recipients=[                     # must sum to 10000
        ("0xBuilder…",   1000),      # 10% builder
        ("0xPlatform…",   500),      #  5% platform fee
        (YOUR_WALLET,    8500),      # 85% you
    ],
)
address, deployed = push_split.predict_split_address(plan)   # set payTo = this
calls, balance    = distribute.distribute_plan(plan)         # releases all three
```

Payout math (`plan.amounts_units`) mirrors Splits exactly for any recipient
count, so your ledger still reconciles to the unit.

---

## Any x402 stack (Node, Go, Rust, …)

The kit ships in Python, but **the method is language-agnostic** — it's just
"advertise the split's address as `payTo`." Any x402 server, in any language, does
the same three reads. Only two on-chain contracts are involved:

- **Builder Codes registry** `0x000000BC7E6457e610fe52Dcc0ca5b3ce59C8E80` — code → payout address
- **0xSplits PushSplitFactory** `0x8E8eB0cC6AE34A38B67D5Cf91ACa38f60bc3Ecf4` — the pair → its deterministic split address

### TypeScript / Node — shipped and tested

The [`ts/`](./ts) package is a full port of `affiliation.py` (same `Affiliation`
surface, only `viem` as a dependency). Its tests assert the encoding **byte-for-byte
against the Python/`cast` bytes**, so it resolves the *identical* split address:

```ts
import { Affiliation } from "@x402-affiliation/kit";

const aff = new Affiliation({ appCode: "bc_yourcode", sellerPayout: "0x…" });
const payTo = await aff.payToFor(req.headers);   // the split, or your wallet
const extensions = aff.extensions;               // declares your `a`
// payouts: const { calls, balanceUnits } = await aff.release("bc_alice");
```

See [`ts/README.md`](./ts/README.md). The recipe below is the same logic spelled
out for **any other language** (Go, Rust, …).

### Buyer side — already official, everywhere

The builder attaches their code with the **official x402 extension** (TypeScript /
JS). No port needed:

```ts
import { BuilderCodeClientExtension } from "@x402/extensions/builder-code";

client.registerExtension(new BuilderCodeClientExtension("bc_yourcode")); // attaches s
// …and send header  X-Builder-Code: bc_yourcode  on the request → payTo routes to the split
```

(See [`test_endpoint/try.html`](./test_endpoint/try.html) for the full browser
flow.) A TS **seller** can likewise declare `a` with that same official package.

### Seller side — the recipe (three view-calls)

To resolve `payTo` for a request in any language:

1. **code → payout.** `payoutAddress(uint256)` on the registry, where the token id
   is the code's ASCII bytes read as a big-endian integer. (Reverts / zero →
   unregistered: fall back to your wallet.)
2. **build the Split tuple.** `recipients = [builderPayout, sellerPayout]`,
   `allocations = [builderBps, 10000 - builderBps]`, `totalAllocation = 10000`,
   `distributionIncentive = 0`.
3. **tuple → address.** `isDeployed(Split, owner=0x0, salt=0x0)` on the factory
   returns `(address, bool)`. That address is your `payTo`.

Here's 1–3 in TypeScript with `viem` — the core of what the [`ts/`](./ts) package
ships, condensed:

```ts
import { createPublicClient, http } from "viem";
import { base } from "viem/chains";

const client   = createPublicClient({ chain: base, transport: http(process.env.BASE_RPC) });
const REGISTRY = "0x000000BC7E6457e610fe52Dcc0ca5b3ce59C8E80";
const FACTORY  = "0x8E8eB0cC6AE34A38B67D5Cf91ACa38f60bc3Ecf4";
const ZERO32   = "0x" + "00".repeat(32);

// builder code → ERC-721 token id (ASCII bytes as a big-endian int)
const toTokenId = (code: string) => BigInt("0x" + Buffer.from(code, "ascii").toString("hex"));

// 1. code → registered payout address (null when unregistered)
async function payoutOf(code: string) {
  try {
    const addr = await client.readContract({
      address: REGISTRY,
      abi: [{ name: "payoutAddress", type: "function", stateMutability: "view",
              inputs: [{ type: "uint256" }], outputs: [{ type: "address" }] }],
      functionName: "payoutAddress", args: [toTokenId(code)],
    });
    return BigInt(addr) === 0n ? null : addr;
  } catch { return null; }
}

const SPLIT_ABI = [{ name: "isDeployed", type: "function", stateMutability: "view",
  inputs: [
    { name: "split", type: "tuple", components: [
      { name: "recipients", type: "address[]" }, { name: "allocations", type: "uint256[]" },
      { name: "totalAllocation", type: "uint256" }, { name: "distributionIncentive", type: "uint16" }]},
    { name: "owner", type: "address" }, { name: "salt", type: "bytes32" }],
  outputs: [{ type: "address" }, { type: "bool" }] }] as const;

// 2 + 3. (seller, builder) → the deterministic PushSplit address → your payTo
async function payToFor(seller: string, code: string, builderBps = 1000) {
  const builder = await payoutOf(code);
  if (!builder) return seller;                       // no/unknown code → unsplit
  const split = {
    recipients: [builder, seller],
    allocations: [BigInt(builderBps), BigInt(10000 - builderBps)],
    totalAllocation: 10000n, distributionIncentive: 0,
  };
  const [address] = await client.readContract({
    address: FACTORY, abi: SPLIT_ABI, functionName: "isDeployed",
    args: [split, "0x0000000000000000000000000000000000000000", ZERO32],
  });
  return address;                                    // set the route's payTo to this
}
```

Wire `payToFor(...)`'s result into however your x402 server middleware exposes a
per-request / dynamic `payTo`. Distribute later with the same factory's
`createSplitDeterministic` (first use) + the split's `distribute(Split, USDC,
distributor)` — see [`push_split.py`](./push_split.py) for the exact calldata,
verified byte-for-byte against the live Base factory.

---

## The money, precisely

- The buyer's payment settles **in full into the split** at request time.
- The split pays the configured cut (default **10% builder / 90% you**).
- Payouts land ~2 base units light: a PushSplit keeps 1 unit warm and floors each
  share, so a $1.00 payment pays `$0.099999 / $0.899999`. `split.amounts_units()`
  mirrors that exactly, so your ledger reconciles to the unit against the settle tx.

The trust level, honestly: because `s` is a self-asserted tag, resolving it tells
you *where the money goes* (the code owner's registered payout), not that the
submitter is *entitled* to that code. For an affiliate program that's fine — the
registered owner gets paid regardless of who drove the traffic.

---

## Which wallet owns a builder code?

Base Builder Codes are an **ERC-721 NFT collection**
([`github.com/base/builder-codes`](https://github.com/base/builder-codes)):
registering a code mints a token whose onchain metadata declares a **payout
address**. Mapping a code → wallet is a plain onchain read — no API key, no
indexer.

- **Registry (Base mainnet):** [`0x000000BC7E6457e610fe52Dcc0ca5b3ce59C8E80`](https://basescan.org/address/0x000000BC7E6457e610fe52Dcc0ca5b3ce59C8E80)
  — a verified ERC1967 proxy, ERC-721 "Builder Codes".
- **Pay `payout_address`, never `owner`.** For the free auto-generated `bc_*`
  codes, base.dev holds the NFT custodially, so `owner` is the registrar. The
  split always pays `payout_address`.
- **Token ID = the code's ASCII bytes as a big-endian integer.**
  `resolver.to_token_id("leap_wallet")` matches the contract exactly. IDs exceed
  JS's safe-integer range — handle as big integers.

```python
import resolver
resolver.resolve("leap_wallet")
# {'code': 'leap_wallet', 'registered': True,
#  'owner': '0xf9d7…8116', 'payout_address': '0xa06c…54c8'}
```

---

## Run it & what's in the box

```bash
pip install cbor2 requests pytest        # core deps
pip install cdp-sdk                       # optional: monitor.py's CDP SQL auth
python3 -m pytest -q                       # declare/decode + split math + payTo + Splits calldata

cd fork-test && forge test                # 2 tests: CDP settle → split → distribute, on a Base mainnet fork
```

To stand up a live endpoint and take a real payment, see
[`test_endpoint/`](./test_endpoint) and [`RUNBOOK-live-test.md`](./RUNBOOK-live-test.md).

| File | What it is |
|------|------------|
| **`affiliation.py`** | **Start here.** The `Affiliation` facade — one object for the whole integration: `aff.pay_to` (route callback), `aff.extensions` (declare `a`), `aff.release()` / `aff.pending()` (payouts). Wraps everything below. |
| **`builder_code.py`** | The core. `declare_builder_code()` (declare `a`) + `parse_builder_code_suffix()` (decode `a`/`s`/`w` from a settle tx). No framework, no db. |
| **`resolver.py`** | **Code → payout wallet.** Resolves any builder code to its registered payout via the Base ERC-721 registry (raw `eth_call`, no keys). |
| **`split.py`** | **Split plan.** A builder code + price → the recipient set + bps a per-pair PushSplit encodes. Pure arithmetic. |
| **`push_split.py`** | **0xSplits v2 calldata + address.** `predict_split_address()` (the pair's counterfactual split, one `eth_call`) + the deploy/distribute calldata. No web3 dep. |
| **`payto.py`** | **The method.** `X-Builder-Code` header → the split address to advertise as `payTo`. Never raises — any failure falls back to your wallet (unsplit, never failed). |
| **`distribute.py`** | **Release a funded split.** Emits the (deploy + distribute) calldata that fans a split out to its recipients. Permissionless. |
| **`monitor.py`** | **What's owed.** Discovers every builder who paid you (from CDP's index) and reports which splits are holding distributable funds. |
| **`buyer_client.py`** | Buyer side: the client extension a builder registers to attach their code and earn. |
| **`cdp_sql.py`** · **`queries.sql`** | Thin CDP SQL API client + copy-paste attribution queries (used by `monitor.py`; also runnable in the no-auth Playground). |
| **`test_endpoint/`** | A deployable FastAPI x402 endpoint on this exact path — plus `try.html`, a one-page browser client to pay it live. |
| **`fork-test/`** | Foundry test running CDP-settle → split → distribute against **live** Base USDC + PushSplitFactory on a mainnet fork. |

---

## Caveats (read before shipping)

- **Mainnet + CDP only.** Codes are only written on-chain on **Base mainnet via
  the Coinbase CDP facilitator**. On testnet / the free `x402.org` facilitator the
  declaration is harmless but nothing lands — and the registry/factory this kit
  reads only exist on mainnet.
- **Use a paid RPC.** `resolver.py` defaults to `https://mainnet.base.org`, which
  `429`s after a few calls in a row. Set `X402_BASE_RPC` — a rate-limited resolve
  means a builder silently isn't split (it falls back to your wallet).
- **One split per builder, deployed once.** Each `(you, builder)` pair has a
  deterministic split address, deployed lazily on its first `distribute` and
  reused forever after. The deploy is a one-time ~cents gas cost per builder.
- **Distribute costs gas.** The buyer's payment is gasless (CDP-sponsored), but
  `distribute` is a plain tx — the caller pays a few cents of ETH, earns nothing
  (`distributionIncentive = 0`), and can only send funds to the split's fixed
  recipients.
- **Hand-rolled by necessity.** The x402 *Python* SDK ships no builder-code
  module, so `builder_code.py` declares + decodes directly (on TypeScript use the
  official `@x402/extensions/builder-code`). Verify against a real settlement
  before trusting attribution.

## References

- [ERC-8021 builder-code extension spec](https://github.com/x402-foundation/x402/blob/main/specs/extensions/builder_code.md)
- [CDP Builder Codes](https://docs.cdp.coinbase.com/x402/core-concepts/builder-codes) · [CDP SQL API](https://docs.cdp.coinbase.com/data/sql-api/schema)
- [Base Builder Codes](https://docs.base.org/apps/builder-codes/builder-codes) · registry [`github.com/base/builder-codes`](https://github.com/base/builder-codes)
- [0xSplits PushSplit V2](https://splits.org/protocol/docs/core/split-v2) · factory `0x8E8eB0cC6AE34A38B67D5Cf91ACa38f60bc3Ecf4`

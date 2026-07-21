# x402 Builder-Code Affiliation Kit

Pay the builders whose apps drive payments to your x402 API — **enforced
on-chain, at settlement, with no facilitator of your own to run.**

If you sell an API behind an x402 paywall, this is the whole machinery for:
"when someone's app pays me on behalf of their user, split the payment so that
app's builder gets a cut — automatically, verifiably, on Base."

> Validated end-to-end on **Base mainnet**: a real browser payment settled through
> the Coinbase CDP facilitator into a per-builder 0xSplits split, with `a`/`s`/`w`
> written on-chain (decoded by this kit's own parser). See
> [`RUNBOOK-live-test.md`](./RUNBOOK-live-test.md) and the live endpoint under
> [`test_endpoint/`](./test_endpoint).

---

## The one idea

[Base Builder Codes](https://docs.cdp.coinbase.com/x402/core-concepts/builder-codes)
are the **ERC-8021 Schema 2** on-chain attribution standard for x402. Three codes
can ride on a paid request:

| Code | Who it names | Who sets it |
|------|--------------|-------------|
| **`a`** app code | **you** (the API / resource server) | you declare it on the route |
| **`s`** service / referer | the **buyer's app** that drove the payment | the buyer's client |
| **`w`** wallet code | the **CDP facilitator** | the facilitator, at settlement |

`s` is the affiliation you care about — it names *which builder* to pay. The kit
turns that into money with one move:

**Set the route's `payTo` to a per-`(you, builder)` [0xSplits](https://splits.org)
PushSplit.** The buyer's app names its builder on the request, your server sets
`payTo` to that pair's split, and the **stock CDP facilitator settles straight
into it** — sponsored gas, `a`/`s`/`w` still written on-chain. The split is
ownerless and immutable, so the builder's cut (default **10%**) is enforced: once
the money lands, nobody — including you — can redirect it.

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

No settler. No self-run facilitator. No smart contract you write or audit — the
split is an audited 0xSplits PushSplit. The only thing that isn't automatic is
**releasing** the money (`distribute`), and that's permissionless — anyone,
including the builder, can trigger it. [`monitor.py`](./monitor.py) tells you
which splits are holding funds ready to release.

---

## The money, precisely

- The buyer's payment settles **in full into the split** at request time.
- The split pays **10% to the builder's registered payout, 90% to you** — the cut
  is `X402_BUILDER_SHARE_BPS` (default `1000` = 10%).
- Payouts land ~2 base units light: a PushSplit keeps 1 unit warm and floors each
  share, so $1.00 pays `$0.099999 / $0.899999`. `split.amounts_units()` mirrors
  that exactly, so your ledger reconciles to the unit against the settle tx.
- It **stacks** with Base's own builder-rewards program on the volume driven.

The trust level, honestly: because `s` is a self-asserted tag, resolving it tells
you *where the money goes* (the code owner's registered payout), not that the
submitter is *entitled* to that code. For an affiliate program that's fine — the
registered owner gets paid regardless of who drove the traffic.

---

## Run it

```bash
pip install cbor2 requests pytest        # core deps
pip install cdp-sdk                       # optional: monitor.py's CDP SQL auth
python3 -m pytest -q                       # 68 tests (declare/decode + split math + payTo + Splits calldata)

cd fork-test && forge test                # 2 tests: CDP settle → split → distribute, on a Base mainnet fork
```

To stand up a live endpoint and take a real payment, see
[`test_endpoint/`](./test_endpoint) and [`RUNBOOK-live-test.md`](./RUNBOOK-live-test.md).

| File | What it is |
|------|------------|
| **`builder_code.py`** | The core. `declare_builder_code()` (declare `a`) + `parse_builder_code_suffix()` (decode `a`/`s`/`w` from a settle tx). No framework, no db. |
| **`resolver.py`** | **Code → payout wallet.** Resolves any builder code to its registered payout via the Base ERC-721 registry (raw `eth_call`, no keys). |
| **`split.py`** | **Split plan.** A builder code + price → the recipient set + bps a per-pair PushSplit encodes (10/90). Pure arithmetic. |
| **`push_split.py`** | **0xSplits v2 calldata + address.** `predict_split_address()` (the pair's counterfactual split, one `eth_call`) + the deploy/distribute calldata. No web3 dep. |
| **`payto.py`** | **The method.** `X-Builder-Code` header → the split address to advertise as `payTo` for the 402. Never raises — any failure falls back to your wallet (unsplit, never failed). |
| **`distribute.py`** | **Release a funded split.** Emits the (deploy + distribute) calldata that fans a split out to builder + seller. Permissionless. |
| **`monitor.py`** | **What's owed.** Discovers every builder who paid you (from CDP's index) and reports which splits are holding distributable funds. |
| **`buyer_client.py`** | Buyer side: the client extension a builder registers to attach their code and earn. |
| **`cdp_sql.py`** · **`queries.sql`** | Thin CDP SQL API client + copy-paste attribution queries (used by `monitor.py`; also runnable in the no-auth Playground). |
| **`test_endpoint/`** | A deployable FastAPI x402 endpoint on this exact path — plus `try.html`, a one-page browser client to pay it live. |
| **`fork-test/`** | Foundry test running CDP-settle → split → distribute against **live** Base USDC + PushSplitFactory on a mainnet fork. |

---

## Step 1 — Declare your app code (`a`)

Get a code at [base.dev](https://base.dev) → *Settings → Builder Codes*, set
`X402_BUILDER_CODE=bc_yourcode`, and merge the declaration into your route's
`extensions`:

```python
from builder_code import declare_builder_code

extensions = {}
extensions.update(declare_builder_code("bc_yourcode"))
# → {"builder-code": {"info": {"a": "bc_yourcode"}, "schema": {...}}}
```

The declaration rides inside the base64'd `402` header; the CDP facilitator
appends the on-chain `a`/`s`/`w` suffix at settlement. **Your server builds no
transaction.** A malformed code must never disable the paywall — wrap it and skip
attribution on error.

## Step 2 — Route `payTo` to the split

When the buyer's app names its builder (an `X-Builder-Code` header on the request),
resolve `payTo` per request to that pair's split:

```python
import payto

code = payto.builder_code_from_headers(request.headers)   # X-Builder-Code
pt = payto.payto_for_request(code, seller_payout=YOUR_WALLET)
route_config.pay_to = pt.address     # the split, or your wallet if no/unknown code
```

`payto_for_request` resolves the code, predicts the pair's split address (one
`eth_call`, cached), and **never raises** — a missing/unknown code or a failed
lookup falls back to your own wallet, so the payment always works; it just isn't
split. See [`test_endpoint/app.py`](./test_endpoint/app.py) for the full wiring as
a FastAPI `DynamicPayTo` callback.

> **Why a header?** `payTo` is fixed when the 402 goes out, but the standard `s`
> code only arrives *inside* the payment. So the buyer's app opts in by naming its
> builder at request time (one header, alongside the usual `s` extension). Buyers
> that don't send it pay you normally, unsplit. See the note in `payto.py`.

## Step 3 — Let CDP settle

Point your route at the CDP facilitator (mainnet). When the buyer pays, CDP
settles a plain USDC transfer into the split and writes `a`/`s`/`w` on-chain.
Nothing to do here — the split just fills. **Verify** any settle tx by pasting it
into [buildercode-checker.vercel.app](https://buildercode-checker.vercel.app/),
or decode it yourself:

```python
import builder_code, requests
inp = requests.post(RPC, json={"jsonrpc":"2.0","id":1,
        "method":"eth_getTransactionByHash","params":[settle_tx]}).json()["result"]["input"]
builder_code.parse_builder_code_suffix(inp)   # {'a': 'bc_yourcode', 's': ['bc_alice'], 'w': 'cdp_facil1'}
```

## Step 4 — Distribute (release the cut)

Funds accumulate in each pair's split until someone calls `distribute`. It's
permissionless — you, a keeper, or the builder themselves can trigger it:

```python
import payto, distribute

pt = payto.payto_for_request("bc_alice", seller_payout=YOUR_WALLET)
calls, balance = distribute.distribute_plan(pt.plan)   # deploy (first use) + distribute
# submit each (target, data) from a funded Base account — gas is cents
```

Use [`monitor.py`](./monitor.py) to see every split holding funds:

```
$ X402_BUILDER_CODE=bc_yourcode X402_SELLER_PAYOUT=0x… python3 monitor.py

builder code                balance  distributable  deployed  status
──────────────────────────────────────────────────────────────────
bc_alice                  $12.400000     $12.399999     True   ◀ DISTRIBUTE
bc_bob                     $0.000000      $0.000000     False   —
```

It discovers builders straight from CDP's attribution index — no local ledger —
and emits ready-to-run `cast` commands for the ones that need distributing.

---

## Buyer side — how a builder earns (for your docs)

A builder registers one client extension so every payment carries their code as
`s`, **and** sends `X-Builder-Code` on the request so `payTo` routes to their
split. See [`buyer_client.py`](./buyer_client.py) and, for a browser, the
`fetchWithPayment` + header pattern in [`test_endpoint/try.html`](./test_endpoint/try.html).

```python
client.register_extension(BuilderCodeClientExtension("bc_yourcode"))  # attaches s
# ...and send header X-Builder-Code: bc_yourcode on the request → payTo = the split
```

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

## Caveats (read before shipping)

- **Mainnet + CDP only.** Codes are only written on-chain on **Base mainnet via
  the Coinbase CDP facilitator**. On testnet / the free `x402.org` facilitator the
  declaration is harmless but nothing lands — and the registry/factory this kit
  reads only exist on mainnet, so `payto`/`resolver`/`push_split` have nothing to
  resolve against on testnet.
- **Use a paid RPC.** `resolver.py` defaults to `https://mainnet.base.org`, which
  `429`s after a few calls in a row. Set `X402_BASE_RPC` — a rate-limited resolve
  means a builder silently isn't split (it falls back to your wallet).
- **One split per builder, deployed once.** Each `(you, builder)` pair has a
  deterministic split address, deployed lazily on its first `distribute` and
  reused forever after. The deploy is a one-time ~cents gas cost per builder.
- **Distribute costs gas.** The buyer's payment is gasless (CDP-sponsored), but
  `distribute` is a plain tx — the caller pays a few cents of ETH. The caller
  earns nothing (`distributionIncentive = 0`) and can only send funds to the
  split's fixed recipients.
- **Hand-rolled by necessity.** The x402 *Python* SDK ships no builder-code
  module, so `builder_code.py` declares + decodes directly (on TypeScript use the
  official `@x402/extensions/builder-code`). Verify against a real settlement
  before trusting attribution.

## References

- [ERC-8021 builder-code extension spec](https://github.com/x402-foundation/x402/blob/main/specs/extensions/builder_code.md)
- [CDP Builder Codes](https://docs.cdp.coinbase.com/x402/core-concepts/builder-codes) · [CDP SQL API](https://docs.cdp.coinbase.com/data/sql-api/schema)
- [Base Builder Codes](https://docs.base.org/apps/builder-codes/builder-codes) · registry [`github.com/base/builder-codes`](https://github.com/base/builder-codes)
- [0xSplits PushSplit V2](https://splits.org/protocol/docs/core/split-v2) · factory `0x8E8eB0cC6AE34A38B67D5Cf91ACa38f60bc3Ecf4`

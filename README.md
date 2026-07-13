# x402 Builder-Code Affiliation Kit

A small, self-contained extraction of how [MiroShark-x402](https://github.com/aaronjmars/MiroShark-x402)
**declares a Base Builder Code** and **tracks affiliation** so the apps/agents
that drive payments to your x402 API get paid a share.

If you sell an API behind an x402 paywall, this is the whole machinery for:
"when someone's app pays me on behalf of their user, record *whose* app it was,
and pay that builder a cut."

> Distilled and simplified for teaching. `builder_code.py` is the real, faithful
> module (fully tested); the server / tracking / backfill files are trimmed,
> dependency-light versions of MiroShark's production code so the whole loop runs
> locally in one second.

---

## The mental model: three codes

[Base Builder Codes](https://docs.cdp.coinbase.com/x402/core-concepts/builder-codes)
are the **ERC-8021 Schema 2** on-chain attribution standard for x402. Every paid
request can carry up to three codes (each `^[a-z0-9_]{1,32}$`):

| Code | Who it names | Who sets it | When you can read it |
|------|--------------|-------------|----------------------|
| **`a`** app code | **you** (the API / resource server) | you declare it on the route | immediately |
| **`s`** service / referer | the **buyer's app/agent** that drove the payment | the buyer's client extension | at request time, off the payment |
| **`w`** wallet code | the **CDP facilitator** | the facilitator, at settlement | only **on-chain, after settle** |

`s` is the affiliation you care about — it tells you *which builder* to pay.

```
  buyer's app  ──pays──▶  YOUR /run  ──settles via──▶  CDP facilitator  ──▶  Base chain
   (carries s)             (declares a)                  (adds w)             tx calldata
                                │                                              carries a+s+w
                                └── capture a,s here ──┐                            │
                                                       ▼                            ▼
                                              record on the payment  ◀── daily backfill reads w
                                                       │
                                                       ▼
                                            pay each `s` builder their share
```

---

## The money

- The buyer's `$1.00` **settles in full, on-chain, to your `payTo`** at request
  time. Attribution changes nothing about that transfer.
- The **revenue share is paid off-chain**, out of band, to the payout wallet the
  builder registered for their code. MiroShark pays **50% of net profit**
  (`net = price − your actual cost`, floored at $0, completed runs only) to a
  buyer's `s` code. A first-party website referral (`?ref=`) gets 25%. Pick your
  own split — `compute_payouts(share=...)`.
- It **stacks** with Base's own builder-rewards program on the volume driven.

Because you track exact per-request cost, each builder's share is computed
precisely from `(recorded s code, price, cost)`.

---

## Run it

```bash
pip install cbor2 requests flask pytest   # cbor2 is the only hard dep of the core
pip install cdp-sdk                        # optional: only for the CDP SQL API path
python demo.py                            # the whole loop, no network, ~1s
pytest test_builder_code.py -q            # 32 tests on declare + on-chain decode
```

| File | What it is |
|------|------------|
| **`builder_code.py`** | The core. `declare_builder_code()` (declare `a`) + `parse_builder_code_suffix()` (decode `a`/`w`/`s` from calldata) + normalizers. No framework, no db. |
| **`server_example.py`** | Resource-server side: **(1)** declare `a` on the route, **(2)** capture `s`/`a` off each payment, **(3)** the WSGI observer that records the settle tx hash. |
| **`tracking.py`** | A tiny SQLite store — one row per payment — plus `compute_payouts()`. Stands in for whatever row you already write per paid request. |
| **`backfill.py`** | Daily cron, **option A**: read each settlement's calldata over a Base RPC and decode the ERC-8021 suffix. Preserves `a`/`w`/`s` roles. |
| **`backfill_sql.py`** | Daily cron, **option B**: recover `w` + reconcile `s` via the **CDP SQL API** — no RPC, no hand-decoding. Uses `cdp_sql.py`. |
| **`cdp_sql.py`** | Thin client for CDP's SQL API (`POST /v2/data/query/run`, Bearer JWT). |
| **`queries.sql`** | Copy-paste attribution queries you can run **right now** in the no-auth SQL Playground. |
| **`resolver.py`** | **Code → wallet.** Resolve any builder code to its **owner** and **payout address** via the Base ERC-721 registry (raw `eth_call`, no keys). |
| **`buyer_client.py`** | Buyer side: the one client extension a builder registers to attach their code and earn. |
| **`demo.py`** | End-to-end, in-memory, no network. |

---

## Step 1 — Declare your app code (`a`)

This is the entire "declare it" step. Get a code at
[base.dev](https://base.dev) → *Settings → Builder Codes*, set
`X402_BUILDER_CODE=bc_yourcode`, and merge the declaration into your route's
`extensions`:

```python
from builder_code import declare_builder_code

extensions = {}                                   # e.g. your Bazaar discovery ext
extensions.update(declare_builder_code("bc_yourcode"))
# → {"builder-code": {"info": {"a": "bc_yourcode"}, "schema": {...}}}

# pass `extensions` to the x402 middleware's RouteConfig for "POST /run"
```

That's it — the declaration rides inside the base64'd `402` header, and the CDP
facilitator appends the on-chain attribution suffix at settlement. **Your server
builds no transaction.** A malformed code must never disable the paywall, so
wrap it and skip attribution on error (see `server_example.create_app`).

## Step 2 — Track affiliation (`s`)

At request time, after the x402 middleware verifies the payment, read the codes
straight off the payload — no chain round-trip — and store them on the request's
row:

```python
from server_example import builder_codes_from_payment

code_a, code_s = builder_codes_from_payment()   # ("bc_yourcode", "bc_alice")
tracking.record_payment(conn, payment_id=pid, builder_code_a=code_a,
                        builder_code_s=code_s, price_usd=1.00, ...)
```

The submitted `s` is byte-for-byte what the facilitator writes on-chain, so
capturing it here is authoritative. `w` is left `NULL` — it doesn't exist yet.

## Step 3 — Recover `w` and verify on-chain

Two moving parts, because settlement happens *after* your view returns:

1. **Settle observer** (`SettleTxCaptureMiddleware`) — a WSGI wrapper *outside*
   the x402 middleware reads the `PAYMENT-RESPONSE` header off the outgoing
   response and records the settlement **tx hash** on the row. Purely
   observational; fully guarded so it can never break a payment.
2. **Daily backfill** — selects rows that have a tx hash but no `w` and reads the
   authoritative on-chain codes. **Two ways to do this** (next section).

**Verify** a real mainnet settlement by pasting the settle tx hash into
[buildercode-checker.vercel.app](https://buildercode-checker.vercel.app/) — you
should see your `a`, the facilitator's `w`, and the buyer's `s`.

## Reading the codes back on-chain: RPC vs CDP SQL API

There are two ways to recover what actually settled on-chain. Pick per your setup:

| | `backfill.py` — RPC + calldata | `backfill_sql.py` — CDP SQL API |
|---|---|---|
| **How** | `eth_getTransactionByHash` → decode ERC-8021 suffix (`parse_builder_code_suffix`) | `SELECT ... FROM base.transaction_attributions` |
| **Roles** | ✅ preserves `a` / `w` / `s` exactly | ❌ role-flat — returns the *set* of codes on a tx, not which is which |
| **Needs** | any Base RPC | a CDP API key (or nothing, in the [SQL Playground](https://portal.cdp.coinbase.com/onchain-tools/sql-api)) |
| **Decoding** | hand-rolled CBOR/ERC-8021 | none — Coinbase already decoded it |
| **Bonus** | — | one query rolls up *all* attribution; reconciles your captured `s` vs chain |

**Why the role-flat table is still enough:** you already captured your own `a` and
the buyer's `s` at request time, so you only need the chain to (1) **reconcile** —
confirm the `s` you recorded truly landed — and (2) **recover `w`** — it's the one
attributed code that isn't your `a` and isn't a recorded `s` (the CDP
facilitator's `cdp_facil`). `backfill_sql.py` does both, and flags any `s` that
was recorded but never settled — a check the RPC path doesn't give you.

**The CDP SQL API in one screen:**

```
POST https://api.cdp.coinbase.com/platform/v2/data/query/run
Authorization: Bearer <CDP API-key JWT>      # ~2 min expiry; or use the Playground (no key)
Content-Type: application/json

{ "sql": "SELECT transaction_hash, builder_code FROM base.transaction_attributions
           WHERE transaction_hash IN ('0x…') GROUP BY transaction_hash, builder_code
           HAVING sum(action) > 0",
  "cache": { "maxAgeMs": 5000 } }

→ { "result": [ { "transaction_hash": "0x…", "builder_code": "bc_alice" }, … ],
    "schema": { "columns": [ … ] } }
```

Two attribution tables (both role-flat, keyed by `transaction_hash`):
`base.transaction_attributions` (`builder_code`, one per row — the normal x402
EIP-3009 settlement) and `base.decoded_user_operations` (`builder_codes` array —
ERC-4337 smart-account settlements). ClickHouse dialect, read-only SELECT, and
every row carries `action` (+1 added / −1 removed) so `HAVING sum(action) > 0`
filters re-orgs. See `queries.sql` for copy-paste queries. Full docs:
[docs.cdp.coinbase.com/data/sql-api](https://docs.cdp.coinbase.com/data/sql-api/schema).

## Step 4 — Pay the builders (and where to send it)

`compute_payouts` tells you *how much* each code is owed. To find *where* to send
it, resolve the code against the Base registry — see the next section:

```python
import resolver

for p in tracking.compute_payouts(conn, share=0.50):
    who = resolver.resolve(p.builder_code)          # onchain lookup, no keys
    print(p.builder_code, f"owed ${p.owed_usd:.2f}",
          "→", who["payout_address"] if who["registered"] else "(unregistered)")
```

## Which wallet owns a builder code?

Base Builder Codes are an **ERC-721 NFT collection** ([`github.com/base/builder-codes`](https://github.com/base/builder-codes)):
registering a code mints a token whose ID is derived from the code string, and
its onchain metadata declares a **payout address**. So mapping a code → wallet is
a plain onchain read — no API key, no indexer, and it works for *any* code anyone
has ever registered.

- **Registry (Base mainnet):** [`0x000000BC7E6457e610fe52Dcc0ca5b3ce59C8E80`](https://basescan.org/address/0x000000BC7E6457e610fe52Dcc0ca5b3ce59C8E80)
  — a verified ERC1967 proxy (`0x000000BC…`, a vanity address), ERC-721 "Builder
  Codes", implementing the ERC-8021 `ICodesRegistry` interface.
- **Two addresses come back — they're different:** `owner` holds the code NFT
  (can transfer it / change its payout); **`payout_address` is where rewards
  go — pay that one.** ⚠️ For the free auto-generated `bc_*` codes, base.dev holds
  the NFT *custodially*, so `owner` is the **base.dev registrar** (one wallet
  holds ~46k codes), not the builder — only self-custodied / vanity codes have
  `owner` == the builder. Always pay `payout_address`, never `owner`.
- **Token ID = the code's ASCII bytes as a big-endian integer** (verified against
  the contract). `resolver.to_token_id("leap_wallet")` matches on-chain exactly.
  Note these IDs exceed JavaScript's safe-integer range — always handle them as
  big integers / strings, never JS numbers.

```python
import resolver
resolver.resolve("leap_wallet")
# {'code': 'leap_wallet', 'token_id': 131042744964646850211374452,
#  'registered': True,
#  'owner':          '0xf9d7dc9e4b9d04c878312edbcc255cfdde798116',
#  'payout_address': '0xa06c433da67182d43c80585d52d3990edcb454c8'}
```

`resolver.py` speaks raw JSON-RPC `eth_call` (only needs `requests`) against any
Base RPC — run `python resolver.py` for a live demo. Under the hood it calls the
registry's `ownerOf(uint256)` and `payoutAddress(uint256)`. The base.dev API does
the *reverse* direction (**wallet → code**, deterministic, no auth) —
`POST https://api.base.dev/v1/agents/builder-codes` with `{"walletAddress": "0x…"}`
— handy for verifying a code belongs to a wallet you already know.

**Full loop:** attribution gives you the `s` code that drove a payment → the
registry resolves that code to a payout wallet → you send the revenue share
there. Onchain from end to end.

---

## Buyer side — how a builder earns (for your docs)

A builder registers one client extension on the x402 client they pay with; every
payment then carries their code as `s`. See `buyer_client.py` for the Python and
TypeScript snippets.

```python
client.register_extension(BuilderCodeClientExtension("bc_yourcode"))
# every client.fetch(...) payment now carries your code
```

---

## Caveats (read before shipping)

- **Mainnet + CDP only.** Codes are only written on-chain on **Base mainnet via
  the Coinbase CDP facilitator**. On testnet or the free `x402.org` facilitator
  the declaration is harmless but nothing lands — so `w` never backfills there.
- **Hand-rolled by necessity — or skip it.** The x402 *Python* SDK ships no
  builder-code module, so `builder_code.py` declares + decodes directly (on
  TypeScript use the official `@x402/extensions/builder-code`). If you'd rather
  not trust a hand-rolled calldata decoder, use the **CDP SQL API path**
  (`backfill_sql.py`) — Coinbase decodes attribution for you. Either way, verify
  against a real settlement before trusting attribution.
- **Off-chain payout is your responsibility.** This kit records who is owed what;
  actually sending the USDC share is a separate job you run against
  `compute_payouts()`.

## Provenance

Extracted from MiroShark-x402:
[`app/utils/builder_code.py`](https://github.com/aaronjmars/MiroShark-x402/blob/main/app/utils/builder_code.py),
[`app/api/x402_run.py`](https://github.com/aaronjmars/MiroShark-x402/blob/main/app/api/x402_run.py),
[`app/utils/settle_capture.py`](https://github.com/aaronjmars/MiroShark-x402/blob/main/app/utils/settle_capture.py),
[`scripts/backfill_builder_codes.py`](https://github.com/aaronjmars/MiroShark-x402/blob/main/scripts/backfill_builder_codes.py).
Spec: [ERC-8021 builder_code.md](https://github.com/x402-foundation/x402/blob/main/specs/extensions/builder_code.md).

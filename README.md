# x402 Builder-Code Affiliation Kit

A small, self-contained, dependency-light kit for **declaring a Base Builder
Code** on an x402 API and **tracking affiliation** so the apps/agents that drive
payments to your API get paid a share.

If you sell an API behind an x402 paywall, this is the whole machinery for:
"when someone's app pays me on behalf of their user, record *whose* app it was,
and pay that builder a cut."

> Framework-agnostic core (`builder_code.py`, `resolver.py`) plus minimal,
> runnable reference implementations for the server, tracking, and backfill —
> the whole loop runs locally in about a second. Wire the same pieces into your
> own stack.

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

- The buyer's payment **settles in full, on-chain, to your `payTo`** at request
  time. Attribution changes nothing about that transfer.
- The **revenue share is paid off-chain**, out of band, to the payout wallet the
  builder registered for their code. A common split is **a share of net profit**
  (`net = price − your actual cost`, floored at $0, completed requests only) to a
  buyer's `s` code — pick your own with `compute_payouts(share=...)` (defaults to
  0.50).
- It **stacks** with Base's own builder-rewards program on the volume driven.

Because you track exact per-request cost, each builder's share is computed
precisely from `(recorded s code, price, cost)`.

**Or enforce it on-chain.** Instead of paying out later, a **settler** can carve a
flat cut (e.g. 10% of price) *at settlement* — **automatic, atomic, auditable** —
by splitting each payment through an audited 0xSplits PushSplit. On this path the
route's `payTo` is the **settler account** (the only address that can redeem the
buyer's signature), and it forwards into the split in the same tx. See `split.py`,
`settler.py`, `fork-test/`, and `INTEGRATION.md` (incl. the honest trust level:
you run the settler, so it's enforced-by-default but not cryptographically
trustless).

Payouts land ~2 base units light — a PushSplit keeps 1 unit and floors each
share, so $1.00 pays $0.099999 / $0.899999. `split.amounts_units()` mirrors that
exactly, so your ledger reconciles to the unit against the settle tx.

---

## Run it

```bash
pip install cbor2 requests flask pytest   # cbor2 is the only hard dep of the core
pip install cdp-sdk                        # optional: only for the CDP SQL API path
python demo.py                            # the whole loop (off-chain + on-chain split), no network, ~1s
python settler.py                         # print the atomic settle multicall for one payment
pytest -q                                 # 57 tests (declare/decode + split math + Splits calldata)

cd fork-test && forge test                # 11 tests against a real Base mainnet fork (needs foundry)
```

> **Wiring the settler:** set your route's `payTo` to `X402_SETTLER_ACCOUNT`, not
> to your payout wallet. The buyer's signature names its recipient, so that's the
> only address funds can enter through — the settler then forwards into the split
> in the same tx. See `fork-test/` and `INTEGRATION.md`.

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
| **`split.py`** | **Enforced payout core.** Turn a captured `s` code + price into an on-chain split plan (90/10 recipients + bps), resolving the builder payout via `resolver`. The on-chain counterpart to `compute_payouts`. |
| **`settler.py`** | Reference **settler**: read `s` at settlement → resolve → one atomic tx (pull the buyer's USDC → fund the per-pair PushSplit → deploy it if new → distribute). Emits ready-to-submit calldata against the confirmed Base PushSplitFactory; config-driven, so any x402 seller can reuse it. See `INTEGRATION.md`. |
| **`buyer_client.py`** | Buyer side: the one client extension a builder registers to attach their code and earn. |
| **`demo.py`** | End-to-end, in-memory, no network. |
| **`fork-test/`** | Foundry tests running the settle legs against **live** Base USDC + PushSplitFactory on a mainnet fork. Pins why `payTo` must be the settler, the exact payout math, and the 7702 delegation (incl. a failing leg rolling back the pull). |

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
- **The settler builds calldata; it doesn't sign or broadcast.** There's no key
  handling anywhere in this repo. `settler.py` emits the target/calldata pairs;
  `fork-test/Delegation7702.t.sol` runs them through a real signed 7702
  delegation. What's left is *broadcasting* — a funded settler account, nonce
  management, gas, liveness. Do a run of your own wiring before any mainnet money
  moves.
- **Don't ship on the public RPC.** `resolver.py` defaults to
  `https://mainnet.base.org`, which starts returning `429` after a few calls in a
  row. Set `X402_BASE_RPC` to a paid endpoint — a rate-limited resolve means a
  builder silently doesn't get paid.

## References

- [ERC-8021 builder-code extension spec](https://github.com/x402-foundation/x402/blob/main/specs/extensions/builder_code.md)
- [CDP Builder Codes](https://docs.cdp.coinbase.com/x402/core-concepts/builder-codes) · [CDP SQL API](https://docs.cdp.coinbase.com/data/sql-api/schema)
- [Base Builder Codes](https://docs.base.org/apps/builder-codes/builder-codes) · registry [`github.com/base/builder-codes`](https://github.com/base/builder-codes)

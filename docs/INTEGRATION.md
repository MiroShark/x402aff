# Enforced Affiliation - Integration Guide

How to wire the x402aff kit into your own x402 stack so the affiliate cut is **split
on-chain, at settlement**, with **no facilitator of your own to run**. This is the
path validated on Base mainnet and reproducible on a mainnet fork (see
[`../fork-test/`](../fork-test)).

---

## 0. TL;DR

- Declare your app code `a` on the route (`declare_builder_code`).
- The buyer's app names its builder at request time (an `X-Builder-Code` header)
  and attaches the standard `s` extension.
- Your server sets the route's **`payTo` to the per-`(you, builder)` 0xSplits
  PushSplit** for that request (`payto.payto_for_request`).
- The **stock CDP facilitator** settles a plain USDC transfer into the split -
  sponsored gas, `a`/`s`/`w` written on-chain. Nothing of yours signs or settles.
- Funds sit in the ownerless, immutable split until anyone calls `distribute`
  (`distribute.py`); `monitor.py` shows what's ready.

No settler, no relayer, no smart contract you write. The only audited contract in
the path is 0xSplits' PushSplit.

---

## 1. How x402 actually works (the money path)

```
buyer client                    resource server                 CDP facilitator      Base chain
     │  1. POST /run (no pay) ──────▶ │                              │                    │
     │     + X-Builder-Code: bc_alice │                              │                    │
     │  ◀── 2. 402 + accepts[] ────── │  payTo = the (you, bc_alice) │                    │
     │      (declares your `a`)       │         split address        │                    │
     │  3. sign EIP-3009 auth ───────▶│                              │                    │
     │     (to = the split)           │  4. POST /verify ──────────▶ │                    │
     │     carries `s` in extensions  │  5. do the work              │                    │
     │                                │  6. POST /settle ──────────▶ │  transfer into ───▶│ split holds
     │  ◀── 7. 200 + result ───────── │  ◀── PAYMENT-RESPONSE ────── │  the split         │ funds; a/s/w
     │                                │      {txHash}                │  (pays gas)        │ in calldata
```

Three facts decide everything:

1. **The buyer signs a gasless EIP-3009 authorization** committing to
   `(from, to = payTo, value, …)`. Whatever `payTo` the 402 advertised is where
   the money goes.
2. **The facilitator settles by calling the USDC token** - a plain transfer to
   `payTo`. It never calls an arbitrary contract. That's fine here: `payTo` is a
   PushSplit *address*, and USDC lands in it like any other account.
3. **`s` is passive metadata** written into the settle calldata. It moves no money
   - it's the tag that says "app `bc_alice` drove this."

## 2. The key move: `payTo` = the split

`payTo` is fixed when the 402 goes out, but `s` only arrives *inside* the payment.
So the split can't be chosen from `s` at 402 time. Instead, the buyer's app names
its builder at **request time** with an `X-Builder-Code` header, and your server
sets `payTo` to that pair's split:

```python
import payto

code = payto.builder_code_from_headers(request.headers)          # X-Builder-Code
pt = payto.payto_for_request(code, seller_payout=YOUR_WALLET)     # never raises
route_config.pay_to = pt.address
if pt.error:
    log.warning("payTo resolve failed, unsplit: %s", pt.error)
```

- **Registered code** → `payTo` = the counterfactual PushSplit for `(you, code)`.
  The address is deterministic (CREATE2), so it's the same every time and can be
  pre-funded before it's deployed.
- **No / unknown code, or a lookup failure** → `payTo` = your own wallet. The
  payment still works; it just isn't split. A bad resolve never breaks the paywall.

Why is that split *enforced*? It's created **ownerless (`owner = 0`) and immutable**
- recipients and the 10/90 ratio are fixed forever at its address. Once USDC lands
there, nobody (including you) can redirect the builder's cut.

The [`Affiliation`](../python/affiliation.py) facade wraps this: `aff.pay_to` is a
drop-in x402 `DynamicPayTo` callback, and `aff.pay_to_for(headers)` is the sync
form for other frameworks. The buyer attaches its code with the official
`@x402/extensions/builder-code` client extension.

## 3. Components

All kit modules live in [`python/`](../python) (import them by bare name from
there). The one-object [`affiliation.py`](../python/affiliation.py) facade wraps
the modules below.

| File (`python/…`) | Role in the path |
|---|---|
| `builder_code.py` | `declare_builder_code(a)` on the route; `parse_builder_code_suffix()` to decode `a`/`s`/`w` off a settle tx. |
| `resolver.py` | Builder code → registered payout address (one `eth_call`, no keys). |
| `split.py` | Builder code + price → the split plan (recipients + 10/90 bps). Pure. |
| `push_split.py` | The plan → 0xSplits calldata; `predict_split_address()` for the counterfactual address. |
| `payto.py` | The request-time `payTo` resolver. Cached, never raises. |
| `distribute.py` | The (deploy + distribute) calldata to release a funded split. |
| `monitor.py` | Which splits are holding distributable funds (via CDP's index). |
| `buyer_client.py` | The buyer-side extension that attaches `s`. |

## 4. Wiring into an x402 server

1. **Declare `a`.** Merge `declare_builder_code("bc_yourcode")` into the route's
   `extensions`. Identity still matters even when a payment isn't split.
2. **Dynamic `payTo`.** Set the route's `payTo` from
   `payto.payto_for_request(code)` per request (a callable/`DynamicPayTo` if your
   SDK supports one, else build the 402 requirements yourself). Keep it
   deterministic so the 402 and the buyer's signed retry agree on `payTo`.
3. **Point at the CDP facilitator** (mainnet). No custom facilitator, no keys.
4. **Warm the cache** (optional) for expected builders at startup, so the first
   402 for each needs no live RPC call - the public RPC rate-limits.

The [`Affiliation`](../python/affiliation.py) facade does 1-2 for you: pass
`extensions=aff.extensions` and `pay_to=aff.pay_to` to your route.

## 5. Distribution & monitoring

Payments accumulate per pair. Release them with `distribute.py` (permissionless -
you, a keeper, or the builder can call it), and track what's owed with
`monitor.py`. Neither needs the buyer's signature or any settle machinery; both
just read the chain / CDP's index and emit calldata.

To automate payouts, run `monitor.py` on a schedule and submit the `cast` commands
it prints from a funded gas key - new code → collect → auto-distribute, hands-off.

### Discovering kit payments across sellers (the marker)

`monitor.py` finds splits for *your* app code. To find every kit-routed payment
**ecosystem-wide** - across sellers you don't know - the buyer extension stamps a
fixed, **shared** marker code (`builder_code.AFFILIATION_MARKER` = the hardcoded
`x402aff`) as a second `s` on every payment. Because every kit install stamps the
same marker, one query finds them all. It rides alongside the real builder code
and never changes a payout (the split always pays the primary code), but it makes
kit payments self-identifying on-chain. Discovery is then a single cheap query
against CDP's index - no `payTo` reconstruction, and it catches even *undeployed*
splits (the marker is written at settle time, not at deploy):

```sql
SELECT DISTINCT transaction_hash
FROM base.transaction_attributions
WHERE builder_code = 'x402aff' AND action = 1;
```

Join those tx hashes to the USDC `Transfer` in `base.events` (bounded by their
`block_number`, so the scan stays tiny) to recover each payment's `payTo` (the
split), then read its balance as usual - see `queries.sql` #5. The marker needs no
registration - it is stamped into the settlement suffix and indexed even as an
unregistered code, and as a shared label it is not exclusive. Because `s` is
buyer-attached, the marker tags
payments made through the kit's client extension - exactly the opted-in population.

## 6. Trust model - be clear-eyed

Because `s` is a self-asserted tag (not part of the buyer's signature), resolving
it tells you *where the money goes* (the code owner's registered payout), not that
the submitter is entitled to that code. For an affiliate program that's the right
level: the registered owner is paid regardless of who drove the traffic, and the
**split ratio is enforced by an immutable contract** - that's the guarantee that
matters. What it is *not* is a trustless proof of who drove a payment.

The split is enforced; the *routing to it* is a client opt-in (the header). A
generic x402 client that only sets `s` and never sends the header pays you
directly, unsplit - which is safe (you keep 100%) but unattributed on-chain in
money terms. For an affiliate program where builders opt in to earn, that's
exactly the right shape.

## 7. Edge cases & caveats

- **No / unregistered / blank code** → `payTo` = your wallet, 100% to you, never
  stranded.
- **Multiple `s` codes** (layered clients, comma-joined) → the split pays the
  **primary** (first valid) code. Single-`s` policy for v1.
- **Mainnet + CDP only.** The registry, the Splits factory, and on-chain
  attribution all live on Base mainnet via the CDP facilitator. Testnet / the free
  `x402.org` facilitator write nothing and have nothing to resolve against.
- **Paid RPC.** `X402_BASE_RPC` should point at a paid endpoint; the public one
  `429`s and silently drops attribution to the seller fallback.
- **One deploy per builder.** Each pair's split deploys once (lazily, on first
  distribute) and is reused. Small one-time gas each.
- **Distribute gas.** The payment is gasless; `distribute` is a plain tx the
  caller pays for (cents). Incentive is 0, so the caller earns nothing and can
  only pay the split's fixed recipients.

## 8. Verification / testing

1. **Unit** - `cd python && pytest` (declare/decode, split math, payTo fallback +
   cache, the `Affiliation` facade, Splits calldata byte-for-byte). The TypeScript
   port has its own suite: `cd ts && npm test`.
2. **Fork** - `cd fork-test && forge test` (CDP-settle → split → distribute against
   live Base USDC + PushSplitFactory; asserts the ownerless split can't be
   redirected).
3. **Live** - point your own x402 endpoint's `payTo` at the split (as in §2), pay
   it once with any x402 client, then paste the settle tx into
   `buildercode-checker.vercel.app` (or decode it with
   `builder_code.parse_builder_code_suffix`) and confirm the split balance rose
   (`distribute.split_balance_units`).

## References

- ERC-8021 builder-code extension spec -
  https://github.com/x402-foundation/x402/blob/main/specs/extensions/builder_code.md
- Base Builder Codes registry - https://github.com/base/builder-codes
  (`0x000000BC7E6457e610fe52Dcc0ca5b3ce59C8E80`)
- EIP-3009 `transferWithAuthorization` - Circle FiatToken
  (Base USDC `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`)
- 0xSplits PushSplit V2 - https://splits.org/protocol/docs/core/split-v2
  (factory `0x8E8eB0cC6AE34A38B67D5Cf91ACa38f60bc3Ecf4`)
- CDP Builder Codes - https://docs.cdp.coinbase.com/x402/core-concepts/builder-codes

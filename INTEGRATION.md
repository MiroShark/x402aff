# Enforceable Affiliation — Integration Guide

> **What this doc is.** The kit in this repo *records* who drove each x402 payment
> and computes what they're owed, but the payout itself is off-chain and
> trust-based: you have to voluntarily send the USDC share. This guide is the
> design for closing that gap — making the affiliate cut **split on-chain,
> atomically, at settlement**, so "honest payout" stops being a trust assumption.
>
> It's a design + integration spec, not shipped code. Wire it into your own stack;
> once it works, it becomes the boilerplate everyone else copies.

---

## v1 — where this landed (now in the code)

The sections below are the full design rationale (and explore a self-built
splitter contract). After talking it through with Base + 0xSplits, **the shipped
v1 is simpler** and lives in `split.py` + `settler.py`:

- **Flat cut, not a declared rate.** A flat **10% of price** ($0.10 of a $1
  payment) to the referer builder code. It's known the instant the payment
  settles, so no cost/net-profit oracle is needed. (`DEFAULT_BUILDER_SHARE_BPS`.)
- **Audited Splits, not our own contract.** The money is split by a
  per-`(seller, builder)` **0xSplits PushSplit** (push variant → pays recipients
  on `distribute()`, no withdraw step). You write and audit no split math.
- **A settler reads `s` at settlement.** The builder is named by the
  **payment-time** `s` code (it rides *inside* the payment, not the request), so
  it isn't known when your 402 sets `payTo`. A settler therefore reads `s` at
  settle and does **one atomic tx** — r0ohafza's (Splits) recipe: a 7702 account
  that *deploys the per-pair PushSplit → funds it → distributes*.
- **Per-pair CREATE2** (Abram/Splits): each `(seller, builder)` split has a
  deterministic address, deployed counterfactually only once it's funded.

**Trust level — be clear-eyed.** Because `s` is *not* in the buyer's signature,
the money must flow through a settler **you** run. So the split is **automatic +
atomic + auditable**, but **not cryptographically trustless**: the contract locks
the 90/10 ratio, yet nothing on-chain forces you to run the settler or to pass the
real `s`. That's the ceiling for builder codes (a payment-time attribution tag),
and it's plenty for an affiliate program — it upgrades "trust me to pay you later,
manually" to "it pays automatically in the settle tx, verifiable on-chain."

**Complexity / downsides.** You run a small settler (funded Base hot wallet +
nonce mgmt + liveness). No CDP gas sponsorship on this rail — but gas is cents,
coverable by **Circle Paymaster** (USDC on Base). Per-pair splits deploy lazily
(small one-time gas each). Covers the **EIP-3009 rail** (your real traffic);
7710/other rails would each need their own wiring.

**Reusable by any x402 seller.** `settler.py` is **config-driven**
(`X402_SELLER_PAYOUT`, `X402_BUILDER_SHARE_BPS`, factory address) — another
service clones it, sets their payout + share, and points their route's settlement
at their own instance. Trust stays local: each seller runs their own settler, so
the builders *they* pay trust *them*, not you.

**Confirmed by 0xSplits (2026-07-15) — the seam is now wired:**
1. ✅ Canonical **PushSplitFactory V2.2** on Base:
   [`0x8E8eB0cC6AE34A38B67D5Cf91ACa38f60bc3Ecf4`](https://basescan.org/address/0x8E8eB0cC6AE34A38B67D5Cf91ACa38f60bc3Ecf4#code)
   (verified contract; canonical list:
   [splits.org docs](https://splits.org/protocol/docs/core/split-v2#addresses)).
   The counterfactual address is computed server-side with ONE `eth_call` to
   `factory.isDeployed(split, owner=0, salt=0)` — `settler.predict_split_address`
   does it with no web3 dependency, verified byte-for-byte against the live
   factory.
2. ✅ Pre-funding an *undeployed* counterfactual split is a **supported/tested
   pattern** — `distribute` reads the balance at call time; arrival order doesn't
   matter.

`settler.py` now emits ready-to-submit calldata for the deploy
(`createSplitDeterministic`) and `distribute` legs (`owner=0` → immutable split,
`salt=0` → one canonical address per pair). What's left to you: the pull leg's
calldata (built at runtime from the buyer's signed EIP-3009 authorization) and
signing/submitting the multicall with your 7702 settler account.

**Confirmed by Base (2026-07-16) — nothing native is coming, the settler stands:**
1. ✅ Builder codes stay **attribution-only**; any revshare is on the seller. A
   revshare demo ([x402-revshare-demo](https://github.com/MattWong-ca/x402-revshare-demo),
   exploring [base/flywheel](https://github.com/base/flywheel)) is gauging demand
   but "most likely won't be pursued in the near future". (Flywheel is campaign
   escrow — sponsor pre-funds, a *manager* submits payouts — i.e. an on-chain
   version of this kit's off-chain ledger path, not atomic-at-settlement.)
2. ✅ CDP's income-splitting stance: **no scheme change** — "solve it via
   `payTo` pointing at a splitting contract". That works only for
   **request-time** identity (the escape hatch below); it structurally *cannot*
   express builder codes, because `payTo` is fixed at 402 time and `s` arrives
   inside the payment. So the settler is the only mechanism for `s`-driven
   splits, and no spec/SDK change will obsolete it.
3. ⚠️ A code's **payout address is not user-configurable today** — it's pinned
   to the project owner's Base account. `resolver.py` pays `payout_address`,
   which is correct now and stays correct if Base later unlocks it. (It also
   means a builder can't currently re-point their payout at their own splitter —
   see the note in §6.)
4. ✅ `toTokenId`/`toCode` (ASCII bytes, big-endian) confirmed — matches what
   `resolver.py` already verified against the on-chain contract.

**Confirmed by MetaMask (2026-07-16) — no delegation rail needed:**
1. ✅ When a client supports EIP-3009, Permit2 *and* ERC-7710, the x402 SDK
   **always defaults to 3009**. MetaMask users therefore come down the exact
   rail the settler already covers — there is no second rail to wire.
2. ✅ Splitting inside a delegation redemption would need your own facilitator,
   custom execution and updated permissions, and MetaMask themselves flag it as
   likely **breaking the x402 spec** — same trust level as the settler, more
   moving parts. Dropped.
3. ✅ The 7702 authorization can't be batched inside the delegation for later
   settlement; gas on a self-run facilitator is sponsorable when the redeemer is
   a smart account — consistent with the Circle Paymaster note above.

> The one way to keep CDP's sponsored settle *and* drop the settler is to route
> the cut on a **request-time** id (one your server sees at 402 time) rather than
> the payment-time `s` — then `payTo` can *be* the split. That's a different
> mechanism than builder codes; noted here only as the escape hatch — and it's
> exactly the pattern CDP's "solve it via `payTo`" stance describes.

---

## 0. TL;DR

- Today: the chain records **who** drove a payment (`s` code); the money is a
  single transfer to the seller; the affiliate's cut is paid **later, off-chain,
  on trust**.
- The fix: settle each payment **into a splitter contract** that carves the
  affiliate's cut out atomically and forwards the rest to the seller — in one
  transaction, with no party able to alter the amounts.
- Enforcement needs **two ingredients**: *who* to pay (already solvable —
  `resolver.py`) and *how much* (doesn't exist yet — you must introduce it).
- The split rate is **seller-declared and stored on-chain** (Model B). The
  contract reads it; nobody can lie about it.
- This is **not** compatible with the stock CDP/x402.org facilitators — but the
  piece you add is a **trustless, permissionless relayer**, not a trusted
  facilitator. It holds no funds and decides no amounts; the contract does.
- You **build one ~40-line contract**. Everything on either side of it — USDC's
  gasless pull, the Base builder-codes registry, the resolver — is reused.

---

## 1. How x402 actually works (the money path)

x402 is HTTP-native payment built on `402 Payment Required`. One paid request:

```
buyer client                    resource server                 facilitator          Base chain
     │  1. POST /run (no pay) ──────▶ │                              │                    │
     │  ◀── 2. 402 + accepts[] ────── │  (price, payTo, network,     │                    │
     │      (declares your `a`)       │   asset=USDC, extensions)    │                    │
     │  3. sign EIP-3009 auth ───────▶│                              │                    │
     │     (from, to=payTo, value)    │  4. POST /verify ──────────▶ │  sig? funds? match?│
     │     carries `s` in extensions  │  ◀────────── ok ──────────── │                    │
     │                                │  5. do the work              │                    │
     │                                │  6. POST /settle ──────────▶ │  broadcast tx ────▶│ transfer +
     │  ◀── 7. 200 + result + ─────── │  ◀── PAYMENT-RESPONSE ────── │  (pays gas)        │ ERC-8021 suffix
     │      X-PAYMENT-RESPONSE        │      {txHash}                │                    │ (a+s+w land)
```

Three facts that decide everything downstream:

1. **The buyer signs a gasless authorization, not a transaction.** For the EVM
   `exact` scheme it's an **EIP-3009** authorization committing to
   `(from, to, value, validAfter, validBefore, nonce)` — one transfer, one
   amount, one recipient.
2. **The facilitator is the settler.** It's just two endpoints — `/verify` and
   `/settle` — plus a funded wallet + RPC. `/settle` broadcasts the tx, pays gas,
   and (for CDP) appends the ERC-8021 `a/s/w` suffix. The **resource server builds
   no transaction** — which is why `server_example.SettleTxCaptureMiddleware` has
   to scrape the tx hash out of a *response header after the view returns*.
3. **The `s` code is passive metadata.** It rides in the payload extensions
   (`buyer_client.BuilderCodeClientExtension`), gets copied into calldata at
   settle, and **moves zero money**. It's a sticker that says "app `bc_alice`
   drove this."

---

## 2. The gap: attribution ≠ enforcement

Put those together and the trust hole is obvious:

- The buyer's signature authorizes **`value → payTo`, single recipient.** Full stop.
- Attribution is a tag on the side of that transfer.
- The affiliate's cut is a **separate, later, voluntary** USDC transfer you make
  off `tracking.compute_payouts()`. Nothing on-chain compels it.

And the facilitator can't just redirect part of the funds — the EIP-3009
signature *commits* to `to` and `value`, so a vanilla settle can only pay the
exact address the buyer signed. **That constraint is what every enforcement
design has to route around.**

You need two ingredients to enforce a split, and you only have one:

| Ingredient | Status today |
|---|---|
| **WHO** to pay (`s` → payout address) | ✅ `resolver.resolve()` — an on-chain read, fast, cacheable |
| **HOW MUCH** (the split rate) | ❌ Doesn't exist anywhere. `compute_payouts(share=…)` is a private, off-chain number the seller picks after the fact |

The ERC-8021 schema carries **identity only** — `a`, `w`, `s`, and (see
`builder_code.BUILDER_CODE_SCHEMA`) `additionalProperties: False`. **There is no
`f` fee field**, unlike Hyperliquid's `{"b": addr, "f": 10}`. The code says *who*,
never *how much*.

---

## 3. The design: on-chain, seller-declared splits (Model B)

### Where the split rate lives — and why on-chain

A smart contract **cannot read an HTTP endpoint.** So a share advertised only in
the 402 has to be carried on-chain by *someone*, and whoever carries it becomes
the trust anchor. Three ways to place the declaration:

| Placement | Who sets it | Whose pocket | Trust anchor |
|---|---|---|---|
| `f` in the buyer's `s` extension | the app | the **buyer** (surcharge) | — (Hyperliquid model) |
| Advertised in the seller's **402** | the seller | the seller's margin | **the facilitator** (it reads + must be trusted on the number) |
| **On-chain registry / splitter** | the seller | the seller's margin | **the chain** — nothing to trust |

We pick the third: **seller-declared, stored on-chain.** The seller registers
their share once; the splitter reads it from chain state at settle. There is
nothing to validate — the split the contract enforces *is* provably the number
the seller registered, and anyone can read it. This is Model B, and it's simpler
where it counts: the facilitator holds **zero economic discretion**.

> The 402 may *still* advertise the share for buyer-facing transparency, but in
> Model B that advertisement is **informational** — the on-chain registry is
> authoritative.

### The shape

```
route.payTo = SPLITTER address           // advertised in the 402; buyer signs to it

buyer ──EIP-3009 auth──▶ BuilderSplit.settle(aCode, sCode, sig…)
                             ├─ pulls buyer's USDC in (gasless)
                             ├─ reads seller's registered share  (on-chain)
                             ├─ resolves `s` → builder payout      (on-chain, the registry resolver.py reads)
                             ├─ share  ─────▶ builder payout
                             └─ value − share ─▶ seller payout      (never blocked)
```

Everything — the share, the builder address, the math — is decided **inside the
contract from on-chain inputs**. The only thing off-chain is *who submits the tx*
(the relayer), and it can't change any amount.

---

## 4. Components

### 4.1 `BuilderSplit.sol` — the router (the one thing you build)

~40 lines. Holds no custody (atomic — nothing sits in it between calls). Standard
pattern: one `receiveWithAuthorization` pull + two `transfer`s.

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IERC20 {
    function transfer(address to, uint256 value) external returns (bool);
    // USDC (Circle FiatToken) — EIP-3009 gasless pull; requires msg.sender == to
    function receiveWithAuthorization(
        address from, address to, uint256 value,
        uint256 validAfter, uint256 validBefore, bytes32 nonce,
        uint8 v, bytes32 r, bytes32 s
    ) external;
}

interface ICodesRegistry {                     // Base Builder Codes (ERC-8021)
    function ownerOf(uint256 tokenId) external view returns (address);
    function payoutAddress(uint256 tokenId) external view returns (address);
}

contract BuilderSplit {
    IERC20        constant USDC     = IERC20(0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913);        // Base native USDC
    ICodesRegistry constant REGISTRY = ICodesRegistry(0x000000BC7E6457e610fe52Dcc0ca5b3ce59C8E80); // Base registry

    struct Seller { address payout; uint16 shareBps; bool set; }
    mapping(bytes32 => Seller) public sellers;  // keyed by keccak(seller's `a` code)

    event Registered(string aCode, address payout, uint16 shareBps);
    event Settled(string aCode, string sCode, address from,
                  address sellerPayout, address builder,
                  uint256 value, uint256 cut, bytes32 nonce);

    // ── seller declares, once, self-service, gated by code ownership ──
    function register(string calldata aCode, address payout, uint16 shareBps) external {
        require(shareBps <= 10_000, "bad share");
        require(payout != address(0), "bad payout");
        require(REGISTRY.ownerOf(_tokenId(aCode)) == msg.sender, "not code owner");
        sellers[keccak256(bytes(aCode))] = Seller(payout, shareBps, true);
        emit Registered(aCode, payout, shareBps);
    }

    // ── PERMISSIONLESS settle: the buyer's signature is the only authorization ──
    function settle(
        string calldata aCode, string calldata sCode,
        address from, uint256 value, uint256 validAfter, uint256 validBefore,
        bytes32 nonce, uint8 v, bytes32 r, bytes32 s
    ) external {
        Seller memory sel = sellers[keccak256(bytes(aCode))];
        require(sel.set, "seller not registered");

        // Pull the buyer's USDC into this contract. receiveWithAuthorization
        // requires msg.sender == to == address(this), so ONLY this function can
        // redeem the buyer's signature — nobody can move the funds without splitting.
        USDC.receiveWithAuthorization(from, address(this), value,
            validAfter, validBefore, nonce, v, r, s);

        address builder = _payoutOf(sCode);
        uint256 cut = builder == address(0) ? 0 : (value * sel.shareBps) / 10_000;
        if (cut > 0) require(USDC.transfer(builder, cut), "cut xfer");
        require(USDC.transfer(sel.payout, value - cut), "seller xfer");   // remainder; never blocked

        emit Settled(aCode, sCode, from, sel.payout, builder, value, cut, nonce);
    }

    // builder code → ERC-721 token id: ASCII bytes as a big-endian int.
    // Mirrors resolver.to_token_id() exactly.
    function _tokenId(string memory code) internal pure returns (uint256 id) {
        bytes memory b = bytes(code);
        require(b.length > 0 && b.length <= 32, "bad code len");
        for (uint256 i = 0; i < b.length; i++) id = (id << 8) | uint8(b[i]);
    }

    // Resolve `s` → payout on-chain; unregistered/blank → address(0) (whole amount to seller).
    function _payoutOf(string calldata code) internal view returns (address) {
        if (bytes(code).length == 0 || bytes(code).length > 32) return address(0);
        try REGISTRY.payoutAddress(_tokenId(code)) returns (address a) { return a; }
        catch { return address(0); }
    }
}
```

Why each line matters:

- **`register` is gated by `ownerOf(aCode)`** — only the wallet that owns the
  seller's builder-code NFT can set that seller's share, so nobody can spoof or
  grief.
- **The builder address is resolved on-chain** by the contract itself, reusing
  the exact registry `resolver.py` reads — so the relayer never passes an address
  and can't misdirect the cut.
- **`receiveWithAuthorization` (not `transferWithAuthorization`)** is deliberate:
  because it requires `msg.sender == to`, only *this contract* can redeem the
  buyer's signature. That closes a front-running hole where someone could push
  the buyer's funds into the contract via a plain transfer *without* triggering
  the split, leaving USDC stuck. Confirm your x402 client/scheme signs a
  `receiveWithAuthorization` authorization to the splitter address — this is the
  one client-side thing to verify (§10).
- **Unresolvable `s` → whole amount to the seller**, nothing ever stranded.
- **Atomic** — pull-in and both pay-outs in one tx; no custody, no reentrancy
  surface (USDC has no transfer hooks; add `ReentrancyGuard` as belt-and-suspenders
  if you like).
- **Replay-safe** — USDC enforces `nonce` uniqueness + `validBefore`.

### 4.2 Seller registration (declare your share)

One-time, self-service, signed by the wallet that owns the `a` code:

```python
# register_share.py (sketch) — uses web3.py or your tx tooling
split.functions.register("bc_seller", SELLER_PAYOUT_ADDR, 5000).transact()  # 5000 bps = 50%
```

That's the entire "declare it" step in Model B. Editing it later is another
`register` call. The number is now public and contract-enforced.

### 4.3 The relayer — the "facilitator" you run

**This is not a trusted facilitator.** `settle()` is permissionless — the buyer's
signature is the only authorization it needs — so the relayer just pays gas and
relays. It holds no funds and decides no amounts; it cannot cheat even if it
wanted to. In x402 terms it implements the standard facilitator interface, and
you point the middleware's `facilitator` config at its URL.

```python
# relayer.py (sketch) — the two x402 facilitator endpoints
# POST /verify  → standard EIP-3009 checks (valid sig, payer has funds, matches requirements)
# POST /settle  → instead of USDC.transferWithAuthorization, call the splitter:

def settle(payment_payload, requirements):
    auth   = extract_eip3009_authorization(payment_payload)   # from, value, validAfter, validBefore, nonce, v/r/s
    a_code = seller_code_for(requirements)                    # your declared `a`
    s_code = service_code_from(payment_payload)               # the buyer's `s` (same capture as builder_codes_from_payment)

    tx = split.functions.settle(
        a_code, s_code,
        auth.from_, auth.value, auth.validAfter, auth.validBefore,
        auth.nonce, auth.v, auth.r, auth.s,
    ).build_transaction({...})
    tx_hash = send(sign(tx, RELAYER_KEY))                     # relayer pays gas on Base

    return {"success": True, "transaction": tx_hash,
            "network": "eip155:8453", "payer": auth.from_}     # x402 PAYMENT-RESPONSE shape
```

The relayer needs: a funded gas wallet on Base (~cents/tx), a Base RPC, and the
splitter address. That's it.

### 4.4 Buyer side — essentially unchanged

The buyer signs to whatever `payTo` the 402 advertises; the seller sets
`payTo = splitter`, so **no buyer code change is needed for where funds go**. The
buyer still attaches their `s` with `buyer_client.BuilderCodeClientExtension`. The
only nuance is the authorization *variant* (`receiveWithAuthorization` vs
`transferWithAuthorization`) — see §10.

---

## 5. Trust model — why it's not "facilitator-agnostic," and why that's fine

A natural question: *can't I just point `payTo` at the splitter and use the stock
CDP facilitator?* No — and the reason is precise:

- **A transfer *into* a contract triggers no code.** USDC is a plain ERC-20 (no
  ERC-777 hooks). A stock facilitator settles by calling
  `USDC.transferWithAuthorization(buyer → payTo)` — that just moves funds into the
  contract, where they **sit**. No function runs; nothing splits.
- To split, the settle tx has to **call your contract's function**
  (`settle(...)`), which pulls the funds in and fans them out atomically. Stock
  facilitators only know how to call the *token*, not your arbitrary contract.

```
stock facilitator:  USDC.transferWithAuthorization(buyer → splitter)  → funds stuck, no split
what you need:      BuilderSplit.settle(aCode, sCode, sig…)           → pull in + split, atomic
                    └─ stock facilitators will not emit this
```

**So: not drop-in agnostic.** But the thing you add is a *dumb, permissionless
relayer* (§4.3), not a *trusted facilitator*. The requirement is **capability**
(does it call your contract?), not **trust** (the contract decides the money).

You can't have all three of **agnostic + atomic + trustless**:

- Stay agnostic (let CDP dump funds in, split in a *second* tx) and you lose
  atomicity — worse, a plain transfer-in emits only a `Transfer` event with **no
  `s` code**, and a later "distribute" tx can't read the original tx's calldata
  on-chain, so the contract has **no trustless way to recover per-payment
  attribution**. That drags a trusted distributor + off-chain accounting back in —
  the exact thing this design removes.

The custom relayer is the price of trustless atomic splitting, and it's a small
price: anyone can run it, nobody has to trust it.

---

## 6. Buy vs build — why not splits.org / Sablier / Superfluid

Every off-the-shelf split/stream platform assumes a **known, stable recipient
set** ("this cap table splits revenue among these N addresses"). This mechanism
is the opposite:

- The builder is **different on every payment** — whatever `s` the buyer submits.
- And `payTo` is fixed in the 402 **before the buyer reveals `s`**, so `payTo`
  can't be a per-builder split address; it must be one fixed contract per seller
  that reads `s` at settle and routes.

A fixed-recipient split can't be that contract — so with any of these you'd
*still* need the router in front, i.e. the thing you were trying to avoid.

| Platform | Built for | Why it misses |
|---|---|---|
| **0xSplits / splits.org** | Split a pool among a **fixed** set; accumulate → `distribute` | Recipients fixed at creation; can't route to a runtime `s`. Non-atomic, no EIP-3009 pull. *Downstream* use — a builder pointing their code's payout at their own split — is **not possible today**: Base pins `payout_address` to the project owner's account (not user-configurable yet). |
| **Sablier** | **Time-based** streaming / vesting / airdrops | Wrong shape — streams X over a duration; we need an instant one-shot carve. |
| **Superfluid** | Continuous **streams** + pool distributions | Time/stream oriented, *and* requires wrapping USDC into a SuperToken — friction against plain-USDC x402. |
| *(OZ PaymentSplitter, thirdweb Split, Drips, Disperse)* | Fixed-recipient splits / batch sends | Same "known recipient set" assumption. |

**Verdict:** build the ~40-line router. It's less code *and* less trust surface
than gluing a fixed-recipient platform into a dynamic-recipient flow (you'd need
the router *plus* their model *plus* still be non-atomic). Reuse everything
around it: USDC's `receiveWithAuthorization`, the Base registry for resolution,
OpenZeppelin for scaffolding, and 0xSplits *below* a builder who wants to split
their own cut.

---

## 7. Wiring into this repo

| File | Change |
|---|---|
| `server_example.py` | Set the route's `payTo` = **splitter address**. Keep the `declare_builder_code(a)` step (identity still matters). Point the x402 middleware's `facilitator` config at **your relayer** (§4.3). `SettleTxCaptureMiddleware` becomes optional — your relayer already knows the settle tx hash and can record it directly. |
| `resolver.py` | Unchanged. Reused *inside the contract* conceptually (same registry, same `to_token_id` derivation — mirrored by `_tokenId`); still handy off-chain for logging/reporting. |
| `builder_code.py` | `declare_builder_code` unchanged. Optionally add a `declare_revshare(bps)` that advertises the share in the 402 for **buyer transparency** (informational in Model B). |
| `tracking.py` | `record_payment` now records a split that **already happened on-chain** — store the settle tx hash, builder payout, `cut`, and `value − cut`. `compute_payouts` becomes **reconciliation/reporting**, not an obligation ledger: nothing is owed because it's already paid. |
| `backfill.py` / `backfill_sql.py` | **Largely redundant** in this design. `w`-recovery existed to read attribution that only appeared post-settle; here `a` and `s` are first-class arguments to `settle()` and land in the `Settled` event, so attribution is captured at settlement, not backfilled. Keep them only if you still settle some routes the old way. |

**Net effect:** the ledger stops tracking *who you owe* and starts recording
*what already settled* — because the split is now part of the payment.

---

## 8. Edge cases & caveats

- **Unregistered / blank `s`** → `builder == address(0)` → whole `value` goes to
  the seller. Never stranded.
- **Multiple `s` codes** (layered clients — `normalize_service_codes` joins them
  comma-separated). The contract as written pays **one** `sCode`. Decide a v1
  policy: pay the primary (first) code, or extend `settle` to accept an array +
  per-code weights. Recommend **single `s` for v1**.
- **`s` is self-asserted.** Resolving `s` tells you *where the money goes* (the
  registered owner's payout), not that the submitter is *entitled* to that code.
  For affiliation that's usually acceptable (the owner gets paid regardless of who
  submitted), but there's no proof-of-authorization on `s`.
- **Mainnet + your relayer only.** The whole enforced-split path runs on **Base
  mainnet** with **your relayer**. On testnet / the free `x402.org` facilitator,
  nothing splits.
- **Seller share is per-seller (or per seller×builder if you key on
  `keccak(aCode, sCode)`), not per-request.** If a seller needs a different share
  on every call, Model B can't express it — you'd fall back to a facilitator that
  passes `shareBps` per call (and re-take on the trust). For a fixed "I pay
  referrers X%," Model B is strictly better.
- **Relayer liveness.** If your relayer is down, payments don't settle. It's a
  standard service to keep up (funded gas wallet, RPC, nonce management). Because
  `settle` is permissionless, a stuck payment can be settled by *anyone* holding
  the buyer's signature — so you can add a fallback submitter.
- **Gas.** The relayer pays Base gas (~cents/tx). Decide whether that's your cost
  or netted from the payment.

---

## 9. Verification / testing

1. **Unit — token id.** Assert `_tokenId("leap_wallet")` in the contract equals
   `resolver.to_token_id("leap_wallet")` (`131042744964646850211374452`). The
   32-test suite in `test_builder_code.py` already anchors the Python side.
2. **Fork test.** Fork Base mainnet, register a seller, craft a buyer EIP-3009
   `receiveWithAuthorization` to the splitter, call `settle`, assert: builder
   payout received `cut`, seller received `value − cut`, `Settled` emitted with
   the right codes.
3. **Unregistered `s`.** Same, with a bogus `sCode` → assert seller receives the
   full `value`, nothing stuck in the contract.
4. **Griefing.** Try to move funds in via a plain `transferWithAuthorization`
   *without* `settle` — confirm your choice of `receiveWithAuthorization` blocks
   it (only the contract can redeem).
5. **Live smoke.** One real mainnet payment through your relayer; read the split
   tx on Basescan; confirm both legs. (The ERC-8021 `a/s/w` checker at
   `buildercode-checker.vercel.app` is for suffix-style settlements — in this
   design attribution is in the `Settled` event + calldata args instead.)

---

## 10. Open decisions for your integration

1. **Authorization variant.** Confirm your x402 client can sign a
   **`receiveWithAuthorization`** (to = splitter). If it only signs
   `transferWithAuthorization`, either (a) add a scheme/client that signs the
   `receive` variant, or (b) accept the front-run window and add a rescue/sweep
   path. Strongly prefer `receive`.
2. **Share granularity.** Per-seller (`keccak(aCode)`) or per seller×builder
   (`keccak(aCode, sCode)`)? Start per-seller.
3. **Multiple `s` policy.** Single primary code for v1 (recommended) vs weighted
   array.
4. **Fee model.** Model A (share out of seller margin — buyer's price unchanged;
   this doc's default) vs Model B-surcharge (advertise `price = base + fee`). Same
   contract; only which number the 402 advertises differs.
5. **Who runs the relayer / eats gas.** You, a keeper, or a permissionless
   fallback. Decide gas accounting.
6. **Registry keying & upgradeability.** Immutable splitter (safest) vs an owner
   who can pause. Recommend immutable + no admin over funds.

---

## References

- ERC-8021 builder-code extension spec —
  https://github.com/x402-foundation/x402/blob/main/specs/extensions/builder_code.md
- Base Builder Codes registry — https://github.com/base/builder-codes
  (`0x000000BC7E6457e610fe52Dcc0ca5b3ce59C8E80`)
- EIP-3009 `transferWithAuthorization` / `receiveWithAuthorization` —
  Circle FiatToken (Base USDC `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`)
- CDP Builder Codes — https://docs.cdp.coinbase.com/x402/core-concepts/builder-codes
- This repo: `resolver.py` (code → payout), `builder_code.py` (declare/decode),
  `server_example.py` (declare/capture/observe), `tracking.py` (ledger).
```

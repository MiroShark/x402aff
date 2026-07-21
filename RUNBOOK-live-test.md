# Live mainnet test — the CDP path (no facilitator)

Prove the enforced split works on **real Base mainnet with real USDC**, for a few
cents, in two stages. Stage 1 needs **zero changes to your production server** —
it proves the on-chain half in isolation. Stage 2 puts a real payment through
your live endpoint.

The path under test (from `payto.py` + `distribute.py`): the buyer names the
builder on the **unpaid** request → your 402 sets `payTo` = the per-pair PushSplit
→ the **stock CDP facilitator** settles a plain USDC transfer into it (sponsored
gas, `a`/`s`/`w` still written) → anyone calls `distribute` to fan it out. No
settler, no facilitator to run. Verified on a fork in
`fork-test/CdpPath.t.sol`; this does it with real money.

## Prereqs

- A **paid Base RPC** (Alchemy/QuickNode). `export X402_BASE_RPC=https://...`
  The public `mainnet.base.org` 429s after a few calls and will strand a resolve.
- Your seller payout address. `export X402_SELLER_PAYOUT=0xYourWallet`
- A builder code to attribute to. Use a real registered one you control, or
  `leap_wallet` (registered, resolves to `0xa06c…54c8`) just to watch the money
  land somewhere real.
- A funded Base wallet with ~$1 USDC + a few cents of ETH for gas (this is your
  distributor/tester wallet — **not** the settler; there is no settler here).

---

## Stage 1 — on-chain half, no server change (~$0.10 + gas)

This simulates exactly what CDP does (a plain USDC transfer to `payTo`) so you can
prove deploy + distribute on mainnet without touching production.

1. **Predict the pair's split address.**
   ```bash
   python3 -c "
   import payto
   r = payto.payto_for_request('leap_wallet')   # uses X402_SELLER_PAYOUT
   print('payTo (split):', r.address)
   print('attributed   :', r.attributed, '| already deployed:', r.split_deployed)
   "
   ```

2. **Send a tiny USDC transfer to that address** from your test wallet — this is
   the exact byte a stock facilitator would emit. Send e.g. `$0.10` (100000 units)
   of USDC on Base to the predicted address. (Any wallet/cast; no special calldata.)

3. **Confirm the money is sitting in the split, then release it.**
   ```bash
   python3 -c "
   import payto, distribute
   r = payto.payto_for_request('leap_wallet')
   calls, bal = distribute.distribute_plan(r.plan)
   print('balance in split (units):', bal)
   for c in calls:
       print(f'  [{c.step}] {c.target}')
       print(f'      {c.data}')
   "
   ```
   Submit each printed `(target, data)` as a transaction from your test wallet
   (deploy_split first if present, then distribute). Gas is cents.

4. **Assert the payout.** 10% to the builder, 90% to your seller. For a $0.10
   transfer: builder (`0xa06c…54c8` for leap_wallet) `0.009999`, seller
   `0.089999`, 2 units dust left in the split. Cross-check against:
   ```bash
   python3 -c "import payto; print(payto.payto_for_request('leap_wallet').plan.amounts(0.10))"
   ```

✅ **Stage 1 proves:** the per-pair split deploys at the predicted address, funds
sent by *anyone* distribute correctly, and the ratio is enforced on-chain — with
real USDC, no server involved.

---

## Stage 2 — one real payment through the live endpoint (~$0.02 + $0)

The endpoint is already deployed and wired (see `test_endpoint/`): it declares
`a = bc_c12702g2`, reads `X-Builder-Code`, and sets `payTo` = the per-pair split
via `payto.payto_for_request`. Live at:

    https://x402-endpoint-production.up.railway.app

Verified: `X-Builder-Code: leap_wallet` → `payTo = 0x3773…2e38` (the split), $0.02
USDC on `eip155:8453`; no header → `payTo` = the seller wallet.

1. **Pay it once** with a funded Base wallet (holds ≥ $0.02 USDC; CDP sponsors
   gas, so no ETH needed):
   ```bash
   pip install eth-account
   export BUYER_PRIVATE_KEY=0x...        # YOUR funded Base wallet
   export BUILDER_CODE=leap_wallet
   python3 test_endpoint/buyer.py
   ```
   `buyer.py` sends the header, attaches `s = leap_wallet` inside the payment, and
   prints the **settle tx hash** on success.

2. **Confirm CDP settled into the split & wrote attribution.** With the tx hash:
   - Paste it into <https://buildercode-checker.vercel.app/> → you should see
     `a = bc_c12702g2`, the facilitator's `w`, and `s = leap_wallet`. **This is
     the proof CDP still writes attribution on this path.**
   - Check the split's balance rose by $0.02 (`distribute.split_balance_units`).

3. **Distribute** exactly as Stage 1, step 3 — deploy if first use, then
   distribute. `leap_wallet` (`0xa06c…54c8`) gets 10%, the seller (`0x95dd…71cc`)
   gets 90%.

✅ **Stage 2 proves:** the real CDP facilitator, with sponsored gas, settles into
your per-pair split AND writes `a`/`s`/`w` — i.e. you get enforced split *and*
keep attribution, with no facilitator of your own.

---

## Rollback / safety

- Every `payto_for_request` failure falls back to `X402_SELLER_PAYOUT`, so a bad
  resolve is an **unsplit** payment, never a failed one. To disable the whole
  path, stop reading the header — `payTo` reverts to your wallet.
- Funds in a split are **never stranded**: the split is ownerless and
  `distribute` is permissionless, so worst case they wait for the next
  `distribute` call. The builder can even call it themselves.
- Start with `leap_wallet` (not your own code) for Stage 1 so a mistake sends
  test cents to a stranger's registered wallet, not into a contract you have to
  reason about.

## What to hand back for review

The Stage 2 settle tx hash — paste it and I'll confirm the `a`/`s`/`w` decode and
that the split balance/payout reconciles to the unit against `amounts()`.

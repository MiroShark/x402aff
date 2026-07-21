# Live mainnet test — the CDP path (no facilitator)

Prove the enforced split works on **real Base mainnet with real USDC**, for a few
cents, in two stages. Stage 1 needs **zero changes to your production server** —
it proves the on-chain half in isolation. Stage 2 puts a real payment through
your live endpoint.

The path under test (from `payto.py` + `distribute.py`): the buyer names the
builder on the **unpaid** request → your 402 sets `payTo` = the per-pair PushSplit
→ the **stock CDP facilitator** settles a plain USDC transfer into it (sponsored
gas, `a`/`s`/`w` still written) → anyone calls `distribute` to fan it out. No
settler, no 7702, no facilitator to run. Verified on a fork in
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

## Stage 2 — one real payment through your live endpoint (~$0.05 + gas)

Now prove CDP itself settles into the split and writes attribution.

1. **Wire request-time `payTo` into your 402 handler.** Where you currently set a
   fixed `payTo`, resolve it per request instead:
   ```python
   import payto

   code = payto.builder_code_from_headers(request.headers)   # X-Builder-Code
   pt = payto.payto_for_request(code)                         # never raises
   route_config.pay_to = pt.address                           # split, or your wallet
   if pt.error:
       log.warning("payTo resolve failed, unsplit: %s", pt.error)
   ```
   Keep `declare_builder_code(a)` — identity still matters. No-header requests get
   your normal wallet, so **existing buyers are unaffected**.

2. **Make one paid request that sends the header.** From an x402 client, set
   `X-Builder-Code: leap_wallet` (or your test code) on the initial request and
   pay the (small) price. The buyer also attaches `s` as usual via
   `buyer_client.BuilderCodeClientExtension` — belt and suspenders.

3. **Confirm CDP settled into the split.** Grab the settle tx hash (from your
   `SettleTxCaptureMiddleware` or the `X-PAYMENT-RESPONSE` header) and:
   - Paste it into <https://buildercode-checker.vercel.app/> → you should see your
     `a`, the facilitator's `w`, and `s = leap_wallet`. **This is the proof CDP
     still writes attribution on this path.**
   - Check the split's balance rose by the price (`distribute.split_balance_units`).

4. **Distribute** exactly as Stage 1, step 3 — deploy if first use, then
   distribute. Builder gets 10%, you get 90%.

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

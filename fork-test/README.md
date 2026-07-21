# fork-test — the settle path, against real Base mainnet

These tests fork Base mainnet locally and run the settler's legs against the
**live** USDC and 0xSplits PushSplitFactory contracts. Fake buyer, fake money
(`deal`), real contracts — no keys, no funds, nothing broadcast.

```bash
cd fork-test
forge install foundry-rs/forge-std     # first time only
forge test -vv                         # ~4s
```

## What they pin down

**`PullLeg.t.sol`** — why the route's `payTo` must be the settler account.
EIP-3009 binds the buyer's signature to one recipient, and
`receiveWithAuthorization` additionally requires `msg.sender == to`. The per-pair
split address is derived from the `s` code, which only arrives *inside* the
payment — after `payTo` was fixed — so the buyer can never have signed to it.

| Test | Asserts |
|---|---|
| `test_A_pullIntoSplitAddress_reverts` | pulling straight into the split **reverts** (sig commits to `payTo`) |
| `test_B_settlerCannotRedeemAuthMadeToSeller` | settler redeeming a seller-addressed auth **reverts** (`msg.sender != to`) |
| `test_C_payToSettler_works` | `payTo` = settler → pull succeeds |
| `test_D_transferWithAuthorization_isOpenToAnyone` | a Receive-typed sig can't be replayed as a Transfer (no griefing hole) |

**`EndToEnd.t.sol`** — the full settle, executing the **exact calldata
`settler.py` emits** (the hex blobs are verbatim output of
`create_split_calldata` / `distribute_calldata` / `is_deployed_calldata`; nothing
here re-implements the kit's encoding). Covers pull → fund → deploy →
distribute, and asserts the deploy lands on the predicted address.

It also pins the payout math. Splits v2 retains 1 base unit (warm-slot gas
optimization) and floors each share, so a $1.00 payment pays **$0.099999 /
$0.899999**, not $0.10 / $0.90 — mirrored by `split.amounts_units()` so the
ledger reconciles to the unit.

## Regenerating the calldata

If you change the plan (share, addresses), regenerate the constants:

```bash
python3 -c "
import split, settler
plan = split.build_split_plan(
    seller_payout='0x2222222222222222222222222222222222222222',
    builder_payout='0x1111111111111111111111111111111111111111',
    builder_code='bc_alice', builder_share_bps=1000)
print('IS_DEPLOYED', settler.is_deployed_calldata(plan))
print('CREATE     ', settler.create_split_calldata(plan))
print('DISTRIBUTE ', settler.distribute_calldata(plan,
      distributor='0x7702770277027702770277027702770277027702'))
"
```

## Not covered

Signing and submitting the multicall from a real 7702 account — the tests
`vm.prank` the settler rather than authorizing one. That's the remaining
integration step.

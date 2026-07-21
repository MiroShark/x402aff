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

**`Delegation7702.t.sol`** — the same settle, but through a **real signed EIP-7702
delegation** instead of `vm.prank`. A minimal `BatchExecutor` (`execute(Call[])`,
gated on `msg.sender == address(this)`) is attached to the settler EOA with
`vm.signAndAttachDelegation`, and the four legs run as one batch.

| Test | Asserts |
|---|---|
| `test_delegationIsAttached` | the settler's code becomes `0xef0100 ++ implementation` |
| `test_thirdPartyCannotDriveTheBatch` | a delegated EOA is still only drivable by itself (`NotSelf`) |
| `test_fullSettle_1USDC_via7702` / `_10USDC_via7702` | identical payouts to `EndToEnd.t.sol`; settler retains 0 USDC |
| `test_failingLegRollsBackThePull` | a failing leg unwinds the pull — buyer keeps their USDC, nothing stranded |

That last one is the reason to batch at all: as four separate transactions, a
failure after the pull would leave the buyer's money sitting on the settler.

⚠️ The settler key here is high-entropy on purpose. Weak keys (`0x7702`, `0x01`,
…) already carry **live** 7702 delegations on Base from sweeper bots, so a fork
using one starts dirty and `test_delegationIsAttached` fails against real state.

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

`DISTRIBUTE` embeds the `distributor` address, so `Delegation7702.t.sol` needs
its own blob — regenerate that one with
`distributor='0xbE248921595D7fbA89190D70CCB1b12cAFD02342'` (that suite's settler).
`IS_DEPLOYED` and `CREATE` are settler-independent and shared by both suites.

## Not covered

Broadcasting for real: these tests sign a delegation but never submit a
transaction to Base. Gas accounting, nonce management, and settler liveness are
still yours — do a testnet or small mainnet run before real money moves.

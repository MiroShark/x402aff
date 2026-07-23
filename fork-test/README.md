# fork-test - the CDP path, against real Base mainnet

This forks Base mainnet locally and runs the settle → split → distribute path
against the **live** USDC and 0xSplits PushSplitFactory contracts. Fake buyer,
fake money (`deal`), real contracts - no keys, no funds, nothing broadcast.

```bash
cd fork-test
forge install foundry-rs/forge-std     # first time only
forge test -vv                         # 7 tests; ~6s warm, longer on a cold fork
```

## What it pins down

**`CdpPath.t.sol`** - the exact path the x402aff kit uses in production: the stock CDP
facilitator settles a **plain USDC transfer** to `payTo` (= the per-pair split),
then anyone releases it. The hex blobs are verbatim output of
`push_split.create_split_calldata` / `distribute_calldata` / `is_deployed_calldata`
- nothing here re-implements the x402aff kit's encoding.

| Test | Asserts |
|---|---|
| `test_cdpPlainTransferThenAnyoneDistributes` | a plain transfer into the split + an **unrelated** address deploying and calling `distribute` pays out **10% / 90%** exactly; the caller keeps 0 (`distributionIncentive = 0`) |
| `test_fundsWaitingInTheSplitCannotBeRedirected` | the split is **ownerless** (`owner() == address(0)`) and the seller can't pause or claw back the builder's cut while it waits |

Together they prove the two claims the whole design rests on: **no settler is
needed** (a plain CDP transfer + a permissionless distribute is enough), and the
cut is **enforced** (once funds land, the immutable split is the only thing that
decides the amounts).

It also pins the payout math. Splits v2 retains 1 base unit (warm-slot gas
optimization) and floors each share, so a $1.00 payment pays **$0.099999 /
$0.899999** - mirrored by `split.amounts_units()` so the ledger reconciles to the
unit.

**`SplitAbuse.t.sol`** - the adversarial half: once USDC lands in the split, can
*anyone* redirect, skim, or grief it? Each test actively tries and fails, so
"only the baked-in recipients (builder + seller) can ever be paid, at the baked
ratio" holds against a motivated attacker - not just on the happy path.

| Test | Attack it defeats |
|---|---|
| `test_tamperedDistributeStructReverts` | calling `distribute` with a tampered `Split` (swap in the attacker, flip the allocations, add a skim incentive) reverts - the wallet hash-checks the struct; not one unit moves |
| `test_tamperedParamsResolveToADifferentAddress` | a malicious split paying the attacker resolves to a **different** CREATE2 address, so the funded address never holds their variant |
| `test_anyCallerTriggersButOnlyBakedRecipientsGetPaid` | `distribute` is permissionless but the caller/named distributor earns **0** (`distributionIncentive = 0`); only builder/seller are paid |
| `test_noOwnerLeversForAnyone` | the split is ownerless, so `updateSplit` / `setPaused` revert for **everyone** - including the seller trying to claw back or freeze the cut |
| `test_frontRunningTheDeployChangesNothing` | front-running the deterministic deploy (attacker as creator) lands the same address with the same recipients; `distribute` still pays builder/seller |

The two-layer lock behind these: the split **address is CREATE2 over the params**
(so funds only accrue at the address for exactly those recipients), and
`distribute` **re-validates the struct against the hash stored at creation** (so
even at the funded address the money can't go anywhere else).

## Regenerating the calldata

If you change the plan (share, addresses), regenerate the constants:

```bash
python3 -c "
import split, push_split
plan = split.build_split_plan(
    seller_payout='0x2222222222222222222222222222222222222222',
    builder_payout='0x1111111111111111111111111111111111111111',
    builder_code='bc_alice', builder_share_bps=1000)
print('IS_DEPLOYED', push_split.is_deployed_calldata(plan))
print('CREATE     ', push_split.create_split_calldata(plan))
print('DISTRIBUTE ', push_split.distribute_calldata(plan))
"
```

## Not covered

Broadcasting for real - the test uses `deal`/`prank` and never submits a
transaction to Base. The one live-money step (a real payment through a deployed
x402 endpoint) is left to you: point your endpoint's `payTo` at the split and pay
it once. The path was also validated end-to-end on mainnet during development.

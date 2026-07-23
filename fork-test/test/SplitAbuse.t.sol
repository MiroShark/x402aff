// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test, console} from "forge-std/Test.sol";

/// Adversarial probe of the PushSplit trust model on LIVE Base mainnet.
///
/// Question: once USDC lands in the per-(seller, builder) split, can ANYONE
/// redirect it, skim it, front-run it, or otherwise get paid something other than
/// the baked-in (builder 10% / seller 90%)? This test tries every such move and
/// asserts each fails - so "only the right addresses ever get paid" holds even
/// against a motivated attacker.

struct SplitParams {
    address[] recipients;
    uint256[] allocations;
    uint256 totalAllocation;
    uint16 distributionIncentive;
}

interface ISplitFactory {
    function createSplitDeterministic(SplitParams calldata s, address owner, address creator, bytes32 salt)
        external
        returns (address);
    function isDeployed(SplitParams calldata s, address owner, bytes32 salt) external view returns (address, bool);
}

interface ISplitWallet {
    function distribute(SplitParams calldata s, address token, address distributor) external;
    function updateSplit(SplitParams calldata s) external;
    function setPaused(bool) external;
    function owner() external view returns (address);
}

interface IUSDC {
    function balanceOf(address) external view returns (uint256);
}

contract SplitAbuseTest is Test {
    IUSDC constant USDC = IUSDC(0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913);
    ISplitFactory constant FACTORY = ISplitFactory(0x8E8eB0cC6AE34A38B67D5Cf91ACa38f60bc3Ecf4);

    // Fresh, fund-less addresses (makeAddr) so payout math reads as absolute
    // balances - vanity addrs like 0x111.. already hold real USDC on mainnet.
    address BUILDER; // registry-registered payout
    address SELLER; // the API's wallet
    address ATTACKER;

    uint256 constant AMOUNT = 1_000_000; // $1.00 sitting in the split

    // The one legit split the kit predicts: [builder:1000, seller:9000], incentive 0.
    function _legit() internal view returns (SplitParams memory p) {
        address[] memory r = new address[](2);
        r[0] = BUILDER;
        r[1] = SELLER;
        uint256[] memory a = new uint256[](2);
        a[0] = 1000;
        a[1] = 9000;
        p = SplitParams({recipients: r, allocations: a, totalAllocation: 10000, distributionIncentive: 0});
    }

    function setUp() public {
        vm.createSelectFork("https://mainnet.base.org");
        BUILDER = makeAddr("builder");
        SELLER = makeAddr("seller");
        ATTACKER = makeAddr("attacker");
    }

    // Fund + deploy the legit split, returning its address.
    function _fundedLegitSplit() internal returns (address split) {
        bool exists;
        (split, exists) = FACTORY.isDeployed(_legit(), address(0), bytes32(0));
        deal(address(USDC), split, AMOUNT); // model the CDP facilitator settling into it
        if (!exists) {
            vm.prank(ATTACKER); // permissionless deploy - even the attacker can do it, harmlessly
            address created = FACTORY.createSplitDeterministic(_legit(), address(0), address(0), bytes32(0));
            assertEq(created, split, "deploy lands at the predicted address");
        }
    }

    /// (1) The attacker calls distribute with a TAMPERED split struct that names
    ///     themselves. The wallet validates the struct against the hash it stored
    ///     at creation, so any tampering reverts - funds stay put.
    function test_tamperedDistributeStructReverts() public {
        address split = _fundedLegitSplit();
        uint256 before = USDC.balanceOf(split);

        // a) swap the builder recipient for the attacker
        SplitParams memory hijack = _legit();
        hijack.recipients[0] = ATTACKER;
        vm.prank(ATTACKER);
        vm.expectRevert();
        ISplitWallet(split).distribute(hijack, address(USDC), ATTACKER);

        // b) flip the allocations to grab the 90% leg
        SplitParams memory flip = _legit();
        flip.allocations[0] = 9000;
        flip.allocations[1] = 1000;
        vm.prank(ATTACKER);
        vm.expectRevert();
        ISplitWallet(split).distribute(flip, address(USDC), ATTACKER);

        // c) sneak in a self-skim via a non-zero distribution incentive
        SplitParams memory inc = _legit();
        inc.distributionIncentive = 5000;
        vm.prank(ATTACKER);
        vm.expectRevert();
        ISplitWallet(split).distribute(inc, address(USDC), ATTACKER);

        assertEq(USDC.balanceOf(split), before, "not a single unit moved on any tamper attempt");
        assertEq(USDC.balanceOf(ATTACKER), 0, "attacker got nothing");
    }

    /// (2) Can the attacker instead CREATE a malicious split (paying themselves)
    ///     that collides with the funded address? No: the address is CREATE2 over
    ///     the params, so any different recipient/allocation/incentive resolves to
    ///     a DIFFERENT address - the money is never there.
    function test_tamperedParamsResolveToADifferentAddress() public view {
        (address legit,) = FACTORY.isDeployed(_legit(), address(0), bytes32(0));

        SplitParams memory hijack = _legit();
        hijack.recipients[0] = ATTACKER;
        (address a1,) = FACTORY.isDeployed(hijack, address(0), bytes32(0));

        SplitParams memory inc = _legit();
        inc.distributionIncentive = 5000;
        (address a2,) = FACTORY.isDeployed(inc, address(0), bytes32(0));

        // a different owner would also be a different address (and non-zero owner = mutable)
        (address a3,) = FACTORY.isDeployed(_legit(), ATTACKER, bytes32(0));

        assertTrue(a1 != legit, "swapping a recipient => different address");
        assertTrue(a2 != legit, "adding an incentive => different address");
        assertTrue(a3 != legit, "a different owner => different address");
        assertEq(USDC.balanceOf(a1), 0, "no funds at the attacker's variant");
    }

    /// (3) distribute is PERMISSIONLESS but not exploitable: whoever triggers it
    ///     (here the attacker, naming themselves as distributor) moves the money to
    ///     the baked recipients and earns nothing, because incentive == 0.
    function test_anyCallerTriggersButOnlyBakedRecipientsGetPaid() public {
        address split = _fundedLegitSplit();

        vm.prank(ATTACKER);
        ISplitWallet(split).distribute(_legit(), address(USDC), ATTACKER); // attacker distributes, names self

        uint256 dist = AMOUNT - 1; // 1 unit kept warm
        assertEq(USDC.balanceOf(BUILDER), dist * 1000 / 10000, "builder = floor(10%)");
        assertEq(USDC.balanceOf(SELLER), dist * 9000 / 10000, "seller  = floor(90%)");
        assertEq(USDC.balanceOf(ATTACKER), 0, "the distributor skims nothing (incentive 0)");
    }

    /// (4) There is no admin path out: the split is ownerless, so every onlyOwner
    ///     lever (retarget recipients, pause to strand funds) is dead - for the
    ///     attacker AND for the seller, who has the most to gain from clawback.
    function test_noOwnerLeversForAnyone() public {
        address split = _fundedLegitSplit();
        assertEq(ISplitWallet(split).owner(), address(0), "split is ownerless");

        SplitParams memory hijack = _legit();
        hijack.recipients[0] = ATTACKER;

        vm.prank(SELLER);
        vm.expectRevert();
        ISplitWallet(split).updateSplit(hijack); // seller tries to retarget the builder's cut

        vm.prank(ATTACKER);
        vm.expectRevert();
        ISplitWallet(split).updateSplit(hijack);

        vm.prank(SELLER);
        vm.expectRevert();
        ISplitWallet(split).setPaused(true); // seller tries to freeze the builder out
    }

    /// (5) Front-running the deploy is harmless: the attacker deploys the split
    ///     FIRST (they can - it's permissionless), but it's the same address with
    ///     the same recipients, so distribute still pays builder/seller and the
    ///     attacker is out only gas.
    function test_frontRunningTheDeployChangesNothing() public {
        (address split,) = FACTORY.isDeployed(_legit(), address(0), bytes32(0));

        vm.prank(ATTACKER);
        FACTORY.createSplitDeterministic(_legit(), address(0), ATTACKER, bytes32(0)); // attacker as creator, too

        assertEq(ISplitWallet(split).owner(), address(0), "attacker-deployed split is still ownerless");

        deal(address(USDC), split, AMOUNT);
        vm.prank(ATTACKER);
        ISplitWallet(split).distribute(_legit(), address(USDC), ATTACKER);

        uint256 dist = AMOUNT - 1;
        assertEq(USDC.balanceOf(BUILDER), dist * 1000 / 10000, "builder still paid");
        assertEq(USDC.balanceOf(SELLER), dist * 9000 / 10000, "seller still paid");
        assertEq(USDC.balanceOf(ATTACKER), 0, "creator field grants no funds/rights");
    }
}

// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test, console} from "forge-std/Test.sol";

interface IUSDC {
    function balanceOf(address) external view returns (uint256);
    function transfer(address, uint256) external returns (bool);
}

/// Can the STOCK CDP facilitator settle straight into a Splits PushSplit?
///
/// INTEGRATION.md §5 says a transfer into a contract "just moves funds in, where
/// they sit. No function runs; nothing splits." True — but for a PushSplit that
/// is not the end of the story: `distribute` is permissionless and reads the
/// balance at call time, so ANY caller can fan the funds out afterwards.
///
/// This models the CDP path with no settler at all:
///   1. facilitator does a plain USDC transfer → payTo (= the split address)
///   2. anyone deploys the split (createSplitDeterministic is permissionless)
///   3. anyone calls distribute
contract CdpPathTest is Test {
    IUSDC constant USDC = IUSDC(0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913);
    address constant FACTORY = 0x8E8eB0cC6AE34A38B67D5Cf91ACa38f60bc3Ecf4;

    address constant BUILDER = 0x1111111111111111111111111111111111111111;
    address constant SELLER  = 0x2222222222222222222222222222222222222222;

    // Nobody special: not the seller, not the builder, not a settler.
    address constant RANDO = 0x00000000000000000000000000000000DeaDBeef;
    address constant BUYER = 0x000000000000000000000000000000000000bEEF;

    bytes constant IS_DEPLOYED_CD = hex"cd6bc121000000000000000000000000000000000000000000000000000000000000006000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000008000000000000000000000000000000000000000000000000000000000000000e000000000000000000000000000000000000000000000000000000000000027100000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000200000000000000000000000011111111111111111111111111111111111111110000000000000000000000002222222222222222222222222222222222222222000000000000000000000000000000000000000000000000000000000000000200000000000000000000000000000000000000000000000000000000000003e80000000000000000000000000000000000000000000000000000000000002328";

    bytes constant CREATE_CD = hex"f79918b00000000000000000000000000000000000000000000000000000000000000080000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000008000000000000000000000000000000000000000000000000000000000000000e000000000000000000000000000000000000000000000000000000000000027100000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000200000000000000000000000011111111111111111111111111111111111111110000000000000000000000002222222222222222222222222222222222222222000000000000000000000000000000000000000000000000000000000000000200000000000000000000000000000000000000000000000000000000000003e80000000000000000000000000000000000000000000000000000000000002328";

    // distributor field names 0x7702… — deliberately NOT the caller below, to
    // show the field is an incentive payee, not an authorization.
    bytes constant DISTRIBUTE_CD = hex"2d3f55370000000000000000000000000000000000000000000000000000000000000060000000000000000000000000833589fcd6edb6e08f4c7c32d4f71b54bda029130000000000000000000000007702770277027702770277027702770277027702000000000000000000000000000000000000000000000000000000000000008000000000000000000000000000000000000000000000000000000000000000e000000000000000000000000000000000000000000000000000000000000027100000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000200000000000000000000000011111111111111111111111111111111111111110000000000000000000000002222222222222222222222222222222222222222000000000000000000000000000000000000000000000000000000000000000200000000000000000000000000000000000000000000000000000000000003e80000000000000000000000000000000000000000000000000000000000002328";

    function setUp() public {
        vm.createSelectFork("https://mainnet.base.org");
        deal(address(USDC), BUYER, 100_000_000);
    }

    function _predict() internal returns (address addr, bool deployed) {
        (bool ok, bytes memory ret) = FACTORY.call(IS_DEPLOYED_CD);
        require(ok, "isDeployed reverted");
        (addr, deployed) = abi.decode(ret, (address, bool));
    }

    function test_cdpPlainTransferThenAnyoneDistributes() public {
        uint256 amount = 1_000_000; // $1.00
        (address splitAddr,) = _predict();

        uint256 builderBefore = USDC.balanceOf(BUILDER);
        uint256 sellerBefore  = USDC.balanceOf(SELLER);

        // 1. What a STOCK facilitator does: a plain ERC-20 transfer to payTo.
        //    No contract call, no settler, no 7702.
        vm.prank(BUYER);
        USDC.transfer(splitAddr, amount);
        assertEq(USDC.balanceOf(splitAddr), amount, "funds landed in the split");
        assertEq(builderBefore, USDC.balanceOf(BUILDER), "nothing split yet");

        // 2. Deploy the split — permissionless, from an unrelated address.
        vm.prank(RANDO);
        (bool okCreate, bytes memory createRet) = FACTORY.call(CREATE_CD);
        require(okCreate, "createSplitDeterministic reverted");
        assertEq(abi.decode(createRet, (address)), splitAddr, "predicted address holds");

        // 3. Distribute — also from the unrelated address, which is NOT the
        //    `distributor` named in the calldata.
        vm.prank(RANDO);
        (bool okDist,) = splitAddr.call(DISTRIBUTE_CD);
        require(okDist, "distribute reverted");

        uint256 builderGot = USDC.balanceOf(BUILDER) - builderBefore;
        uint256 sellerGot  = USDC.balanceOf(SELLER)  - sellerBefore;

        console.log("builder received:", builderGot);
        console.log("seller  received:", sellerGot);
        console.log("left in split   :", USDC.balanceOf(splitAddr));
        console.log("rando kept      :", USDC.balanceOf(RANDO));

        uint256 distributable = amount - 1;
        assertEq(builderGot, distributable * 1000 / 10000, "builder = floor(10%)");
        assertEq(sellerGot,  distributable * 9000 / 10000, "seller  = floor(90%)");
        assertEq(USDC.balanceOf(RANDO), 0, "an unrelated distributor earns nothing");
    }

    /// The funds are not merely "sitting" — while they wait, nobody can redirect
    /// them. The split is immutable (owner = 0), so there is no admin path out.
    function test_fundsWaitingInTheSplitCannotBeRedirected() public {
        uint256 amount = 1_000_000;
        (address splitAddr,) = _predict();

        vm.prank(BUYER);
        USDC.transfer(splitAddr, amount);

        vm.prank(RANDO);
        (bool okCreate,) = FACTORY.call(CREATE_CD);
        require(okCreate, "create reverted");

        // The seller — the party with the most to gain — tries to pull the
        // builder's cut back out. There is no owner, so there is no lever.
        vm.prank(SELLER);
        (bool okOwner, bytes memory ownerRet) = splitAddr.call(abi.encodeWithSignature("owner()"));
        require(okOwner, "owner() reverted");
        assertEq(abi.decode(ownerRet, (address)), address(0), "split must be ownerless");

        vm.prank(SELLER);
        (bool okPause,) = splitAddr.call(abi.encodeWithSignature("setPaused(bool)", true));
        assertFalse(okPause, "seller must not be able to pause distributions");

        console.log("split is ownerless; the 10% cannot be clawed back");
    }
}

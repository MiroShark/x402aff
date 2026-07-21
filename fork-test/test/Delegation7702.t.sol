// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test, console} from "forge-std/Test.sol";

interface IUSDC {
    function balanceOf(address) external view returns (uint256);
    function transfer(address, uint256) external returns (bool);
    function DOMAIN_SEPARATOR() external view returns (bytes32);
    function receiveWithAuthorization(
        address from, address to, uint256 value,
        uint256 validAfter, uint256 validBefore, bytes32 nonce,
        uint8 v, bytes32 r, bytes32 s
    ) external;
}

/// Minimal EIP-7702 implementation: batch calls in the EOA's own context.
///
/// `msg.sender == address(this)` is the standard 7702 authorization check — when
/// the settler EOA sends a transaction *to its own address*, the delegated code
/// runs with both equal to the settler. Nobody else can drive the batch.
contract BatchExecutor {
    struct Call { address target; uint256 value; bytes data; }

    error NotSelf();
    error CallFailed(uint256 index, bytes reason);

    function execute(Call[] calldata calls) external payable {
        if (msg.sender != address(this)) revert NotSelf();
        for (uint256 i; i < calls.length; ++i) {
            (bool ok, bytes memory ret) =
                calls[i].target.call{value: calls[i].value}(calls[i].data);
            if (!ok) revert CallFailed(i, ret);
        }
    }
}

/// The gap `EndToEnd.t.sol` leaves open: it `vm.prank`s the settler, so nothing
/// exercises the 7702 delegation the real settler depends on. This runs the same
/// four legs through a *real* signed delegation on the same mainnet fork.
///
/// DISTRIBUTE_CD is verbatim `settler.distribute_calldata(plan, distributor=SETTLER)`;
/// CREATE_CD / IS_DEPLOYED_CD are settler-independent (the split params name only
/// the builder + seller) so they are the same blobs `EndToEnd.t.sol` uses.
contract Delegation7702Test is Test {
    IUSDC constant USDC = IUSDC(0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913);
    address constant FACTORY = 0x8E8eB0cC6AE34A38B67D5Cf91ACa38f60bc3Ecf4;

    bytes32 constant RECEIVE_TYPEHASH = keccak256(
        "ReceiveWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)"
    );

    uint256 buyerPk = 0xB0FFED;
    address buyer;

    // A real key, so the delegation can actually be signed. Must be high-entropy
    // even for a test: weak keys (0x7702, 0x01, …) already carry *live* 7702
    // delegations on Base from sweeper bots, so the fork would start dirty.
    //   cast keccak "x402-affiliation fork-test settler key v1"
    //   cast wallet address --private-key <that>   # verified code == 0x on mainnet
    uint256 constant settlerPk =
        0x33e1c50970ba5601ff8c56dc3cb6c2b4602ac65ce50e113c7a513b54b848a758;
    address constant SETTLER = 0xbE248921595D7fbA89190D70CCB1b12cAFD02342;

    address constant BUILDER = 0x1111111111111111111111111111111111111111;
    address constant SELLER  = 0x2222222222222222222222222222222222222222;

    BatchExecutor impl;

    bytes constant IS_DEPLOYED_CD = hex"cd6bc121000000000000000000000000000000000000000000000000000000000000006000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000008000000000000000000000000000000000000000000000000000000000000000e000000000000000000000000000000000000000000000000000000000000027100000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000200000000000000000000000011111111111111111111111111111111111111110000000000000000000000002222222222222222222222222222222222222222000000000000000000000000000000000000000000000000000000000000000200000000000000000000000000000000000000000000000000000000000003e80000000000000000000000000000000000000000000000000000000000002328";

    bytes constant CREATE_CD = hex"f79918b00000000000000000000000000000000000000000000000000000000000000080000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000008000000000000000000000000000000000000000000000000000000000000000e000000000000000000000000000000000000000000000000000000000000027100000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000200000000000000000000000011111111111111111111111111111111111111110000000000000000000000002222222222222222222222222222222222222222000000000000000000000000000000000000000000000000000000000000000200000000000000000000000000000000000000000000000000000000000003e80000000000000000000000000000000000000000000000000000000000002328";

    bytes constant DISTRIBUTE_CD = hex"2d3f55370000000000000000000000000000000000000000000000000000000000000060000000000000000000000000833589fcd6edb6e08f4c7c32d4f71b54bda02913000000000000000000000000be248921595d7fba89190d70ccb1b12cafd02342000000000000000000000000000000000000000000000000000000000000008000000000000000000000000000000000000000000000000000000000000000e000000000000000000000000000000000000000000000000000000000000027100000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000200000000000000000000000011111111111111111111111111111111111111110000000000000000000000002222222222222222222222222222222222222222000000000000000000000000000000000000000000000000000000000000000200000000000000000000000000000000000000000000000000000000000003e80000000000000000000000000000000000000000000000000000000000002328";

    function setUp() public {
        vm.createSelectFork("https://mainnet.base.org");
        buyer = vm.addr(buyerPk);
        deal(address(USDC), buyer, 100_000_000);
        assertEq(vm.addr(settlerPk), SETTLER, "settlerPk must derive SETTLER");
        impl = new BatchExecutor();
    }

    function _sign(address to, uint256 value, bytes32 nonce)
        internal view returns (uint8 v, bytes32 r, bytes32 s)
    {
        bytes32 structHash = keccak256(
            abi.encode(RECEIVE_TYPEHASH, buyer, to, value, uint256(0), type(uint256).max, nonce)
        );
        bytes32 digest = keccak256(
            abi.encodePacked("\x19\x01", USDC.DOMAIN_SEPARATOR(), structHash)
        );
        (v, r, s) = vm.sign(buyerPk, digest);
    }

    function _predict() internal returns (address addr, bool deployed) {
        (bool ok, bytes memory ret) = FACTORY.call(IS_DEPLOYED_CD);
        require(ok, "isDeployed reverted");
        (addr, deployed) = abi.decode(ret, (address, bool));
    }

    /// The four settle legs, as `settler.settlement_calls()` orders them.
    function _settleCalls(uint256 amount, address splitAddr, bytes32 nonce)
        internal view returns (BatchExecutor.Call[] memory calls)
    {
        (uint8 v, bytes32 r, bytes32 s) = _sign(SETTLER, amount, nonce);

        calls = new BatchExecutor.Call[](4);
        calls[0] = BatchExecutor.Call(address(USDC), 0, abi.encodeCall(
            IUSDC.receiveWithAuthorization,
            (buyer, SETTLER, amount, 0, type(uint256).max, nonce, v, r, s)
        ));
        calls[1] = BatchExecutor.Call(address(USDC), 0,
            abi.encodeCall(IUSDC.transfer, (splitAddr, amount)));
        calls[2] = BatchExecutor.Call(FACTORY, 0, CREATE_CD);
        calls[3] = BatchExecutor.Call(splitAddr, 0, DISTRIBUTE_CD);
    }

    /// The delegation designator itself: 0xef0100 ++ implementation address.
    function test_delegationIsAttached() public {
        assertEq(SETTLER.code.length, 0, "settler starts as a bare EOA");
        vm.signAndAttachDelegation(address(impl), settlerPk);
        assertEq(
            SETTLER.code,
            abi.encodePacked(hex"ef0100", address(impl)),
            "7702 designator must point at the executor"
        );
    }

    /// Nobody but the settler can drive the batch, even once delegated.
    function test_thirdPartyCannotDriveTheBatch() public {
        vm.signAndAttachDelegation(address(impl), settlerPk);
        (address splitAddr,) = _predict();
        BatchExecutor.Call[] memory calls =
            _settleCalls(1_000_000, splitAddr, keccak256("griefer"));

        vm.prank(address(0xDEAD));
        vm.expectRevert(BatchExecutor.NotSelf.selector);
        BatchExecutor(payable(SETTLER)).execute(calls);
    }

    function test_fullSettle_1USDC_via7702()  public { _run(1_000_000); }
    function test_fullSettle_10USDC_via7702() public { _run(10_000_000); }

    function _run(uint256 amount) internal {
        vm.signAndAttachDelegation(address(impl), settlerPk);

        (address splitAddr, bool deployed) = _predict();
        console.log("predicted split:", splitAddr, "| deployed:", deployed);
        assertEq(deployed, false, "fresh pair should not be deployed yet");

        uint256 builderBefore = USDC.balanceOf(BUILDER);
        uint256 sellerBefore  = USDC.balanceOf(SELLER);

        BatchExecutor.Call[] memory calls =
            _settleCalls(amount, splitAddr, keccak256(abi.encode("7702", amount)));

        // The settler EOA sends one transaction to its own address; the delegated
        // code runs all four legs atomically with msg.sender == SETTLER.
        vm.prank(SETTLER);
        BatchExecutor(payable(SETTLER)).execute(calls);

        uint256 builderGot = USDC.balanceOf(BUILDER) - builderBefore;
        uint256 sellerGot  = USDC.balanceOf(SELLER)  - sellerBefore;
        uint256 stuck      = USDC.balanceOf(splitAddr);

        console.log("builder received:", builderGot);
        console.log("seller  received:", sellerGot);
        console.log("left in split   :", stuck);

        assertEq(USDC.balanceOf(SETTLER), 0, "settler must not retain any USDC");

        // Identical to EndToEnd.t.sol — the delegation changes nothing about the math.
        uint256 distributable = amount - 1;
        assertEq(builderGot, distributable * 1000 / 10000, "builder = floor(10% of amount-1)");
        assertEq(sellerGot,  distributable * 9000 / 10000, "seller  = floor(90% of amount-1)");
        assertEq(stuck, amount - builderGot - sellerGot, "remainder stays as dust");
        assertLe(stuck, 2, "dust must be <= 2 units regardless of amount");
    }

    /// The reason to batch at all: a failing leg unwinds the pull. With four
    /// separate transactions the buyer's USDC would already be on the settler.
    function test_failingLegRollsBackThePull() public {
        vm.signAndAttachDelegation(address(impl), settlerPk);

        uint256 amount = 1_000_000;
        (address splitAddr,) = _predict();
        BatchExecutor.Call[] memory calls =
            _settleCalls(amount, splitAddr, keccak256("atomicity"));

        // Break the last leg: transfer more USDC than the settler will ever hold.
        calls[3] = BatchExecutor.Call(address(USDC), 0,
            abi.encodeCall(IUSDC.transfer, (SELLER, type(uint256).max)));

        uint256 buyerBefore = USDC.balanceOf(buyer);

        vm.prank(SETTLER);
        vm.expectRevert();
        BatchExecutor(payable(SETTLER)).execute(calls);

        assertEq(USDC.balanceOf(buyer), buyerBefore, "buyer keeps their USDC");
        assertEq(USDC.balanceOf(SETTLER), 0, "nothing stranded on the settler");
        assertEq(USDC.balanceOf(splitAddr), 0, "nothing stranded in the split");
        console.log("failing leg reverted the whole batch; buyer still holds", buyerBefore);
    }
}

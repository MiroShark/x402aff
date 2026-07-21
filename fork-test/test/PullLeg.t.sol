// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test, console} from "forge-std/Test.sol";

interface IUSDC {
    function balanceOf(address) external view returns (uint256);
    function DOMAIN_SEPARATOR() external view returns (bytes32);
    function receiveWithAuthorization(
        address from, address to, uint256 value,
        uint256 validAfter, uint256 validBefore, bytes32 nonce,
        uint8 v, bytes32 r, bytes32 s
    ) external;
    function transferWithAuthorization(
        address from, address to, uint256 value,
        uint256 validAfter, uint256 validBefore, bytes32 nonce,
        uint8 v, bytes32 r, bytes32 s
    ) external;
}

/// Does the settler's `pull_funds` leg actually work on Base mainnet?
///
/// settler.py:238 claims: "receiveWithAuthorization {amt} USDC from {buyer} -> {split_addr}"
/// But the buyer signed against `payTo`, fixed in the 402 BEFORE the `s` code
/// (and therefore the per-pair split address) was known. These tests pin down
/// whether that leg is constructible at all.
contract PullLegTest is Test {
    IUSDC constant USDC = IUSDC(0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913);

    bytes32 constant RECEIVE_TYPEHASH = keccak256(
        "ReceiveWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)"
    );

    uint256 buyerPk = 0xB0FFED;
    address buyer;

    address SELLER_PAYTO = address(0x2222222222222222222222222222222222222222);
    address SPLIT_ADDR   = address(0x5911777777777777777777777777777777777777); // per-(seller,builder)
    address SETTLER      = address(0x7702770277027702770277027702770277027702);

    uint256 constant VALUE = 1_000_000; // $1.00 USDC (6dp)

    function setUp() public {
        vm.createSelectFork("https://mainnet.base.org");
        buyer = vm.addr(buyerPk);
        deal(address(USDC), buyer, 10_000_000);
    }

    /// Sign an EIP-3009 ReceiveWithAuthorization for (from=buyer, to=`to`).
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

    /// A) The shipped v1 as written: buyer signed to=payTo(seller), settler tries
    ///    to redeem it INTO the per-pair split address.
    function test_A_pullIntoSplitAddress_reverts() public {
        bytes32 nonce = keccak256("A");
        // buyer signs what the 402 advertised: $1 -> SELLER_PAYTO
        (uint8 v, bytes32 r, bytes32 s) = _sign(SELLER_PAYTO, VALUE, nonce);

        // settler tries to pull those funds into the split instead
        vm.prank(SETTLER);
        vm.expectRevert();
        USDC.receiveWithAuthorization(
            buyer, SPLIT_ADDR, VALUE, 0, type(uint256).max, nonce, v, r, s
        );
        console.log("A) pull -> split address: REVERTED (sig commits to payTo)");
    }

    /// B) Even targeting the address the buyer DID sign, the settler can't redeem:
    ///    receiveWithAuthorization requires msg.sender == to.
    function test_B_settlerCannotRedeemAuthMadeToSeller() public {
        bytes32 nonce = keccak256("B");
        (uint8 v, bytes32 r, bytes32 s) = _sign(SELLER_PAYTO, VALUE, nonce);

        vm.prank(SETTLER);
        vm.expectRevert();
        USDC.receiveWithAuthorization(
            buyer, SELLER_PAYTO, VALUE, 0, type(uint256).max, nonce, v, r, s
        );
        console.log("B) settler redeeming a payTo=seller auth: REVERTED (msg.sender != to)");
    }

    /// C) THE FIX: set payTo = the settler account. Buyer signs to=SETTLER (one
    ///    fixed address, known at 402 time), settler redeems it, then forwards
    ///    to the split. Same tx, so still atomic.
    function test_C_payToSettler_works() public {
        bytes32 nonce = keccak256("C");
        (uint8 v, bytes32 r, bytes32 s) = _sign(SETTLER, VALUE, nonce);

        vm.prank(SETTLER);
        USDC.receiveWithAuthorization(
            buyer, SETTLER, VALUE, 0, type(uint256).max, nonce, v, r, s
        );

        assertEq(USDC.balanceOf(SETTLER), VALUE, "settler should hold the pulled USDC");
        console.log("C) payTo=settler pull: OK, settler holds", USDC.balanceOf(SETTLER));
    }

    /// D) Griefing check the doc calls for: can anyone shove the buyer's funds
    ///    into the split via transferWithAuthorization, bypassing the split logic?
    function test_D_transferWithAuthorization_isOpenToAnyone() public {
        bytes32 nonce = keccak256("D");
        // NOTE: transferWithAuthorization has its own typehash; signing a
        // Receive-typed digest must NOT be redeemable as a Transfer.
        (uint8 v, bytes32 r, bytes32 s) = _sign(SPLIT_ADDR, VALUE, nonce);

        vm.prank(address(0xDEAD));
        vm.expectRevert();
        USDC.transferWithAuthorization(
            buyer, SPLIT_ADDR, VALUE, 0, type(uint256).max, nonce, v, r, s
        );
        console.log("D) Receive-typed sig cannot be replayed as Transfer: REVERTED");
    }
}

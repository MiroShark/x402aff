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

/// Full settle path, executing the EXACT calldata `settler.py` emits.
///
/// The hex blobs below are verbatim output of settler.create_split_calldata /
/// distribute_calldata / is_deployed_calldata for the plan:
///   seller 0x2222 (9000bps) + builder bc_alice 0x1111 (1000bps)
/// Nothing here re-implements the kit's encoding — that's the point.
contract EndToEndTest is Test {
    IUSDC constant USDC = IUSDC(0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913);
    address constant FACTORY = 0x8E8eB0cC6AE34A38B67D5Cf91ACa38f60bc3Ecf4;

    bytes32 constant RECEIVE_TYPEHASH = keccak256(
        "ReceiveWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)"
    );

    uint256 buyerPk = 0xB0FFED;
    address buyer;

    address constant BUILDER = 0x1111111111111111111111111111111111111111;
    address constant SELLER  = 0x2222222222222222222222222222222222222222;
    address constant SETTLER = 0x7702770277027702770277027702770277027702;

    uint256 constant VALUE = 1_000_000; // $1.00

    bytes constant IS_DEPLOYED_CD = hex"cd6bc121000000000000000000000000000000000000000000000000000000000000006000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000008000000000000000000000000000000000000000000000000000000000000000e000000000000000000000000000000000000000000000000000000000000027100000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000200000000000000000000000011111111111111111111111111111111111111110000000000000000000000002222222222222222222222222222222222222222000000000000000000000000000000000000000000000000000000000000000200000000000000000000000000000000000000000000000000000000000003e80000000000000000000000000000000000000000000000000000000000002328";

    bytes constant CREATE_CD = hex"f79918b00000000000000000000000000000000000000000000000000000000000000080000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000008000000000000000000000000000000000000000000000000000000000000000e000000000000000000000000000000000000000000000000000000000000027100000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000200000000000000000000000011111111111111111111111111111111111111110000000000000000000000002222222222222222222222222222222222222222000000000000000000000000000000000000000000000000000000000000000200000000000000000000000000000000000000000000000000000000000003e80000000000000000000000000000000000000000000000000000000000002328";

    bytes constant DISTRIBUTE_CD = hex"2d3f55370000000000000000000000000000000000000000000000000000000000000060000000000000000000000000833589fcd6edb6e08f4c7c32d4f71b54bda029130000000000000000000000007702770277027702770277027702770277027702000000000000000000000000000000000000000000000000000000000000008000000000000000000000000000000000000000000000000000000000000000e000000000000000000000000000000000000000000000000000000000000027100000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000200000000000000000000000011111111111111111111111111111111111111110000000000000000000000002222222222222222222222222222222222222222000000000000000000000000000000000000000000000000000000000000000200000000000000000000000000000000000000000000000000000000000003e80000000000000000000000000000000000000000000000000000000000002328";

    function setUp() public {
        vm.createSelectFork("https://mainnet.base.org");
        buyer = vm.addr(buyerPk);
        deal(address(USDC), buyer, 100_000_000);
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

    /// The kit's predict_split_address, on-chain: factory.isDeployed(split,0,0).
    function _predict() internal returns (address addr, bool deployed) {
        (bool ok, bytes memory ret) = FACTORY.call(IS_DEPLOYED_CD);
        require(ok, "isDeployed reverted");
        (addr, deployed) = abi.decode(ret, (address, bool));
    }

    /// Whole path in ONE tx from the settler: pull -> fund split -> deploy -> distribute.
    function test_fullSettle_1USDC() public { _run(1_000_000); }
    function test_fullSettle_10USDC() public { _run(10_000_000); }

    function _run(uint256 amount) internal {
        (address splitAddr, bool deployed) = _predict();
        console.log("predicted split:", splitAddr, "| deployed:", deployed);
        assertEq(deployed, false, "fresh pair should not be deployed yet");

        bytes32 nonce = keccak256(abi.encode("e2e", amount));
        (uint8 v, bytes32 r, bytes32 s) = _sign(SETTLER, amount, nonce);

        uint256 builderBefore = USDC.balanceOf(BUILDER);
        uint256 sellerBefore  = USDC.balanceOf(SELLER);

        vm.startPrank(SETTLER);

        // leg 1 — pull the buyer's USDC (payTo = settler, the fix from PullLeg.t.sol)
        USDC.receiveWithAuthorization(buyer, SETTLER, amount, 0, type(uint256).max, nonce, v, r, s);

        // leg 2 — forward into the counterfactual split (funding before deploy is supported)
        USDC.transfer(splitAddr, amount);

        // leg 3 — deploy the per-pair PushSplit  [kit calldata, verbatim]
        (bool okCreate, bytes memory createRet) = FACTORY.call(CREATE_CD);
        require(okCreate, "createSplitDeterministic reverted");
        address created = abi.decode(createRet, (address));
        assertEq(created, splitAddr, "deploy must land on the predicted address");

        // leg 4 — distribute  [kit calldata, verbatim]
        (bool okDist,) = splitAddr.call(DISTRIBUTE_CD);
        require(okDist, "distribute reverted");

        vm.stopPrank();

        uint256 builderGot = USDC.balanceOf(BUILDER) - builderBefore;
        uint256 sellerGot  = USDC.balanceOf(SELLER)  - sellerBefore;
        uint256 stuck      = USDC.balanceOf(splitAddr);

        console.log("builder received:", builderGot);
        console.log("seller  received:", sellerGot);
        console.log("left in split   :", stuck);

        // Splits v2 keeps 1 unit in the split (warm-slot gas opt) and floors each
        // recipient's share, so payouts are (amount-1)*bps/10000 rounded down.
        uint256 distributable = amount - 1;
        assertEq(builderGot, distributable * 1000 / 10000, "builder = floor(10% of amount-1)");
        assertEq(sellerGot,  distributable * 9000 / 10000, "seller  = floor(90% of amount-1)");
        assertEq(stuck, amount - builderGot - sellerGot, "remainder stays as dust");
        assertLe(stuck, 2, "dust must be <= 2 units regardless of amount");
    }
}

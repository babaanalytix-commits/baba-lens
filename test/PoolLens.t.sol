// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {PoolLens} from "../src/PoolLens.sol";

// Offline tests: the arithmetic and the NO_DATA paths, against small mocks. Live correctness (fees
// to the wei against collect()) is acceptance/run.py's job; these run anywhere, including CI.

contract MockV3Factory {
    address public pool;

    function set(address p) external {
        pool = p;
    }

    function getPool(address, address, uint24) external view returns (address) {
        return pool;
    }
}

contract MockV3Pool {
    address public factory;
    address public token0;
    address public token1;
    int24 public tick;
    uint256 public feeGrowthGlobal0X128;
    uint256 public feeGrowthGlobal1X128;
    mapping(int24 => uint256) public outside0;
    mapping(bytes32 => uint256[5]) internal pos;
    bool public observeReverts;
    int56 public cumPast;
    int56 public cumNow;

    constructor(address f, address t0, address t1) {
        factory = f;
        token0 = t0;
        token1 = t1;
    }

    function setTick(int24 t) external {
        tick = t;
    }

    function setGlobal(uint256 g0) external {
        feeGrowthGlobal0X128 = g0;
    }

    function setOutside(int24 t, uint256 o0) external {
        outside0[t] = o0;
    }

    function setPos(bytes32 k, uint256[5] calldata v) external {
        pos[k] = v;
    }

    function setObserve(bool reverts, int56 past, int56 now_) external {
        observeReverts = reverts;
        cumPast = past;
        cumNow = now_;
    }

    function slot0() external view returns (uint160, int24, uint16, uint16, uint16, uint8, bool) {
        return (uint160(1 << 96), tick, 0, 7, 7, 0, true);
    }

    function fee() external pure returns (uint24) {
        return 3000;
    }

    function tickSpacing() external pure returns (int24) {
        return 60;
    }

    function liquidity() external pure returns (uint128) {
        return 1;
    }

    function ticks(int24 t)
        external
        view
        returns (uint128, int128, uint256, uint256, int56, uint160, uint32, bool)
    {
        return (1, 0, outside0[t], 0, 0, 0, 0, true);
    }

    function positions(bytes32 k) external view returns (uint128, uint256, uint256, uint128, uint128) {
        uint256[5] memory v = pos[k];
        return (uint128(v[0]), v[1], v[2], uint128(v[3]), uint128(v[4]));
    }

    function observe(uint32[] calldata) external view returns (int56[] memory c, uint160[] memory s) {
        require(!observeReverts, "OLD");
        c = new int56[](2);
        s = new uint160[](2);
        c[0] = cumPast;
        c[1] = cumNow;
    }
}

contract MockToken {
    function decimals() external pure returns (uint8) {
        return 18;
    }
}

contract MockNpm {
    address public factory;
    address public token0;
    address public token1;
    address public owner;
    uint256[12] internal p;

    constructor(address f, address t0, address t1, address o) {
        factory = f;
        token0 = t0;
        token1 = t1;
        owner = o;
    }

    function set(uint256[12] calldata v) external {
        p = v;
    }

    function ownerOf(uint256) external view returns (address) {
        return owner;
    }

    function positions(uint256)
        external
        view
        returns (uint96, address, address, address, uint24, int24, int24, uint128, uint256, uint256, uint128, uint128)
    {
        return (0, address(0), token0, token1, uint24(p[4]), int24(int256(p[5])), int24(int256(p[6])),
            uint128(p[7]), p[8], p[9], uint128(p[10]), uint128(p[11]));
    }
}

contract ShortSlot0 {
    function slot0() external pure returns (uint160, int24, uint16, uint16, uint16) {
        return (1, 2, 3, 4, 5);
    }
}

contract MockArbSys {
    function arbBlockNumber() external pure returns (uint256) {
        return 70_000_000;
    }
}

contract PoolLensTest is Test {
    PoolLens lens;
    MockV3Factory factory;
    MockV3Pool pool;
    address t0;
    address t1;

    function setUp() public {
        lens = new PoolLens();
        factory = new MockV3Factory();
        t0 = address(new MockToken());
        t1 = address(new MockToken());
        pool = new MockV3Pool(address(factory), t0, t1);
        factory.set(address(pool));
    }

    // ── NO_DATA, never zero ──────────────────────────────────────────────────────────────────

    function test_eoa_is_no_data() public view {
        PoolLens.PoolState memory s = lens.poolState(address(0xBEEF));
        assertEq(uint8(s.status), uint8(PoolLens.Status.NO_DATA));
        assertEq(s.reason, bytes32("slot0"));
    }

    function test_unrecognised_slot0_shape_is_no_data() public {
        PoolLens.PoolState memory s = lens.poolState(address(new ShortSlot0()));
        assertEq(uint8(s.status), uint8(PoolLens.Status.NO_DATA));
    }

    function test_real_shape_ok() public view {
        PoolLens.PoolState memory s = lens.poolState(address(pool));
        assertEq(uint8(s.status), uint8(PoolLens.Status.OK));
        assertEq(uint8(s.kind), uint8(PoolLens.Kind.UNIV3));
    }

    function test_factory_that_disowns_the_pool_is_no_data() public {
        factory.set(address(0x1234));
        PoolLens.PoolState memory s = lens.poolState(address(pool));
        assertEq(uint8(s.status), uint8(PoolLens.Status.NO_DATA));
        assertEq(s.reason, bytes32("factory_mismatch"));
        assertEq(s.token0, address(0)); // cleared, not half-filled
    }

    // ── TWAP ─────────────────────────────────────────────────────────────────────────────────

    function test_twap_rounds_toward_negative_infinity() public {
        pool.setObserve(false, 0, -7);
        assertEq(lens.twap(address(pool), 2).arithmeticMeanTick, -4); // -3.5 -> -4, not -3
        pool.setObserve(false, 0, 7);
        assertEq(lens.twap(address(pool), 2).arithmeticMeanTick, 3);
        pool.setObserve(false, 0, -8);
        assertEq(lens.twap(address(pool), 2).arithmeticMeanTick, -4); // exact, no extra step
    }

    function test_twap_insufficient_history_is_no_data_not_zero() public {
        pool.setObserve(true, 0, 0);
        PoolLens.Twap memory t = lens.twap(address(pool), 3600);
        assertEq(uint8(t.status), uint8(PoolLens.Status.NO_DATA));
        assertEq(t.reason, bytes32("observe_reverted"));
    }

    function test_twap_zero_window_is_no_data() public view {
        assertEq(uint8(lens.twap(address(pool), 0).status), uint8(PoolLens.Status.NO_DATA));
    }

    // ── fees: wrapping arithmetic ────────────────────────────────────────────────────────────

    function test_fee_growth_wraps_like_the_pool() public {
        // global 100, below 200, above 50 -> inside = 100 - 200 - 50 = -150 (mod 2^256)
        // checkpoint -350 (mod 2^256)       -> delta  = 200 per unit;  L = 2^127 -> 100 wei owed
        address owner = address(0xA11CE);
        MockNpm npm = new MockNpm(address(factory), t0, t1, owner);
        pool.setTick(0);
        pool.setGlobal(100);
        pool.setOutside(-60, 200);
        pool.setOutside(60, 50);
        uint256 last = type(uint256).max - 349; // == -350 mod 2^256
        uint256 L = 1 << 127;
        npm.set([uint256(0), 0, 0, 0, 3000, uint256(int256(-60)), 60, L, last, 0, 0, 0]);
        bytes32 key = keccak256(abi.encodePacked(address(npm), int24(-60), int24(60)));
        pool.setPos(key, [L, last, 0, 0, 0]);

        PoolLens.PositionState memory p = lens.position(address(npm), 1);
        assertEq(uint8(p.status), uint8(PoolLens.Status.OK));
        assertEq(p.feeGrowthInside0X128, type(uint256).max - 149); // -150
        assertEq(p.fees0, 100);
        assertTrue(p.inRange);
    }

    function test_pool_side_cap_binds() public {
        // NPM thinks 100 is owed; the pool's own position will only release 98.
        address owner = address(0xA11CE);
        MockNpm npm = new MockNpm(address(factory), t0, t1, owner);
        pool.setTick(0);
        pool.setGlobal(200); // NPM side: 200 * 2^127 / 2^128 = 100
        uint256 L = 1 << 127;
        npm.set([uint256(0), 0, 0, 0, 3000, uint256(int256(-60)), 60, L, 0, 0, 0, 0]);
        bytes32 key = keccak256(abi.encodePacked(address(npm), int24(-60), int24(60)));
        pool.setPos(key, [L, 4, 0, 0, 0]); // pool checkpoint 4 -> 196 * 2^127 / 2^128 = 98
        assertEq(lens.position(address(npm), 1).fees0, 98);
    }

    function test_out_of_range_position() public {
        MockNpm npm = new MockNpm(address(factory), t0, t1, address(0xA11CE));
        pool.setTick(60); // == tickUpper: out of range (upper bound is exclusive)
        npm.set([uint256(0), 0, 0, 0, 3000, uint256(int256(-60)), 60, 0, 0, 0, 0, 0]);
        PoolLens.PositionState memory p = lens.position(address(npm), 1);
        assertEq(uint8(p.status), uint8(PoolLens.Status.OK));
        assertFalse(p.inRange);
        pool.setTick(-60); // == tickLower: in range
        assertTrue(lens.position(address(npm), 1).inRange);
    }

    // ── chain-specific ───────────────────────────────────────────────────────────────────────

    function test_core_price_off_hyperevm_is_not_supported() public view {
        PoolLens.CorePrice memory c = lens.corePrice(159);
        assertEq(uint8(c.status), uint8(PoolLens.Status.NOT_SUPPORTED));
    }

    function test_core_price_on_999_without_precompile_is_no_data() public {
        vm.chainId(999);
        PoolLens.CorePrice memory c = lens.corePrice(159);
        assertEq(uint8(c.status), uint8(PoolLens.Status.NO_DATA));
        assertEq(c.oraclePx, 0);
    }

    function test_block_number_is_block_number_without_arbsys() public {
        vm.roll(1234);
        assertEq(lens.poolState(address(pool)).blockNumber, 1234);
    }

    function test_block_number_comes_from_arbsys_on_arbitrum() public {
        vm.roll(26_000_000); // block.number on an Arbitrum chain is the parent chain's
        vm.etch(address(0x64), address(new MockArbSys()).code);
        assertEq(lens.poolState(address(pool)).blockNumber, 70_000_000);
        assertEq(lens.twap(address(pool), 0).blockNumber, 70_000_000);
    }
}

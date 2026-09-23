// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {FullMath} from "./vendor/uniswap-v4-core/FullMath.sol";
import {TickMath} from "./vendor/uniswap-v4-core/TickMath.sol";
import {SqrtPriceMath} from "./vendor/uniswap-v4-core/SqrtPriceMath.sol";

// Signatures only: the compiler derives every selector from these, so none is hand-typed. The
// lens calls them through staticcall with length checks, never through these interfaces' ABI
// decoders, because a return shape it does not recognise has to become NO_DATA, not a revert.
interface IPool {
    function slot0() external view;
    function token0() external view;
    function token1() external view;
    function fee() external view;
    function tickSpacing() external view;
    function liquidity() external view;
    function feeGrowthGlobal0X128() external view;
    function feeGrowthGlobal1X128() external view;
    function factory() external view;
    function ticks(int24 tick) external view;
    function positions(bytes32 key) external view;
    function observe(uint32[] calldata secondsAgos) external view;
}

interface ISlipstreamPool {
    function stakedLiquidity() external view;
    function gauge() external view;
}

interface INpm {
    function positions(uint256 tokenId) external view;
    function ownerOf(uint256 tokenId) external view;
    function factory() external view;
    function balanceOf(address owner) external view;
    function tokenOfOwnerByIndex(address owner, uint256 index) external view;
}

interface IUniV3Factory {
    function getPool(address tokenA, address tokenB, uint24 fee) external view;
}

interface ISlipstreamFactory {
    function getPool(address tokenA, address tokenB, int24 tickSpacing) external view;
}

interface IArbSys {
    function arbBlockNumber() external view;
}

interface IERC20Decimals {
    function decimals() external view;
}

/// @title PoolLens v1
/// @notice Read-only lens over concentrated-liquidity pools and position NFTs. Every field in one
///         return value is read in the same call, so it describes a single block. `blockNumber` is
///         this chain's own height (on Arbitrum chains, from ArbSys, not the parent-chain number).
/// @dev    No owner, no storage, no upgradeability, not payable, no delegatecall. Every outward
///         call is a low-level STATICCALL whose failure becomes a status, never a revert and never
///         a zero. A value the lens could not read is reported as NO_DATA, with the read that
///         failed named in `reason`; it is never returned as 0.
///
///         Venues are told apart by shape, then confirmed by the pool's own factory:
///           UNIV3      slot0 is 7 words, ticks() is 8 words, factory.getPool(t0, t1, uint24 fee)
///           SLIPSTREAM slot0 is 6 words, ticks() is 10 words, factory.getPool(t0, t1, int24 spacing)
///         Each factory reverts on the other's getPool signature, so the two cannot be confused.
contract PoolLens {
    enum Status {
        OK,
        NO_DATA,
        NOT_SUPPORTED
    }

    enum Kind {
        UNKNOWN,
        UNIV3,
        SLIPSTREAM
    }

    struct PoolState {
        Status status;
        bytes32 reason; // the read that failed; empty when status is OK
        Kind kind;
        address pool;
        address factory;
        address token0;
        address token1;
        uint8 decimals0;
        uint8 decimals1;
        uint24 fee;
        int24 tickSpacing;
        uint160 sqrtPriceX96;
        int24 tick;
        uint16 observationCardinality;
        uint128 liquidity;
        uint128 stakedLiquidity; // SLIPSTREAM only; 0 on UNIV3, where no liquidity can be staked
        address gauge; // SLIPSTREAM only; address(0) on UNIV3 or when the pool has no gauge
        uint256 feeGrowthGlobal0X128;
        uint256 feeGrowthGlobal1X128;
        uint256 blockNumber;
        uint256 blockTimestamp;
    }

    struct PositionState {
        Status status;
        bytes32 reason;
        Kind kind;
        address npm;
        uint256 tokenId;
        address owner;
        address pool;
        address token0;
        address token1;
        int24 tickLower;
        int24 tickUpper;
        uint128 liquidity;
        int24 tick;
        uint160 sqrtPriceX96;
        bool inRange; // tickLower <= tick < tickUpper, the pool's own definition of active
        bool staked; // SLIPSTREAM: the NFT is held by the pool's gauge
        uint256 amount0; // principal if all liquidity were removed at this block (rounded down, as burn does)
        uint256 amount1;
        uint256 fees0; // exactly what NPM.collect(max, max) would return to the owner at this block
        uint256 fees1;
        uint256 feeGrowthInside0X128; // live, rebuilt from the pool — not the position's checkpoint
        uint256 feeGrowthInside1X128;
        uint256 blockNumber;
        uint256 blockTimestamp;
    }

    struct Twap {
        Status status;
        bytes32 reason;
        address pool;
        uint32 secondsAgo;
        int24 arithmeticMeanTick;
        int56 tickCumulativePast;
        int56 tickCumulativeNow;
        uint256 blockNumber;
        uint256 blockTimestamp;
    }

    struct CorePrice {
        Status status;
        bytes32 reason;
        uint32 assetIndex;
        uint64 oraclePx; // raw; human price = oraclePx / 10**(6 - szDecimals) for a perp
        uint8 szDecimals;
        string coin;
        uint256 blockNumber;
        uint256 blockTimestamp;
    }

    string public constant VERSION = "PoolLens v1";

    uint256 private constant Q128 = 1 << 128;
    uint256 private constant HYPEREVM_CHAIN_ID = 999;
    // HyperCore read precompiles. Provenance: Hyperliquid docs, "HyperEVM > Interacting with
    // HyperCore" (read precompiles start at 0x…0800, oracle price example at 0x…0807), and
    // hyperliquid-dev/hyper-evm-lib test/utils/L1Read.sol @ 4eb7ab04: ORACLE_PX = 0x…0807,
    // PERP_ASSET_INFO = 0x…080a.
    address private constant ORACLE_PX_PRECOMPILE = 0x0000000000000000000000000000000000000807;
    address private constant PERP_ASSET_INFO_PRECOMPILE = 0x000000000000000000000000000000000000080a;
    uint256 private constant PRECOMPILE_GAS = 200_000;
    // Arbitrum chains (Robinhood Chain): block.number is the parent chain's block, not this chain's.
    // Provenance: docs.arbitrum.io "Block gas limit, numbers and time" and the precompiles reference
    // (ArbSys at 0x…64). On Base, HyperEVM and Arc nothing lives at 0x…64 (checked 2026-09-23).
    address private constant ARBSYS = 0x0000000000000000000000000000000000000064;

    // ── pool ─────────────────────────────────────────────────────────────────────────────────

    function poolState(address pool) public view returns (PoolState memory s) {
        s.pool = pool;
        s.blockNumber = _blockNumber();
        s.blockTimestamp = block.timestamp;

        bytes memory slot0;
        (s.kind, slot0) = _kindOf(pool);
        if (s.kind == Kind.UNKNOWN) return _poolNoData(s, "slot0");
        s.sqrtPriceX96 = uint160(_w(slot0, 0));
        s.tick = int24(int256(_w(slot0, 1)));
        s.observationCardinality = uint16(_w(slot0, 3));

        bool ok;
        uint256 v;
        (ok, v) = _read(pool, IPool.token0.selector);
        if (!ok) return _poolNoData(s, "token0");
        s.token0 = address(uint160(v));
        (ok, v) = _read(pool, IPool.token1.selector);
        if (!ok) return _poolNoData(s, "token1");
        s.token1 = address(uint160(v));
        (ok, v) = _read(pool, IPool.fee.selector);
        if (!ok) return _poolNoData(s, "fee");
        s.fee = uint24(v);
        (ok, v) = _read(pool, IPool.tickSpacing.selector);
        if (!ok) return _poolNoData(s, "tickSpacing");
        s.tickSpacing = int24(int256(v));
        (ok, v) = _read(pool, IPool.liquidity.selector);
        if (!ok) return _poolNoData(s, "liquidity");
        s.liquidity = uint128(v);
        (ok, v) = _read(pool, IPool.feeGrowthGlobal0X128.selector);
        if (!ok) return _poolNoData(s, "feeGrowthGlobal0");
        s.feeGrowthGlobal0X128 = v;
        (ok, v) = _read(pool, IPool.feeGrowthGlobal1X128.selector);
        if (!ok) return _poolNoData(s, "feeGrowthGlobal1");
        s.feeGrowthGlobal1X128 = v;
        (ok, v) = _read(pool, IPool.factory.selector);
        if (!ok) return _poolNoData(s, "factory");
        s.factory = address(uint160(v));

        // The pool's own factory must return this pool for its key, under the signature that
        // matches its shape. This is what makes `kind` a fact rather than a guess.
        if (_getPool(s.factory, s.kind, s.token0, s.token1, s.fee, s.tickSpacing) != pool) {
            return _poolNoData(s, "factory_mismatch");
        }

        if (s.kind == Kind.SLIPSTREAM) {
            (ok, v) = _read(pool, ISlipstreamPool.stakedLiquidity.selector);
            if (!ok) return _poolNoData(s, "stakedLiquidity");
            s.stakedLiquidity = uint128(v);
            (ok, v) = _read(pool, ISlipstreamPool.gauge.selector);
            if (!ok) return _poolNoData(s, "gauge");
            s.gauge = address(uint160(v));
        }

        (ok, v) = _read(s.token0, IERC20Decimals.decimals.selector);
        if (!ok || v > type(uint8).max) return _poolNoData(s, "decimals0");
        s.decimals0 = uint8(v);
        (ok, v) = _read(s.token1, IERC20Decimals.decimals.selector);
        if (!ok || v > type(uint8).max) return _poolNoData(s, "decimals1");
        s.decimals1 = uint8(v);

        s.status = Status.OK;
    }

    // ── TWAP ─────────────────────────────────────────────────────────────────────────────────

    /// @notice Arithmetic-mean tick over the last `secondsAgo` seconds, from the pool's oracle.
    /// @dev    If the pool's observation history does not reach back that far, observe() reverts
    ///         and the lens returns NO_DATA. It never returns a zero tick in place of a reading.
    function twap(address pool, uint32 secondsAgo) public view returns (Twap memory t) {
        t.pool = pool;
        t.secondsAgo = secondsAgo;
        t.blockNumber = _blockNumber();
        t.blockTimestamp = block.timestamp;
        if (secondsAgo == 0) return _twapNoData(t, "zero_window");
        if (pool.code.length == 0) return _twapNoData(t, "no_code");

        uint32[] memory ago = new uint32[](2);
        ago[0] = secondsAgo;
        (bool ok, bytes memory r) = pool.staticcall(abi.encodeWithSelector(IPool.observe.selector, ago));
        if (!ok) return _twapNoData(t, "observe_reverted");

        // Expected ABI: (int56[] tickCumulatives, uint160[] secondsPerLiquidityCumulativeX128s), each
        // of length 2. Parsed by hand with bounds checks so malformed data is NO_DATA, not a revert.
        if (r.length < 64) return _twapNoData(t, "observe_short");
        uint256 off = _w(r, 0);
        if (off % 32 != 0 || off + 96 > r.length) return _twapNoData(t, "observe_malformed");
        uint256 i = off / 32;
        if (_w(r, i) != 2) return _twapNoData(t, "observe_malformed");
        int256 past = int256(_w(r, i + 1));
        int256 nowc = int256(_w(r, i + 2));
        if (past != int256(int56(past)) || nowc != int256(int56(nowc))) return _twapNoData(t, "observe_malformed");
        t.tickCumulativePast = int56(past);
        t.tickCumulativeNow = int56(nowc);

        int256 delta = nowc - past;
        int256 mean = delta / int256(uint256(secondsAgo));
        if (delta < 0 && delta % int256(uint256(secondsAgo)) != 0) mean--; // round toward -infinity
        if (mean < TickMath.MIN_TICK || mean > TickMath.MAX_TICK) return _twapNoData(t, "tick_out_of_range");
        t.arithmeticMeanTick = int24(mean);
        t.status = Status.OK;
    }

    // ── positions ────────────────────────────────────────────────────────────────────────────

    function position(address npm, uint256 tokenId) public view returns (PositionState memory p) {
        p.npm = npm;
        p.tokenId = tokenId;
        p.blockNumber = _blockNumber();
        p.blockTimestamp = block.timestamp;
        if (npm.code.length == 0) return _posNoData(p, "npm_no_code");

        // positions(uint256): nonce, operator, token0, token1, fee|tickSpacing, tickLower, tickUpper,
        // liquidity, feeGrowthInside0LastX128, feeGrowthInside1LastX128, tokensOwed0, tokensOwed1
        (bool ok, bytes memory pr) = npm.staticcall(abi.encodeWithSelector(INpm.positions.selector, tokenId));
        if (!ok || pr.length != 12 * 32) return _posNoData(p, "positions");
        p.token0 = address(uint160(_w(pr, 2)));
        p.token1 = address(uint160(_w(pr, 3)));
        p.tickLower = int24(int256(_w(pr, 5)));
        p.tickUpper = int24(int256(_w(pr, 6)));
        p.liquidity = uint128(_w(pr, 7));
        if (p.tickLower >= p.tickUpper || p.tickLower < TickMath.MIN_TICK || p.tickUpper > TickMath.MAX_TICK) {
            return _posNoData(p, "ticks_invalid");
        }

        uint256 v;
        (ok, v) = _readArg(npm, INpm.ownerOf.selector, tokenId);
        if (!ok) return _posNoData(p, "ownerOf");
        p.owner = address(uint160(v));
        (ok, v) = _read(npm, INpm.factory.selector);
        if (!ok) return _posNoData(p, "npm_factory");
        address factory = address(uint160(v));

        // word 4 is a v3 fee or a Slipstream tickSpacing; the factory that answers decides which.
        uint256 w4 = _w(pr, 4);
        p.pool = _getPool(factory, Kind.UNIV3, p.token0, p.token1, uint24(w4), 0);
        if (p.pool != address(0)) {
            p.kind = Kind.UNIV3;
        } else {
            p.pool = _getPool(factory, Kind.SLIPSTREAM, p.token0, p.token1, 0, int24(int256(w4)));
            if (p.pool == address(0)) return _posNoData(p, "pool_lookup");
            p.kind = Kind.SLIPSTREAM;
        }

        (Kind shape, bytes memory slot0) = _kindOf(p.pool);
        if (shape != p.kind) return _posNoData(p, "pool_shape");
        p.sqrtPriceX96 = uint160(_w(slot0, 0));
        p.tick = int24(int256(_w(slot0, 1)));
        p.inRange = p.tickLower <= p.tick && p.tick < p.tickUpper;

        if (p.kind == Kind.SLIPSTREAM) {
            (ok, v) = _read(p.pool, ISlipstreamPool.gauge.selector);
            if (!ok) return _posNoData(p, "gauge");
            p.staked = v != 0 && address(uint160(v)) == p.owner;
        }

        bytes32 why = _fees(p, pr);
        if (why != bytes32(0)) return _posNoData(p, why);
        _amounts(p);
        p.status = Status.OK;
    }

    /// @notice Every position NFT `owner` holds on `npm`. A position staked in a gauge is held by
    ///         the gauge, so it is not listed here under its depositor.
    function positionsOf(address npm, address owner)
        external
        view
        returns (Status status, bytes32 reason, uint256 total, PositionState[] memory positions)
    {
        return positionsOf(npm, owner, 0, type(uint256).max);
    }

    /// @notice Paged form, for owners with more positions than one call's gas allows.
    function positionsOf(address npm, address owner, uint256 offset, uint256 limit)
        public
        view
        returns (Status status, bytes32 reason, uint256 total, PositionState[] memory positions)
    {
        if (npm.code.length == 0) return (Status.NO_DATA, "npm_no_code", 0, positions);
        bool ok;
        (ok, total) = _readArg(npm, INpm.balanceOf.selector, uint256(uint160(owner)));
        if (!ok) return (Status.NO_DATA, "balanceOf", 0, positions);
        uint256 end = offset >= total ? offset : (total - offset < limit ? total : offset + limit);
        positions = new PositionState[](end - offset);
        for (uint256 i = offset; i < end; i++) {
            (bool ok2, bytes memory r) =
                npm.staticcall(abi.encodeWithSelector(INpm.tokenOfOwnerByIndex.selector, owner, i));
            if (!ok2 || r.length < 32) return (Status.NO_DATA, "tokenOfOwnerByIndex", total, positions);
            positions[i - offset] = position(npm, _w(r, 0));
        }
        status = Status.OK;
    }

    // ── HyperCore (HyperEVM only) ────────────────────────────────────────────────────────────

    /// @notice HyperCore perp oracle price for `assetIndex`, via the read precompile. HyperCore
    ///         values are those at the time this EVM block was built. NOT_SUPPORTED off HyperEVM.
    function corePrice(uint32 assetIndex) public view returns (CorePrice memory c) {
        c.assetIndex = assetIndex;
        c.blockNumber = _blockNumber();
        c.blockTimestamp = block.timestamp;
        if (block.chainid != HYPEREVM_CHAIN_ID) {
            c.status = Status.NOT_SUPPORTED;
            c.reason = "not_hyperevm";
            return c;
        }
        (bool ok, bytes memory r) = ORACLE_PX_PRECOMPILE.staticcall{gas: PRECOMPILE_GAS}(abi.encode(assetIndex));
        if (!ok || r.length != 32 || _w(r, 0) > type(uint64).max) return _coreNoData(c, "oraclePx");
        c.oraclePx = uint64(_w(r, 0));

        // perpAssetInfo -> (string coin, uint32 marginTableId, uint8 szDecimals, uint8 maxLeverage, bool onlyIsolated)
        (ok, r) = PERP_ASSET_INFO_PRECOMPILE.staticcall{gas: PRECOMPILE_GAS}(abi.encode(assetIndex));
        if (!ok || r.length < 7 * 32 || _w(r, 0) != 32) return _coreNoData(c, "perpAssetInfo");
        uint256 sz = _w(r, 3);
        if (sz > 6) return _coreNoData(c, "perpAssetInfo");
        c.szDecimals = uint8(sz);
        uint256 strOff = 32 + _w(r, 1); // string offset is relative to the tuple, which starts at byte 32
        if (strOff % 32 != 0 || strOff + 32 > r.length) return _coreNoData(c, "perpAssetInfo");
        uint256 len = _w(r, strOff / 32);
        if (len > 32 || strOff + 32 + len > r.length) return _coreNoData(c, "perpAssetInfo");
        bytes memory coin = new bytes(len);
        for (uint256 k = 0; k < len; k++) {
            coin[k] = r[strOff + 32 + k];
        }
        c.coin = string(coin);
        c.status = Status.OK;
    }

    /// @notice Pool state and a HyperCore oracle price from the same block, so pool-vs-oracle
    ///         deviation is one read. Off HyperEVM the core half is NOT_SUPPORTED.
    function poolStateWithCore(address pool, uint32 assetIndex)
        external
        view
        returns (PoolState memory s, CorePrice memory c)
    {
        s = poolState(pool);
        c = corePrice(assetIndex);
    }

    // ── internals: fees and amounts ──────────────────────────────────────────────────────────

    /// @dev Reproduces NonfungiblePositionManager.collect(max, max) as a view. collect returns
    ///      min(what the NPM thinks is owed, what the pool will actually pay):
    ///        NPM side:  tokensOwed + L_pos * (feeGrowthInside - position checkpoint) / 2^128
    ///                   — skipped on a staked Slipstream position, whose NPM branch only moves
    ///                   the checkpoint (fees on staked liquidity go to the gauge, not the owner)
    ///        pool side: the pool's own position for (npm or gauge, lower, upper), updated the
    ///                   way burn(0) would update it, then capped by its tokensOwed
    ///      All fee-growth arithmetic wraps, as it does in the pool (Solidity 0.7, unchecked).
    function _fees(PositionState memory p, bytes memory pr) private view returns (bytes32) {
        (bool ok, uint256 g0) = _read(p.pool, IPool.feeGrowthGlobal0X128.selector);
        if (!ok) return "feeGrowthGlobal0";
        uint256 g1;
        (ok, g1) = _read(p.pool, IPool.feeGrowthGlobal1X128.selector);
        if (!ok) return "feeGrowthGlobal1";

        uint256 tickWords = p.kind == Kind.UNIV3 ? 8 : 10;
        uint256 outsideAt = p.kind == Kind.UNIV3 ? 2 : 3; // Slipstream inserts stakedLiquidityNet
        bytes memory lo;
        bytes memory hi;
        (ok, lo) = p.pool.staticcall(abi.encodeWithSelector(IPool.ticks.selector, p.tickLower));
        if (!ok || lo.length != tickWords * 32) return "ticks_lower";
        (ok, hi) = p.pool.staticcall(abi.encodeWithSelector(IPool.ticks.selector, p.tickUpper));
        if (!ok || hi.length != tickWords * 32) return "ticks_upper";

        unchecked {
            uint256 below0 = _w(lo, outsideAt);
            uint256 below1 = _w(lo, outsideAt + 1);
            if (p.tick < p.tickLower) (below0, below1) = (g0 - below0, g1 - below1);
            uint256 above0 = _w(hi, outsideAt);
            uint256 above1 = _w(hi, outsideAt + 1);
            if (p.tick >= p.tickUpper) (above0, above1) = (g0 - above0, g1 - above1);
            p.feeGrowthInside0X128 = g0 - below0 - above0;
            p.feeGrowthInside1X128 = g1 - below1 - above1;
        }

        // NPM side
        uint128 npm0 = uint128(_w(pr, 10));
        uint128 npm1 = uint128(_w(pr, 11));
        if (p.liquidity > 0 && !p.staked) {
            unchecked {
                npm0 += uint128(FullMath.mulDiv(p.feeGrowthInside0X128 - _w(pr, 8), p.liquidity, Q128));
                npm1 += uint128(FullMath.mulDiv(p.feeGrowthInside1X128 - _w(pr, 9), p.liquidity, Q128));
            }
        }

        // pool side: pool.positions(keccak256(abi.encodePacked(holder, tickLower, tickUpper)))
        address holder = p.staked ? p.owner : p.npm;
        bytes32 key = keccak256(abi.encodePacked(holder, p.tickLower, p.tickUpper));
        bytes memory pp;
        (ok, pp) = p.pool.staticcall(abi.encodeWithSelector(IPool.positions.selector, key));
        if (!ok || pp.length != 5 * 32) return "pool_positions";
        uint128 pool0 = uint128(_w(pp, 3));
        uint128 pool1 = uint128(_w(pp, 4));
        uint128 poolL = uint128(_w(pp, 0));
        if (p.liquidity > 0 && !p.staked) {
            if (poolL == 0) return "pool_position_empty"; // burn(0) would revert "NP"
            unchecked {
                pool0 += uint128(FullMath.mulDiv(p.feeGrowthInside0X128 - _w(pp, 1), poolL, Q128));
                pool1 += uint128(FullMath.mulDiv(p.feeGrowthInside1X128 - _w(pp, 2), poolL, Q128));
            }
        }

        p.fees0 = npm0 < pool0 ? npm0 : pool0;
        p.fees1 = npm1 < pool1 ? npm1 : pool1;
        return bytes32(0);
    }

    /// @dev Token amounts for p.liquidity at the current price, rounded down exactly as the pool's
    ///      burn rounds them (SqrtPriceMath with roundUp = false, same tick comparisons as the pool).
    function _amounts(PositionState memory p) private pure {
        if (p.liquidity == 0) return;
        uint160 sa = TickMath.getSqrtPriceAtTick(p.tickLower);
        uint160 sb = TickMath.getSqrtPriceAtTick(p.tickUpper);
        if (p.tick < p.tickLower) {
            p.amount0 = SqrtPriceMath.getAmount0Delta(sa, sb, p.liquidity, false);
        } else if (p.tick < p.tickUpper) {
            p.amount0 = SqrtPriceMath.getAmount0Delta(p.sqrtPriceX96, sb, p.liquidity, false);
            p.amount1 = SqrtPriceMath.getAmount1Delta(sa, p.sqrtPriceX96, p.liquidity, false);
        } else {
            p.amount1 = SqrtPriceMath.getAmount1Delta(sa, sb, p.liquidity, false);
        }
    }

    // ── internals: reads ─────────────────────────────────────────────────────────────────────

    /// @dev slot0 shape decides the candidate kind: 7 words UNIV3, 6 words SLIPSTREAM.
    function _kindOf(address pool) private view returns (Kind kind, bytes memory slot0) {
        if (pool.code.length == 0) return (Kind.UNKNOWN, slot0);
        bool ok;
        (ok, slot0) = pool.staticcall(abi.encodeWithSelector(IPool.slot0.selector));
        if (!ok) return (Kind.UNKNOWN, slot0);
        if (slot0.length == 7 * 32) return (Kind.UNIV3, slot0);
        if (slot0.length == 6 * 32) return (Kind.SLIPSTREAM, slot0);
        return (Kind.UNKNOWN, slot0);
    }

    function _getPool(address factory, Kind kind, address t0, address t1, uint24 fee, int24 spacing)
        private
        view
        returns (address)
    {
        bytes memory data = kind == Kind.UNIV3
            ? abi.encodeWithSelector(IUniV3Factory.getPool.selector, t0, t1, fee)
            : abi.encodeWithSelector(ISlipstreamFactory.getPool.selector, t0, t1, spacing);
        if (factory.code.length == 0) return address(0);
        (bool ok, bytes memory r) = factory.staticcall(data);
        if (!ok || r.length != 32) return address(0);
        return address(uint160(_w(r, 0)));
    }

    /// @dev A zero-argument read that must return exactly one word from a contract with code.
    function _read(address target, bytes4 selector) private view returns (bool, uint256) {
        if (target.code.length == 0) return (false, 0);
        (bool ok, bytes memory r) = target.staticcall(abi.encodeWithSelector(selector));
        if (!ok || r.length != 32) return (false, 0);
        return (true, _w(r, 0));
    }

    function _readArg(address target, bytes4 selector, uint256 arg) private view returns (bool, uint256) {
        if (target.code.length == 0) return (false, 0);
        (bool ok, bytes memory r) = target.staticcall(abi.encodeWithSelector(selector, arg));
        if (!ok || r.length != 32) return (false, 0);
        return (true, _w(r, 0));
    }

    /// @dev This chain's own block height: ArbSys.arbBlockNumber() where it answers, else block.number.
    function _blockNumber() private view returns (uint256) {
        if (ARBSYS.code.length > 0) {
            (bool ok, bytes memory r) = ARBSYS.staticcall(abi.encodeWithSelector(IArbSys.arbBlockNumber.selector));
            if (ok && r.length == 32) return _w(r, 0);
        }
        return block.number;
    }

    function _w(bytes memory r, uint256 i) private pure returns (uint256 v) {
        assembly {
            v := mload(add(r, add(32, mul(32, i))))
        }
    }

    // A NO_DATA result keeps its identifying inputs and block, and clears everything read, so a
    // partial read can never be mistaken for data.

    function _poolNoData(PoolState memory s, bytes32 why) private pure returns (PoolState memory o) {
        o.status = Status.NO_DATA;
        o.reason = why;
        o.pool = s.pool;
        o.blockNumber = s.blockNumber;
        o.blockTimestamp = s.blockTimestamp;
    }

    function _posNoData(PositionState memory p, bytes32 why) private pure returns (PositionState memory o) {
        o.status = Status.NO_DATA;
        o.reason = why;
        o.npm = p.npm;
        o.tokenId = p.tokenId;
        o.blockNumber = p.blockNumber;
        o.blockTimestamp = p.blockTimestamp;
    }

    function _twapNoData(Twap memory t, bytes32 why) private pure returns (Twap memory o) {
        o.status = Status.NO_DATA;
        o.reason = why;
        o.pool = t.pool;
        o.secondsAgo = t.secondsAgo;
        o.blockNumber = t.blockNumber;
        o.blockTimestamp = t.blockTimestamp;
    }

    function _coreNoData(CorePrice memory c, bytes32 why) private pure returns (CorePrice memory o) {
        o.status = Status.NO_DATA;
        o.reason = why;
        o.assetIndex = c.assetIndex;
        o.blockNumber = c.blockNumber;
        o.blockTimestamp = c.blockTimestamp;
    }
}

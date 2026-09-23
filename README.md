# baba-lens

**PoolLens v1** is a read-only contract that returns a concentrated-liquidity pool's or position's
full live state **from one block, in one call**. The same bytecode is deployed at the same address
on Base, HyperEVM, Arc and Robinhood Chain.

It holds no keys and no funds, and has no owner, no storage and no upgrade path. It cannot be paid.
Every call it makes outward is a `STATICCALL`. These are properties of the compiled bytes, and
`acceptance/bytecode_audit.py` checks them there, not in the source.

## Deployments

Same bytecode, same address on every chain: **`0xADebbd5825B033bCD4B84FB019AbFC673406d902`**.
Deployed 2026-09-23 through the deterministic deployment proxy
`0x4e59b44847b379578588920cA78FbF26c0B4956C` with salt `keccak256("baba-lens/PoolLens/v1")`.

| chain | chainId | deploy transaction | block |
|---|---|---|---|
| Base | 8453 | `0x970a571f02544e7ea8f1ed52836492c87f4c03ed954f9b97e5335f2ac14797ba` | 51,684,343 |
| Arc | 5042 | `0x5bafd25bf3e13aecee1a76086f7ebd181ce16be395825cb073c71d1fa64aa2af` | 22,330,677 |
| Robinhood Chain | 4663 | `0xce0be0ff03c2e0b918721af772517ef2518df452371d8479f335acab16ca11f6` | 70,427,233 |
| HyperEVM | 999 | `0x29c361def3be0e569b29b84403c0ee824a26ece6c85727bba1ceecaff7bdf749` | 46,666,726 |

On each chain the deployed code is byte-identical to the runtime the acceptance tests ran, checked on
two RPCs per chain with `acceptance/deploy_check.py post`. Source is published on
[Sourcify](https://sourcify.dev) as an exact match on all four chains.

## What it returns

| function | returns |
|---|---|
| `poolState(pool)` | tokens, decimals, fee, tickSpacing, sqrtPriceX96, tick, liquidity, feeGrowthGlobal0/1, observation cardinality; on Slipstream also staked liquidity and gauge |
| `twap(pool, secondsAgo)` | arithmetic-mean tick from the pool's own oracle |
| `position(npm, tokenId)` | owner, pool, ticks, liquidity, inRange, token amounts at the current price, and **uncollected fees equal to what `collect()` would pay the owner at that block** |
| `positionsOf(npm, owner[, offset, limit])` | `position()` for every NFT the owner holds |
| `corePrice(assetIndex)` | HyperEVM only: HyperCore perp oracle price via the read precompile |
| `poolStateWithCore(pool, assetIndex)` | both of the above from the same block |

Every return carries its block number and timestamp, and a `status`:

- `OK`: every field was read.
- `NO_DATA`: something could not be read. `reason` names the read that failed, and the other
  fields are cleared. **A value the lens could not read is never returned as zero.**
- `NOT_SUPPORTED`: the question doesn't apply on this chain (`corePrice` off HyperEVM).

## Venues

Venues are recognised by shape, and each shape is then confirmed by asking the pool's own factory.

| kind | slot0 | ticks() | factory lookup | seen on |
|---|---|---|---|---|
| `UNIV3` | 7 words | 8 words | `getPool(t0, t1, uint24 fee)` | Uniswap v3 (Arc, Robinhood Chain), prjx (HyperEVM) |
| `SLIPSTREAM` | 6 words | 10 words | `getPool(t0, t1, int24 tickSpacing)` | Aerodrome Slipstream (Base), Aero Lite (Arc) |

A contract that fits neither shape, or whose factory doesn't return it, gets `NO_DATA`. Uniswap v4
is not covered in v1.

## Two things the fee maths gets right

1. **The live counter, not the checkpoint.** `positions().feeGrowthInside*LastX128` is the
   position's checkpoint from its last interaction, and it doesn't move until the position is
   touched. The lens rebuilds the live `feeGrowthInside` from the pool (global minus both
   outsides, using the pool's wrapping arithmetic).
2. **What `collect()` actually pays.** `collect()` pays the smaller of two amounts: what the
   position manager thinks is owed, and what the pool will release for its own aggregated position.
   These can differ by a few wei of rounding, and on 2026-09-23 they did on a live Base position
   (`330` vs `332`). On a **staked** Slipstream position the manager adds nothing: fees on staked
   liquidity go to the gauge. The lens reproduces all three cases.

## How it is tested

`acceptance/run.py` runs the exact compiled bytecode against live chain state at a pinned block
`H`, via an `eth_call` state override. Nothing is deployed or signed. Every field is compared with
the position manager's own answer, called from the NFT's owner at `H`:

- fees == `collect(tokenId, owner, max, max)`, to the wei
- amounts == `decreaseLiquidity(tokenId, liquidity, 0, 0, max)`, to the wei
- ticks and liquidity == `positions()`, owner == `ownerOf()`, tick == `slot0()`

Each run uses two RPCs per chain and requires identical output from both. Every RPC is first
checked to execute at the block it is asked for, because some HyperEVM RPCs silently answer at
latest. Every check is re-run against a deliberately wrong expectation and must fail.
`acceptance/mutants.py` goes further: it compiles deliberately broken lenses and requires the live
acceptance to reject each one.

The tests run against any concentrated-liquidity position: list the token IDs in
`acceptance/local_positions.json` (not tracked) and run `python3 acceptance/run.py base` or
`hyperevm`. Our own result files name our positions, so they are held privately rather than
published here.

## Source

Math libraries are vendored
unmodified from Uniswap v4-core (MIT) at commit `46c6834`; see `src/vendor/`.

MIT licence. This is a software tool that reports chain reads.

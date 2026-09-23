#!/usr/bin/env python3
"""Reproduce CC0's ops/lp/measure_prjx_fee_apr.py from lens snapshots at two agreed blocks.

    python3 acceptance/cc0_repro.py BLOCK_A BLOCK_B tokenId [tokenId ...]

CC0's script computes, per position,
    uncollected_t = L * (feeGrowthInside_live - feeGrowthInside_checkpoint) / 2^128     (per token)
from five separate reads at "latest". This prints the same quantity from ONE lens call per block,
plus the lens's own `fees` (which also includes tokensOwed and the pool-side cap, i.e. exactly what
collect() pays), and the change between the two blocks.

The blocks must be agreed with CC0 first (work order), and CC0's script must be run AT those blocks
for the comparison to mean anything: it currently has no block parameter, and its RPC
(rpc.hyperliquid.xyz) executes every call at latest whatever block is asked for.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lensrpc import STATUS, Lens, Rpc  # noqa: E402
from run import Truth  # noqa: E402

NPM = "0xeaD19AE861c29bBb2101E834922B2FEee69B9091"
RPC = "https://hyperliquid.drpc.org"
DECIMALS = {"0x5555555555555555555555555555555555555555": 18,   # WHYPE
            "0xb88339cb7199b77e23db6e890353e22632ba630f": 6,    # USDC
            "0x9fdbda0a5e284c32744d2f17ee5c74b284993463": 8}    # UBTC


def snapshot(lens, truth, tid, block):
    p = lens.call("position(address,uint256)", [NPM, tid], block)[0]
    if STATUS[p["status"]] != "OK":
        return {"status": STATUS[p["status"]], "reason": p["reason"]}
    pos = truth.positions(tid)
    cc0 = [((p["feeGrowthInside%dX128" % k] - pos["fgi%dLast" % k]) % 2 ** 256) * p["liquidity"] // 2 ** 128
           for k in (0, 1)]
    d0, d1 = DECIMALS.get(p["token0"].lower()), DECIMALS.get(p["token1"].lower())
    return {"block": p["blockNumber"], "timestamp": p["blockTimestamp"], "liquidity": p["liquidity"],
            "cc0_formula_raw": cc0, "cc0_formula": [cc0[0] / 10 ** d0, cc0[1] / 10 ** d1] if d0 and d1 else None,
            "tokensOwed": [pos["owed0"], pos["owed1"]], "lens_fees_raw": [p["fees0"], p["fees1"]]}


def main():
    a, b = int(sys.argv[1]), int(sys.argv[2])
    ids = [int(x) for x in sys.argv[3:]]
    if not ids:
        raise SystemExit("usage: cc0_repro.py BLOCK_A BLOCK_B tokenId [tokenId ...]")
    rpc = Rpc(RPC)
    for blk in (a, b):
        got = rpc.executed_height(blk)
        if got != blk:
            raise SystemExit("RPC executed at %d when asked for %d; refusing" % (got, blk))
    lens = Lens(rpc)
    out = {}
    for tid in ids:
        sa = snapshot(lens, Truth(rpc, NPM, a), tid, a)
        sb = snapshot(lens, Truth(rpc, NPM, b), tid, b)
        row = {"A": sa, "B": sb}
        if "cc0_formula_raw" in sa and "cc0_formula_raw" in sb and sa["liquidity"] == sb["liquidity"]:
            row["earned_between_raw"] = [sb["cc0_formula_raw"][k] - sa["cc0_formula_raw"][k] for k in (0, 1)]
            row["seconds_between"] = sb["timestamp"] - sa["timestamp"]
        out[tid] = row
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()

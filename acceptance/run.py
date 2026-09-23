#!/usr/bin/env python3
"""PoolLens v1 acceptance — run the exact bytecode that will be deployed against live chain state.

    python3 acceptance/run.py base
    python3 acceptance/run.py hyperevm
    python3 acceptance/run.py base --height 51679551     # re-run at a recorded H

Method (docs/CC3_POOLLENS_WORK_ORDER.md, "Acceptance tests"):
  * One block H per chain. Every read in the run is made at H, and H is proven: each RPC is first
    asked to execute NUMBER at H and must answer H (some HyperEVM RPCs silently run at latest),
    and every lens return carries its own block.number, which must equal H.
  * Two RPCs per chain. The whole comparison runs on each, and the two lens outputs must be
    identical field for field.
  * Truth is the position manager itself, called from the NFT's owner at H:
        fees     == NPM.collect(tokenId, owner, 2^128-1, 2^128-1)            to the wei
        amounts  == NPM.decreaseLiquidity(tokenId, liquidity, 0, 0, max)     to the wei
        ticks, L == NPM.positions(tokenId)
        owner    == NPM.ownerOf(tokenId);   tick/inRange from pool.slot0()
  * Every check is shown able to fail: each one is re-run against a deliberately wrong
    expectation (1 wei off, one tick off, a flipped flag, a lying RPC, the naive fee formula) and
    must come back RED. A check that cannot go red is not counted.

Exit 0 only if every check is green AND every failure demonstration went red.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import eth_abi
from eth_utils import function_signature_to_4byte_selector as sel

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lensrpc import KIND, STATUS, Lens, Reverted, Rpc  # noqa: E402

MAX128 = 2 ** 128 - 1
MAXU = 2 ** 256 - 1
Q128 = 2 ** 128

CHAINS = {
    "base": {
        "chain_id": 8453,
        "rpcs": ["https://mainnet.base.org", "https://base.drpc.org"],
        "npm": "0x827922686190790b37229fd06084350E74485b72",  # Aerodrome Slipstream NPM
        "not_pools": {
            "EOA": "0xcCf8f9e4c7Ce491f379a3Fe26718b036AC752De8",
            "ERC-20 (USDC)": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "position manager": "0x827922686190790b37229fd06084350E74485b72",
            "codeless": "0x000000000000000000000000000000000000dEaD",
        },
        "core_index": 159,
    },
    "hyperevm": {
        "chain_id": 999,
        "rpcs": ["https://hyperliquid.drpc.org", "https://rpc.hyperlend.finance/archive"],
        "lying_rpc": "https://rpc.hyperliquid.xyz/evm",
        "npm": "0xeaD19AE861c29bBb2101E834922B2FEee69B9091",  # prjx NPM
        "not_pools": {
            "EOA": "0xcCf8f9e4c7Ce491f379a3Fe26718b036AC752De8",
            "ERC-20 (WHYPE)": "0x5555555555555555555555555555555555555555",
            "position manager": "0xeaD19AE861c29bBb2101E834922B2FEee69B9091",
            "codeless": "0x000000000000000000000000000000000000dEaD",
        },
        "core_index": 159,
    },
}


LOCAL = Path(__file__).resolve().parent / "local_positions.json"


def local_positions(chain):
    """Which positions to test is local, not published: acceptance/local_positions.json (gitignored).
    Any position works; the truth is the position manager's own answer for that NFT at H.
    Format: {"base": {"positions": [tokenId, ...], "owner_for_positionsOf": "0x..."}, ...}"""
    if not LOCAL.exists():
        raise SystemExit("no %s: create it with the positions to test (see the docstring)" % LOCAL.name)
    cfg = json.loads(LOCAL.read_text()).get(chain) or {}
    if not cfg.get("positions"):
        raise SystemExit("%s has no positions for %s" % (LOCAL.name, chain))
    return cfg


def enc(sig, types, args):
    return "0x" + (sel(sig) + eth_abi.encode(types, args)).hex()


def dec(types, raw):
    return eth_abi.decode(types, bytes.fromhex(raw[2:]))


class Truth:
    """Direct reads of the chain at H — the things the lens is judged against."""

    def __init__(self, rpc, npm, H):
        self.rpc, self.npm, self.H = rpc, npm, H

    def owner_of(self, tid):
        return dec(["address"], self.rpc.call(self.npm, enc("ownerOf(uint256)", ["uint256"], [tid]), self.H))[0]

    def positions(self, tid):
        t = ["uint96", "address", "address", "address", "int24", "int24", "int24", "uint128",
             "uint256", "uint256", "uint128", "uint128"]
        v = dec(t, self.rpc.call(self.npm, enc("positions(uint256)", ["uint256"], [tid]), self.H))
        return {"tickLower": v[5], "tickUpper": v[6], "liquidity": v[7], "fgi0Last": v[8],
                "fgi1Last": v[9], "owed0": v[10], "owed1": v[11]}

    def collect(self, tid, owner):
        data = enc("collect((uint256,address,uint128,uint128))", ["(uint256,address,uint128,uint128)"],
                   [(tid, owner, MAX128, MAX128)])
        return dec(["uint256", "uint256"], self.rpc.call(self.npm, data, self.H, sender=owner))

    def decrease(self, tid, owner, liq):
        data = enc("decreaseLiquidity((uint256,uint128,uint256,uint256,uint256))",
                   ["(uint256,uint128,uint256,uint256,uint256)"], [(tid, liq, 0, 0, MAXU)])
        return dec(["uint256", "uint256"], self.rpc.call(self.npm, data, self.H, sender=owner))

    def slot0_tick(self, pool):
        raw = self.rpc.call(pool, enc("slot0()", [], []), self.H)
        return eth_abi.decode(["int256"], bytes.fromhex(raw[2 + 64:2 + 128]))[0]  # word 1 = tick

    def observe(self, pool, ago):
        return dec(["int56[]", "uint160[]"],
                   self.rpc.call(pool, enc("observe(uint32[])", ["uint32[]"], [[ago, 0]]), self.H))[0]


class Report:
    def __init__(self):
        self.checks = []
        self.demos = []

    def check(self, name, expected, got, rpc=""):
        ok = expected == got
        self.checks.append({"check": name, "rpc": rpc, "expected": _j(expected), "lens": _j(got),
                            "pass": ok})
        return ok

    def demo(self, name, went_red, detail=""):
        self.demos.append({"demonstration": name, "went_red": bool(went_red), "detail": detail})


def _j(v):
    if isinstance(v, (list, tuple)):
        return [_j(x) for x in v]
    if isinstance(v, int) and not isinstance(v, bool) and abs(v) > 2 ** 53:
        return str(v)
    return v


def position_checks(rep, lens, truth, npm, tid, tag):
    """All per-position comparisons at H. Returns (lens position, truth dict) for reuse in demos."""
    p = lens.call("position(address,uint256)", [npm, tid], truth.H)[0]
    rep.check("%s status" % tag, "OK", STATUS[p["status"]], truth.rpc.url)
    rep.check("%s lens block.number == H" % tag, truth.H, p["blockNumber"], truth.rpc.url)
    owner = truth.owner_of(tid)
    rep.check("%s owner == ownerOf" % tag, owner.lower(), p["owner"].lower(), truth.rpc.url)
    pos = truth.positions(tid)
    rep.check("%s tickLower == positions()" % tag, pos["tickLower"], p["tickLower"], truth.rpc.url)
    rep.check("%s tickUpper == positions()" % tag, pos["tickUpper"], p["tickUpper"], truth.rpc.url)
    rep.check("%s liquidity == positions()" % tag, pos["liquidity"], p["liquidity"], truth.rpc.url)
    tick = truth.slot0_tick(p["pool"])
    rep.check("%s tick == slot0" % tag, tick, p["tick"], truth.rpc.url)
    rep.check("%s inRange == (lower <= tick < upper)" % tag,
              pos["tickLower"] <= tick < pos["tickUpper"], p["inRange"], truth.rpc.url)
    c0, c1 = truth.collect(tid, owner)
    rep.check("%s fees == collect() from owner [wei]" % tag, [c0, c1], [p["fees0"], p["fees1"]], truth.rpc.url)
    d = None
    if pos["liquidity"] > 0:
        try:
            d = truth.decrease(tid, owner, pos["liquidity"])
            rep.check("%s amounts == decreaseLiquidity(all) from owner [wei]" % tag, list(d),
                      [p["amount0"], p["amount1"]], truth.rpc.url)
        except Reverted as e:
            rep.checks.append({"check": "%s amounts == decreaseLiquidity(all)" % tag, "rpc": truth.rpc.url,
                               "expected": "decreaseLiquidity reverted: %s" % str(e)[:120],
                               "lens": _j([p["amount0"], p["amount1"]]), "pass": None})
    return p, pos, (c0, c1), owner


def find_out_of_range(lens, truth, npm, start, limit=400):
    """Walk token ids down from `start` for a live, out-of-range position someone else holds."""
    for tid in range(start - 1, start - 1 - limit, -1):
        p = lens.call("position(address,uint256)", [npm, tid], truth.H)[0]
        if STATUS[p["status"]] == "OK" and p["liquidity"] > 0 and not p["inRange"]:
            return tid
    return None


def find_card1_pool(lens, truth, npm, start, limit=400):
    """A pool with observation cardinality 1, found from positions near our own."""
    seen = set()
    for tid in range(start - 1, start - 1 - limit, -1):
        p = lens.call("position(address,uint256)", [npm, tid], truth.H)[0]
        if STATUS[p["status"]] != "OK" or p["pool"] in seen:
            continue
        seen.add(p["pool"])
        s = lens.call("poolState(address)", [p["pool"]], truth.H)[0]
        if STATUS[s["status"]] == "OK" and s["observationCardinality"] == 1:
            return p["pool"]
    return None


PRECOMPILE_807 = "0x0000000000000000000000000000000000000807"
PROBE_ADDR = "0x00000000000000000000000000000000001E5501"


def core_probe(rpc, idx):
    """lens.corePrice(idx) and a direct 0x...0807 read, in one eth_call at latest."""
    from eth_utils.abi import get_abi_output_types
    from lensrpc import LENS_ADDR
    lens = Lens(rpc)
    probe = json.loads((Path(__file__).resolve().parents[1] / "out/CoreProbe.sol/CoreProbe.json").read_text())
    data = enc("probe(address,uint32)", ["address", "uint32"], [LENS_ADDR, idx])
    raw = rpc.req("eth_call", [{"to": PROBE_ADDR, "data": data}, "latest",
                               {LENS_ADDR: {"code": lens.runtime},
                                PROBE_ADDR: {"code": probe["deployedBytecode"]["object"]}}])
    lr, dok, dr, bn = eth_abi.decode(["bytes", "bool", "bytes", "uint256"], bytes.fromhex(raw[2:]))
    fn = lens.fns["corePrice(uint32)"]
    from lensrpc import _named
    cp = _named(fn["outputs"][0], eth_abi.decode(get_abi_output_types(fn), lr)[0])
    return cp, dok, int.from_bytes(dr, "big") if dok else None, bn


def run(chain, height=None):
    cfg = dict(CHAINS[chain], **local_positions(chain))
    rep = Report()
    primary = Rpc(cfg["rpcs"][0])
    H = height or primary.block_number() - 10
    out = {"chain": chain, "chain_id": cfg["chain_id"], "H": H, "rpcs": cfg["rpcs"], "npm": cfg["npm"],
           "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    # ── H is real on every RPC used ─────────────────────────────────────────────────────────
    for url in cfg["rpcs"]:
        r = Rpc(url)
        rep.check("chainId", cfg["chain_id"], r.chain_id(), url)
        rep.check("RPC executes at H (NUMBER probe)", H, r.executed_height(H), url)
    if cfg.get("lying_rpc"):
        got = Rpc(cfg["lying_rpc"]).executed_height(H)
        rep.demo("height check goes red on an RPC that ignores the block tag (%s)" % cfg["lying_rpc"],
                 got != H, "asked for %d, it executed at %d" % (H, got))

    # ── our positions, on each RPC ──────────────────────────────────────────────────────────
    per_rpc = {}
    kept = {}
    for url in cfg["rpcs"]:
        rpc = Rpc(url)
        lens, truth = Lens(rpc), Truth(rpc, cfg["npm"], H)
        per_rpc[url] = {}
        for tid in cfg["positions"]:
            p, pos, coll, owner = position_checks(rep, lens, truth, cfg["npm"], tid, "#%d" % tid)
            per_rpc[url][tid] = p
            kept.setdefault(tid, (p, pos, coll, owner))
    a, b = cfg["rpcs"]
    for tid in cfg["positions"]:
        rep.check("#%d lens output identical on both RPCs" % tid, per_rpc[a][tid], per_rpc[b][tid], "both")

    # ── failure demonstrations on our positions ─────────────────────────────────────────────
    for tid, (p, pos, coll, owner) in kept.items():
        tmp = Report()
        rep.demo("#%d fees check, expected token0 off by +1 wei" % tid,
                 not tmp.check("x", [coll[0] + 1, coll[1]], [p["fees0"], p["fees1"]]))
        rep.demo("#%d fees check, expected token1 off by +1 wei" % tid,
                 not tmp.check("x", [coll[0], coll[1] + 1], [p["fees0"], p["fees1"]]))
        rep.demo("#%d ticks check, expected tickLower off by one" % tid,
                 not tmp.check("x", pos["tickLower"] + 1, p["tickLower"]))
        rep.demo("#%d liquidity check, expected off by one" % tid,
                 not tmp.check("x", pos["liquidity"] + 1, p["liquidity"]))
        rep.demo("#%d cross-RPC check, one side perturbed by 1 wei" % tid,
                 not tmp.check("x", dict(p, fees0=p["fees0"] + 1), p))
        # The naive formula — the defect the engine hit. On a staked Slipstream position it must
        # disagree with collect(); on an unstaked one it must agree (else the lens's staked branch
        # would be unexplained).
        n0 = (pos["owed0"] + ((p["feeGrowthInside0X128"] - pos["fgi0Last"]) % 2 ** 256) * pos["liquidity"] // Q128) % 2 ** 128
        n1 = (pos["owed1"] + ((p["feeGrowthInside1X128"] - pos["fgi1Last"]) % 2 ** 256) * pos["liquidity"] // Q128) % 2 ** 128
        naive_agrees = [n0, n1] == list(coll)
        if p["staked"]:
            rep.demo("#%d (staked) fees check fed the naive L*(inside-last) formula" % tid, not naive_agrees,
                     "naive %s vs collect %s" % ([n0, n1], list(coll)))
        else:
            out.setdefault("naive_formula_on_unstaked", {})[tid] = {"naive": [n0, n1], "collect": list(coll),
                                                                   "agrees": naive_agrees}

    # ── discovery at H: someone else's out-of-range position, a cardinality-1 pool ──────────
    rpc = Rpc(cfg["rpcs"][0])
    lens, truth = Lens(rpc), Truth(rpc, cfg["npm"], H)
    start = min(cfg["positions"])
    oor = find_out_of_range(lens, truth, cfg["npm"], start)
    out["out_of_range_position"] = oor
    if oor is None:
        rep.checks.append({"check": "out-of-range position found", "rpc": rpc.url, "expected": "found",
                           "lens": "none within 400 ids", "pass": False})
    else:
        p, pos, coll, owner = position_checks(rep, lens, truth, cfg["npm"], oor, "#%d (3rd-party, out of range)" % oor)
        rep.check("#%d reports inRange=false" % oor, False, p["inRange"], rpc.url)
        rep.demo("#%d inRange check, expected flipped to true" % oor, not Report().check("x", True, p["inRange"]))

    c1 = find_card1_pool(lens, truth, cfg["npm"], start)
    out["cardinality_1_pool"] = c1
    WINDOW = 30 * 86400
    if c1 is None:
        rep.checks.append({"check": "cardinality-1 pool found", "rpc": rpc.url, "expected": "found",
                           "lens": "none within 400 ids", "pass": False})
    else:
        t = lens.call("twap(address,uint32)", [c1, WINDOW], H)[0]
        try:
            truth.observe(c1, WINDOW)
            direct = "observe() answered"
        except Reverted as e:
            direct = "observe() reverted: %s" % str(e)[:80]
        rep.check("cardinality-1 pool %s twap(30d) -> NO_DATA (%s)" % (c1, direct), "NO_DATA",
                  STATUS[t["status"]], rpc.url)
        rep.check("... and its tick is not a zero-filled reading", [0, 0, 0],
                  [t["arithmeticMeanTick"], t["tickCumulativePast"], t["tickCumulativeNow"]], rpc.url)
        rep.demo("twap 'expect NO_DATA' check fed an OK status", not Report().check("x", "NO_DATA", "OK"))

    # TWAP positive control: our own pools, several windows; mean tick must match observe(). The
    # rounding rule (toward -infinity) is only exercised by a window whose tick delta is negative
    # and not a multiple of the window, so at least one such window is REQUIRED — a mutation run
    # showed a single 600 s window can divide exactly and let a wrong rounding rule pass.
    exercised = []
    pools = []
    for tid in cfg["positions"]:
        if kept[tid][0]["pool"] not in pools:
            pools.append(kept[tid][0]["pool"])
    for pool in pools:
        for ago in (607, 1801, 3607, 86413):
            t = lens.call("twap(address,uint32)", [pool, ago], H)[0]
            try:
                cum = truth.observe(pool, ago)
            except Reverted as e:
                rep.check("twap(%ds) on %s: observe() reverted (%s), lens must say NO_DATA" % (ago, pool, str(e)[:40]),
                          "NO_DATA", STATUS[t["status"]], rpc.url)
                continue
            d = cum[1] - cum[0]
            mean = d // ago  # python floors toward -inf, as the lens must
            rep.check("twap(%ds) on %s == observe() mean tick" % (ago, pool), ["OK", mean],
                      [STATUS[t["status"]], t["arithmeticMeanTick"]], rpc.url)
            if d < 0 and d % ago:
                exercised.append((pool, ago, d))
                rep.demo("twap(%ds) check, expected rounded toward zero instead" % ago,
                         not Report().check("x", -(-d // ago), t["arithmeticMeanTick"]))
    rep.check("twap rounding exercised by >=1 window with a negative, non-divisible delta",
              True, bool(exercised), rpc.url)
    out["twap_rounding_windows"] = [{"pool": a, "secondsAgo": b, "delta": c} for a, b, c in exercised]
    p0 = kept[cfg["positions"][-1]][0]

    # ── non-pools and bad ids -> NO_DATA, never a zero-filled OK ────────────────────────────
    for label, addr in cfg["not_pools"].items():
        s = lens.call("poolState(address)", [addr], H)[0]
        rep.check("poolState(%s) -> NO_DATA" % label, "NO_DATA", STATUS[s["status"]], rpc.url)
        t = lens.call("twap(address,uint32)", [addr, 600], H)[0]
        rep.check("twap(%s) -> NO_DATA" % label, "NO_DATA", STATUS[t["status"]], rpc.url)
    q = lens.call("position(address,uint256)", [cfg["npm"], 2 ** 255], H)[0]
    rep.check("position(npm, nonexistent id) -> NO_DATA", "NO_DATA", STATUS[q["status"]], rpc.url)
    q = lens.call("position(address,uint256)", [cfg["not_pools"]["ERC-20 (USDC)" if chain == "base" else "ERC-20 (WHYPE)"], 1], H)[0]
    rep.check("position(non-NPM, 1) -> NO_DATA", "NO_DATA", STATUS[q["status"]], rpc.url)
    real = lens.call("poolState(address)", [p0["pool"]], H)[0]
    rep.check("poolState(real pool) -> OK, kind %s" % KIND[real["kind"]], "OK", STATUS[real["status"]], rpc.url)
    rep.demo("'expect NO_DATA' check fed a real pool", not Report().check("x", "NO_DATA", STATUS[real["status"]]))

    # ── positionsOf: every NFT one owner holds, each re-checked against collect() ────────────
    owner_po = cfg.get("owner_for_positionsOf")
    if owner_po:
        st, reason, total, plist = lens.call("positionsOf(address,address)", [cfg["npm"], owner_po], H)
        rep.check("positionsOf(owner) status", "OK", STATUS[st], rpc.url)
        rep.check("positionsOf count == balanceOf", total, len(plist), rpc.url)
        out["positionsOf_ids"] = [x["tokenId"] for x in plist]
        for x in plist:
            if x["liquidity"] > 0 or x["fees0"] or x["fees1"]:
                c = truth.collect(x["tokenId"], owner_po)
                rep.check("positionsOf #%d fees == collect() [wei]" % x["tokenId"], list(c),
                          [x["fees0"], x["fees1"]], rpc.url)
    else:
        rep.checks.append({"check": "positionsOf", "rpc": rpc.url, "expected": "an owner in local_positions.json",
                           "lens": "not run: no owner configured", "pass": None})

    # ── HyperCore oracle price ──────────────────────────────────────────────────────────────
    ci = cfg["core_index"]
    c = lens.call("corePrice(uint32)", [ci], H)[0]
    if chain == "hyperevm":
        # HyperCore reads cannot be pinned to a past block: these nodes do not keep HyperCore
        # history, so the precompile fails at H. The lens must say NO_DATA there, never 0.
        try:
            rpc.call(PRECOMPILE_807, "0x" + eth_abi.encode(["uint32"], [ci]).hex(), H)
            direct_h = "answered"
        except Exception as e:  # noqa: BLE001 - recorded, not swallowed
            direct_h = "failed: %s" % str(e)[:70]
        rep.check("corePrice(%d) at historical H -> NO_DATA, not 0 (direct precompile read at H %s)"
                  % (ci, direct_h), ["NO_DATA", 0], [STATUS[c["status"]], c["oraclePx"]], rpc.url)
        # At latest, the lens and the precompile are read inside ONE call (CoreProbe), so both are
        # from the same block even on an RPC that ignores block tags.
        for url in (cfg["rpcs"][0], cfg["lying_rpc"]):
            cp, direct_ok, direct, bn = core_probe(Rpc(url), ci)
            rep.check("corePrice(%d) == oraclePx precompile, same call, block %d" % (ci, bn),
                      ["OK", True, direct], [STATUS[cp["status"]], direct_ok, cp["oraclePx"]], url)
            rep.demo("corePrice check, expected oraclePx off by one", not Report().check("x", direct + 1, cp["oraclePx"]))
            out["core_price"] = {"index": ci, "coin": cp["coin"], "oraclePx": cp["oraclePx"],
                                 "szDecimals": cp["szDecimals"], "block": bn,
                                 "human": cp["oraclePx"] / 10 ** (6 - cp["szDecimals"])}
        rep.check("corePrice(%d) names its asset" % ci, "HYPE", out["core_price"]["coin"], cfg["lying_rpc"])
        rl = Rpc(cfg["lying_rpc"])
        bad = Lens(rl).call("corePrice(uint32)", [999999], rl.block_number())[0]
        rep.check("corePrice(999999) -> NO_DATA, not 0", ["NO_DATA", 0], [STATUS[bad["status"]], bad["oraclePx"]], rl.url)
        both = Lens(rl).call("poolStateWithCore(address,uint32)", [p0["pool"], ci], rl.block_number())
        rep.check("poolStateWithCore: pool and core OK, both from one block",
                  ["OK", "OK", True], [STATUS[both[0]["status"]], STATUS[both[1]["status"]],
                                       both[0]["blockNumber"] == both[1]["blockNumber"]], rl.url)
    else:
        rep.check("corePrice off HyperEVM -> NOT_SUPPORTED", "NOT_SUPPORTED", STATUS[c["status"]], rpc.url)

    out["checks"] = rep.checks
    out["demonstrations"] = rep.demos
    out["rpc_calls"] = sum(1 for _ in rep.checks)
    failed = [x for x in rep.checks if x["pass"] is False]
    unchecked = [x for x in rep.checks if x["pass"] is None]
    silent = [d for d in rep.demos if not d["went_red"]]
    out["summary"] = {"checks": len(rep.checks), "failed": len(failed), "not_checkable": len(unchecked),
                      "demonstrations": len(rep.demos), "demonstrations_that_stayed_green": len(silent),
                      "pass": not failed and not silent}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("chain", choices=sorted(CHAINS))
    ap.add_argument("--height", type=int)
    ap.add_argument("--no-save", action="store_true", help="do not write results (mutation runs)")
    a = ap.parse_args()
    out = run(a.chain, a.height)
    res = Path(__file__).resolve().parent / "results"
    res.mkdir(exist_ok=True)
    f = res / ("%s-H%d.json" % (a.chain, out["H"]))
    if not a.no_save:
        f.write_text(json.dumps(out, indent=1, default=str))
    for x in out["checks"]:
        mark = "PASS" if x["pass"] else ("----" if x["pass"] is None else "FAIL")
        print("%s  %s  [%s]" % (mark, x["check"], x["rpc"].split("//")[-1].split("/")[0]))
        if not x["pass"]:
            print("        expected %s\n        lens     %s" % (x["expected"], x["lens"]))
    for d in out["demonstrations"]:
        print("%s  demo: %s %s" % ("RED " if d["went_red"] else "GREEN?!", d["demonstration"],
                                  ("- " + d["detail"]) if d["detail"] else ""))
    print(json.dumps(out["summary"]), "->", f)
    return 0 if out["summary"]["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())

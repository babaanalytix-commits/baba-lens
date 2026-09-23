#!/usr/bin/env python3
"""Mutation test: does the live acceptance catch a WRONG lens, not just a wrong expectation?

Each mutant is PoolLens.sol with one deliberate defect. It is compiled with the same pinned
settings and run through acceptance/run.py at a recorded height. A mutant must make the run fail
(exit 1). One that passes has "survived": the acceptance cannot see that defect, and that is
reported rather than hidden.

    python3 acceptance/mutants.py base 51679592
    python3 acceptance/mutants.py hyperevm 46657317
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORGE = str(Path.home() / ".foundry/bin/forge")

MUTANTS = {
    "staked branch removed (every Slipstream position treated as unstaked)": [
        ("p.staked = v != 0 && address(uint160(v)) == p.owner;", "p.staked = false;")],
    "pool-side cap removed (fees = what the NPM thinks is owed)": [
        ("p.fees0 = npm0 < pool0 ? npm0 : pool0;", "p.fees0 = npm0;"),
        ("p.fees1 = npm1 < pool1 ? npm1 : pool1;", "p.fees1 = npm1;")],
    "checkpoint read as a live counter (the engine's 2026-09 defect)": [
        ("p.feeGrowthInside0X128 = g0 - below0 - above0;", "p.feeGrowthInside0X128 = _w(pr, 8);"),
        ("p.feeGrowthInside1X128 = g1 - below1 - above1;", "p.feeGrowthInside1X128 = _w(pr, 9);")],
    "amounts rounded up instead of down": [
        ("SqrtPriceMath.getAmount0Delta(p.sqrtPriceX96, sb, p.liquidity, false)",
         "SqrtPriceMath.getAmount0Delta(p.sqrtPriceX96, sb, p.liquidity, true)"),
        ("SqrtPriceMath.getAmount1Delta(sa, p.sqrtPriceX96, p.liquidity, false)",
         "SqrtPriceMath.getAmount1Delta(sa, p.sqrtPriceX96, p.liquidity, true)")],
    "TWAP rounds toward zero": [
        ("if (delta < 0 && delta % int256(uint256(secondsAgo)) != 0) mean--;", "")],
    "a failed pool read returned as a zero-filled OK": [
        ("o.status = Status.NO_DATA;\n        o.reason = why;\n        o.pool = s.pool;",
         "o.status = Status.OK;\n        o.reason = why;\n        o.pool = s.pool;")],
    "TWAP observe() revert swallowed as tick 0": [
        ('if (!ok) return _twapNoData(t, "observe_reverted");',
         'if (!ok) { t.status = Status.OK; return t; }')],
}


def build(mutations):
    d = Path(tempfile.mkdtemp(prefix="lens-mutant-"))
    shutil.copytree(ROOT / "src", d / "src")
    shutil.copytree(ROOT / "test", d / "test")
    shutil.copy(ROOT / "foundry.toml", d / "foundry.toml")
    (d / "lib").symlink_to(ROOT / "lib")
    f = d / "src" / "PoolLens.sol"
    s = f.read_text()
    for old, new in mutations:
        if s.count(old) != 1:
            raise SystemExit("mutation anchor not unique/absent: %r" % old)
        s = s.replace(old, new)
    f.write_text(s)
    r = subprocess.run([FORGE, "build", "--root", str(d)], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit("mutant failed to compile:\n" + r.stdout[-2000:] + r.stderr[-2000:])
    return d


def main():
    chain, height = sys.argv[1], sys.argv[2]
    rows = []
    for name, muts in MUTANTS.items():
        d = build(muts)
        ft = subprocess.run([FORGE, "test", "--root", str(d)], capture_output=True, text=True)
        offline_killed = ft.returncode != 0
        env = dict(os.environ, LENS_ARTIFACT=str(d / "out/PoolLens.sol/PoolLens.json"))
        r = subprocess.run([sys.executable, str(ROOT / "acceptance/run.py"), chain, "--height", height,
                            "--no-save"], capture_output=True, text=True, env=env)
        failed = [l.strip() for l in r.stdout.splitlines() if l.startswith("FAIL")]
        rows.append({"mutant": name, "killed": r.returncode == 1 and bool(failed),
                     "killed_by_offline_unit_tests": offline_killed,
                     "exit": r.returncode, "first_failing_checks": failed[:4],
                     "stderr_tail": r.stderr.strip().splitlines()[-1:] if r.returncode not in (0, 1) else []})
        shutil.rmtree(d, ignore_errors=True)
        print("%-8s %s  (offline unit tests: %s)" % ("KILLED" if rows[-1]["killed"] else "SURVIVED", name,
                                                   "killed" if offline_killed else "survived"), flush=True)
        for l in failed[:4]:
            print("           " + l, flush=True)
        if rows[-1]["stderr_tail"]:
            print("           crashed:", rows[-1]["stderr_tail"])
    out = ROOT / "acceptance" / "results" / ("mutants-%s-H%s.json" % (chain, height))
    out.write_text(json.dumps(rows, indent=1))
    survived = [r for r in rows if not r["killed"]]
    print("%d/%d mutants killed -> %s" % (len(rows) - len(survived), len(rows), out))
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())

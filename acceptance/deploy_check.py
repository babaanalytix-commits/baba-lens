#!/usr/bin/env python3
"""Gates for the signing sitting. Nothing here signs or sends.

  before signing — check what the WALLET shows, not what this repo says:
    python3 acceptance/deploy_check.py pre  <chain> <to-from-wallet> <calldata-file-or-hex-from-wallet>

  after the transaction lands:
    python3 acceptance/deploy_check.py post <chain>

Lesson carried from the 2026-09-17 ERC-8004 incident: a gate that re-checks its own pinned
constant cannot catch a wrong destination. So `pre` takes the destination and calldata from outside,
compares them in full, not by prefix, prints where they diverge, and checks the destination has the
expected deployer code on that chain before anything else.

`pre` rebuilds nothing. It compares against out/PoolLens.sol/PoolLens.json, so run `forge build` from
the committed source first. The initcode hash printed here must match the hash in docs/DEPLOY.md.
"""
import json
import sys
from pathlib import Path

from eth_utils import keccak, to_checksum_address

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lensrpc import Rpc  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEPLOYER = "0x4e59b44847b379578588920cA78FbF26c0B4956C"  # Arachnid deterministic deployment proxy
DEPLOYER_CODE = ("0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffe03601600081602082"
                 "378035828234f58015156039578182fd5b8082525050506014600cf3")
SALT = "0x" + keccak(text="baba-lens/PoolLens/v1").hex()
CHAINS = {
    "base": (8453, ["https://mainnet.base.org", "https://base.drpc.org"]),
    "hyperevm": (999, ["https://rpc.hyperliquid.xyz/evm", "https://hyperliquid.drpc.org"]),
    "arc": (5042, ["https://rpc.mainnet.arc.io", "https://arc.drpc.org"]),
    "robinhood": (4663, ["https://rpc.mainnet.chain.robinhood.com", "https://robinhood.drpc.org"]),
}


def build():
    a = json.loads((ROOT / "out/PoolLens.sol/PoolLens.json").read_text())
    init = bytes.fromhex(a["bytecode"]["object"][2:])
    runtime = a["deployedBytecode"]["object"].lower()
    addr = to_checksum_address(keccak(b"\xff" + bytes.fromhex(DEPLOYER[2:]) + bytes.fromhex(SALT[2:]) + keccak(init))[12:])
    return init, runtime, addr


def diverge(a, b):
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return min(len(a), len(b)) if len(a) != len(b) else None


def rpc_code(url, addr):
    return Rpc(url).req("eth_getCode", [addr, "latest"]).lower()


def pre(chain, to, data):
    cid, rpcs = CHAINS[chain]
    init, _, addr = build()
    expected = SALT + init.hex()
    ok = True
    for url in rpcs:
        try:
            got_cid = Rpc(url).chain_id()
            code = rpc_code(url, to)
        except Exception as e:  # noqa: BLE001 - an unreachable RPC is reported, not skipped
            print("FAIL  %s unreachable: %s" % (url, str(e)[:80]))
            ok = False
            continue
        print("%s  %s chainId %d (expected %d)" % ("PASS" if got_cid == cid else "FAIL", url, got_cid, cid))
        ok &= got_cid == cid
        print("%s  destination has the deployer's 69-byte code on %s" % ("PASS" if code == DEPLOYER_CODE else "FAIL", url))
        ok &= code == DEPLOYER_CODE
    d = diverge(to.lower(), DEPLOYER.lower())
    print("%s  destination == %s%s" % ("PASS" if d is None else "FAIL", DEPLOYER,
                                        "" if d is None else "  (diverges at character %d: %r)" % (d, to)))
    ok &= d is None
    data = data.strip().lower()
    d = diverge(data, expected.lower())
    print("%s  calldata == salt || initcode (%d bytes)%s" % (
        "PASS" if d is None else "FAIL", len(expected) // 2 - 1,
        "" if d is None else "  (diverges at hex character %d)" % d))
    ok &= d is None
    print("      initcode keccak %s" % ("0x" + keccak(init).hex()))
    print("      lens will be at %s" % addr)
    existing = rpc_code(rpcs[0], addr)
    print("%s  nothing deployed at the lens address yet (%d bytes there)" % (
        "PASS" if existing in ("0x", "0x0") else "NOTE", len(existing) // 2 - 1))
    print("GO" if ok else "STOP — do not sign")
    return 0 if ok else 1


def post(chain):
    _, rpcs = CHAINS[chain]
    _, runtime, addr = build()
    ok = True
    for url in rpcs:
        code = rpc_code(url, addr)
        match = code == runtime
        print("%s  code at %s on %s == the runtime the acceptance ran (%d bytes)" % ("PASS" if match else "FAIL", addr, url, len(code) // 2 - 1))
        ok &= match
    return 0 if ok else 1


def main():
    if len(sys.argv) >= 5 and sys.argv[1] == "pre":
        data = sys.argv[4]
        if not data.startswith("0x") and Path(data).exists():
            data = Path(data).read_text()
        return pre(sys.argv[2], sys.argv[3], data)
    if len(sys.argv) == 3 and sys.argv[1] == "post":
        return post(sys.argv[2])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())

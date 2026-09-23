#!/usr/bin/env python3
"""Cross-check tools/bigblocks/hl.js against the official SDK (hyperliquid-python-sdk @ 2fdb18f).

Needs `pip install git+https://github.com/hyperliquid-dex/hyperliquid-python-sdk@2fdb18f9517675ea03695a0962bd19eece9c83f0`
and node. Uses fixed TEST keys only. Also shows the comparison can fail: a one-byte change to the
page's action (usingBigBlocks true -> false) must produce a different signature.
"""
import json
import subprocess
import sys
from pathlib import Path

import eth_account
import msgpack
from hyperliquid.utils.signing import action_hash, sign_agent, sign_l1_action

HERE = Path(__file__).resolve().parent
js = json.loads(subprocess.run(["node", str(HERE / "test_hl.js")], capture_output=True, text=True, check=True).stdout)
user = eth_account.Account.from_key("0x" + "11" * 32)
agent = eth_account.Account.from_key("0x" + "22" * 32)
nonce = js["nonce"]
ok = True


def check(name, a, b):
    global ok
    good = a == b
    ok &= good
    print("%s  %s" % ("PASS" if good else "FAIL", name))
    if not good:
        print("      page %s\n      sdk  %s" % (a, b))


def norm(sig):
    return {"r": sig["r"].lower() if isinstance(sig["r"], str) else hex(sig["r"]),
            "s": sig["s"].lower() if isinstance(sig["s"], str) else hex(sig["s"]), "v": int(sig["v"])}


def hexint(x):
    return int(x, 16) if isinstance(x, str) else int(x)


check("addresses", [js["user"], js["agent"]], [user.address, agent.address])
for on in (True, False):
    action = {"type": "evmUserModify", "usingBigBlocks": on}
    k = str(on).lower()
    check("msgpack(usingBigBlocks=%s)" % on, js["msgpack_" + k], "0x" + msgpack.packb(action).hex())
    check("action hash (%s)" % on, js["hash_" + k], "0x" + action_hash(action, None, nonce, None).hex())
    sdk = sign_l1_action(agent, action, None, nonce, None, True)
    p = js["l1sig_" + k]
    check("agent L1 signature (%s)" % on, [hexint(p["r"]), hexint(p["s"]), p["v"]],
          [hexint(sdk["r"]), hexint(sdk["s"]), sdk["v"]])

# the SDK's sign_agent forces signatureChainId 0x66eee; sign the page's exact action the same way
from hyperliquid.utils.signing import sign_user_signed_action, user_signed_payload, sign_inner  # noqa: E402
act = {"type": "approveAgent", "hyperliquidChain": "Mainnet", "signatureChainId": "0x3e7",
       "agentAddress": agent.address, "agentName": "lens valid_until 1790003600000", "nonce": nonce}
types = [{"name": "hyperliquidChain", "type": "string"}, {"name": "agentAddress", "type": "address"},
         {"name": "agentName", "type": "string"}, {"name": "nonce", "type": "uint64"}]
sdk = sign_inner(user, user_signed_payload("HyperliquidTransaction:ApproveAgent", types, act))
p = js["approve_sig"]
check("wallet approveAgent signature (signatureChainId 0x3e7)", [hexint(p["r"]), hexint(p["s"]), p["v"]],
      [hexint(sdk["r"]), hexint(sdk["s"]), sdk["v"]])

# the comparison can fail
wrong = sign_l1_action(agent, {"type": "evmUserModify", "usingBigBlocks": False}, None, nonce, None, True)
p = js["l1sig_true"]
red = [hexint(p["r"]), hexint(p["s"])] != [hexint(wrong["r"]), hexint(wrong["s"])]
print("%s  demo: page's 'true' signature vs SDK 'false' signature differ" % ("RED " if red else "GREEN?!"))
ok &= red
print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)

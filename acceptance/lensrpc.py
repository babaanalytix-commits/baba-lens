"""Minimal JSON-RPC client for running PoolLens against live chains at a pinned block.

The lens is not deployed anywhere yet. Its compiled runtime bytecode is placed at LENS_ADDR with an
eth_call state override, so the exact bytes that will be deployed run against real chain state at
block H. Nothing is sent, signed or written.

Failures are classified, never collapsed:
  Reverted    - the node executed the call and it reverted. A real answer; never retried.
  RateLimited - HTTP 429 / provider rate-limit text. Retried with backoff on the same RPC.
  RpcFail     - anything else (transport, malformed reply). Retried, then raised.
"""
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import eth_abi
from eth_utils.abi import get_abi_output_types

LENS_ADDR = "0x00000000000000000000000000000000001E5500"
ROOT = Path(__file__).resolve().parents[1]
# LENS_ARTIFACT lets acceptance/mutants.py run a deliberately broken build through the same checks.
ARTIFACT = Path(os.environ.get("LENS_ARTIFACT") or ROOT / "out" / "PoolLens.sol" / "PoolLens.json")


class Reverted(Exception):
    pass


class RateLimited(Exception):
    pass


class RpcFail(Exception):
    pass


def artifact():
    d = json.loads(ARTIFACT.read_text())
    return d["abi"], d["deployedBytecode"]["object"], d["bytecode"]["object"]


class Rpc:
    def __init__(self, url, tries=10):
        self.url = url
        self.tries = tries
        self.calls = 0

    def _post(self, method, params):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.url, data=body, headers={
            "content-type": "application/json", "user-agent": "baba-lens-acceptance/1"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise RateLimited("HTTP 429")
            raise RpcFail("HTTP %s" % e.code)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise RpcFail(str(e))
        self.calls += 1
        if "error" in d:
            err = d["error"]
            msg = (err.get("message") or "") if isinstance(err, dict) else str(err)
            low = msg.lower()
            if "rate" in low and "limit" in low or "too many" in low or "429" in low:
                raise RateLimited(msg)
            if "revert" in low or (isinstance(err, dict) and err.get("code") == 3):
                raise Reverted(msg)
            raise RpcFail(msg)
        return d["result"]

    def req(self, method, params):
        last = None
        for i in range(self.tries):
            try:
                return self._post(method, params)
            except Reverted:
                raise
            except (RateLimited, RpcFail) as e:
                last = e
                time.sleep(min(2 ** i, 30))
        raise last

    def block_number(self):
        return int(self.req("eth_blockNumber", []), 16)

    def chain_id(self):
        return int(self.req("eth_chainId", []), 16)

    def call(self, to, data, block, sender=None, overrides=None):
        tx = {"to": to, "data": data}
        if sender:
            tx["from"] = sender
        params = [tx, hex(block)]
        if overrides:
            params.append(overrides)
        return self.req("eth_call", params)

    def executed_height(self, block):
        """Run NUMBER at `block` and return the height the node actually executed at. Some RPCs
        (rpc.hyperliquid.xyz, rpc.hypurrscan.io) ignore the block tag and run at latest."""
        probe = "0x000000000000000000000000000000000000bEEF"
        r = self.call(probe, "0x", block, overrides={probe: {"code": "0x4360005260206000f3"}})
        return int(r, 16)


class Lens:
    """Encode/decode PoolLens calls from the compiled ABI and run them via state override."""

    def __init__(self, rpc):
        self.rpc = rpc
        self.abi, self.runtime, self.initcode = artifact()
        self.fns = {}
        for f in self.abi:
            if f.get("type") == "function":
                sig = "%s(%s)" % (f["name"], ",".join(i["type"] for i in f["inputs"]))
                self.fns[sig] = f

    def _encode(self, sig, args):
        from eth_utils import function_signature_to_4byte_selector
        f = self.fns[sig]
        types = [_abi_type(i) for i in f["inputs"]]
        return "0x" + (function_signature_to_4byte_selector(sig) + eth_abi.encode(types, args)).hex()

    def call(self, sig, args, block):
        f = self.fns[sig]
        raw = self.rpc.call(LENS_ADDR, self._encode(sig, args), block,
                            overrides={LENS_ADDR: {"code": self.runtime}})
        vals = eth_abi.decode(get_abi_output_types(f), bytes.fromhex(raw[2:]))
        return [_named(o, v) for o, v in zip(f["outputs"], vals)]


def _abi_type(i):
    t = i["type"]
    if t.startswith("tuple"):
        return "(" + ",".join(_abi_type(c) for c in i["components"]) + ")" + t[len("tuple"):]
    return t


def _named(o, v):
    t = o["type"]
    if t == "tuple":
        return {c["name"]: _named(c, x) for c, x in zip(o["components"], v)}
    if t.startswith("tuple["):
        inner = dict(o, type="tuple")
        return [_named(inner, x) for x in v]
    if t == "bytes32":
        return v.rstrip(b"\0").decode("ascii", "replace")
    if isinstance(v, bytes):
        return "0x" + v.hex()
    return v


STATUS = {0: "OK", 1: "NO_DATA", 2: "NOT_SUPPORTED"}
KIND = {0: "UNKNOWN", 1: "UNIV3", 2: "SLIPSTREAM"}

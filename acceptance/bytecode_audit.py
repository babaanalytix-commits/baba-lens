#!/usr/bin/env python3
"""Prove the work order's hard rules from the bytes that will be deployed, not from the source.

    no storage writes, no delegatecall, only staticcall outward, not payable, no owner, no upgrade

The runtime bytecode is disassembled (PUSH immediates skipped, the trailing CBOR metadata cut off
by its own length suffix) and must contain none of the opcodes that write state, move value or
run foreign code in our context. The ABI must hold only view/pure functions, no fallback, no
receive, no payable constructor.

The audit is run first against small bytecodes that DO contain each forbidden opcode, and each of
those must be caught — an audit that cannot fail is not evidence.

    python3 acceptance/bytecode_audit.py            # exit 0 only if the lens is clean AND every seeded violation was caught
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN = {
    0x55: "SSTORE", 0x5D: "TSTORE",
    0xF0: "CREATE", 0xF1: "CALL", 0xF2: "CALLCODE", 0xF4: "DELEGATECALL", 0xF5: "CREATE2",
    0xFF: "SELFDESTRUCT",
    0xA0: "LOG0", 0xA1: "LOG1", 0xA2: "LOG2", 0xA3: "LOG3", 0xA4: "LOG4",
}


def strip_metadata(code: bytes) -> bytes:
    """Solidity appends CBOR metadata followed by its 2-byte big-endian length."""
    if len(code) < 2:
        return code
    n = int.from_bytes(code[-2:], "big")
    if 0 < n <= len(code) - 2 and code[-2 - n] in (0xA1, 0xA2, 0xA3):  # CBOR map header
        return code[:-2 - n]
    return code


def opcodes(code: bytes):
    i = 0
    while i < len(code):
        op = code[i]
        yield i, op
        i += 1 + (op - 0x5F if 0x60 <= op <= 0x7F else 0)  # skip PUSH1..PUSH32 immediates


def audit_code(code: bytes):
    found = {}
    for pc, op in opcodes(strip_metadata(code)):
        if op in FORBIDDEN:
            found.setdefault(FORBIDDEN[op], []).append(pc)
    return found


def audit_abi(abi):
    bad = []
    for e in abi:
        t = e.get("type")
        if t in ("fallback", "receive"):
            bad.append(t)
        if t == "constructor" and e.get("stateMutability") == "payable":
            bad.append("payable constructor")
        if t == "function" and e.get("stateMutability") not in ("view", "pure"):
            bad.append("%s is %s" % (e["name"], e.get("stateMutability")))
    return bad


def main():
    ok = True
    # 1. seeded violations: every forbidden opcode, alone, must be caught
    for op, name in FORBIDDEN.items():
        seeded = bytes([0x60, 0x00, 0x60, 0x00, op, 0x00])  # PUSH1 0 PUSH1 0 <op> STOP
        caught = name in audit_code(seeded)
        print("%s  seeded %s is caught" % ("RED " if caught else "MISS", name))
        ok &= caught
    # ...and an immediate that merely CONTAINS the byte must not be (PUSH1 0x55 is data, not SSTORE)
    fp = audit_code(bytes([0x60, 0x55, 0x60, 0xF4, 0x00]))
    print("%s  PUSH immediates 0x55/0xF4 are not mistaken for opcodes" % ("ok  " if not fp else "FAIL"))
    ok &= not fp
    bad_abi = audit_abi([{"type": "function", "name": "pay", "stateMutability": "payable"}, {"type": "receive"}])
    print("%s  seeded payable function + receive are caught: %s" % ("RED " if len(bad_abi) == 2 else "MISS", bad_abi))
    ok &= len(bad_abi) == 2

    # 2. the lens itself
    art = json.loads((ROOT / "out/PoolLens.sol/PoolLens.json").read_text())
    runtime = bytes.fromhex(art["deployedBytecode"]["object"][2:])
    found = audit_code(runtime)
    ops = {}
    for _, op in opcodes(strip_metadata(runtime)):
        ops[op] = ops.get(op, 0) + 1
    print("lens runtime %d bytes (%d after metadata); STATICCALL x%d, SLOAD x%d" % (
        len(runtime), len(strip_metadata(runtime)), ops.get(0xFA, 0), ops.get(0x54, 0)))
    print("%s  lens has no forbidden opcode%s" % ("PASS" if not found else "FAIL", "" if not found else ": %s" % found))
    ok &= not found
    bad = audit_abi(art["abi"])
    print("%s  lens ABI is view/pure only, no fallback/receive/payable%s" % ("PASS" if not bad else "FAIL",
                                                                          "" if not bad else ": %s" % bad))
    ok &= not bad
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

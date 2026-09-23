// Hyperliquid signing payloads for the big-blocks toggle. No keys here: pure functions that build
// what gets signed. Shared by index.html (browser) and test_hl.js (node), which checks every byte
// against the official Python SDK (hyperliquid-python-sdk @ 2fdb18f, hyperliquid/utils/signing.py).
(function (root) {
  "use strict";

  // msgpack of {"type": "evmUserModify", "usingBigBlocks": <bool>}, key order as the SDK sends it.
  // fixmap(2) | fixstr "type" | fixstr "evmUserModify" | fixstr "usingBigBlocks" | true/false
  function msgpackEvmUserModify(on) {
    const enc = (s) => Array.from(new TextEncoder().encode(s));
    const str = (s) => [0xa0 | s.length].concat(enc(s));
    return new Uint8Array([0x82].concat(str("type"), str("evmUserModify"), str("usingBigBlocks"), [on ? 0xc3 : 0xc2]));
  }

  // action_hash(action, vault_address=None, nonce, expires_after=None):
  //   keccak(msgpack(action) || nonce as 8 bytes big-endian || 0x00)
  function actionHash(ethers, on, nonce) {
    const packed = msgpackEvmUserModify(on);
    const n = new Uint8Array(8);
    let v = BigInt(nonce);
    for (let i = 7; i >= 0; i--) { n[i] = Number(v & 0xffn); v >>= 8n; }
    return ethers.keccak256(ethers.concat([packed, n, new Uint8Array([0])]));
  }

  // L1 action, signed by the (temporary) agent. Mainnet phantom agent source is "a".
  function l1TypedData(ethers, on, nonce) {
    return {
      domain: { chainId: 1337, name: "Exchange", verifyingContract: "0x0000000000000000000000000000000000000000", version: "1" },
      types: { Agent: [{ name: "source", type: "string" }, { name: "connectionId", type: "bytes32" }] },
      message: { source: "a", connectionId: actionHash(ethers, on, nonce) },
    };
  }

  // User-signed ApproveAgent, signed by the wallet. signatureChainId is the chain the wallet is on
  // when it signs (the SDK: "can be any chain"); hyperliquidChain pins it to Mainnet.
  function approveAgentAction(agentAddress, agentName, nonce, signatureChainIdHex) {
    return { type: "approveAgent", hyperliquidChain: "Mainnet", signatureChainId: signatureChainIdHex,
             agentAddress: agentAddress, agentName: agentName, nonce: nonce };
  }

  function approveAgentTypedData(action) {
    return {
      domain: { name: "HyperliquidSignTransaction", version: "1", chainId: parseInt(action.signatureChainId, 16),
                verifyingContract: "0x0000000000000000000000000000000000000000" },
      types: { "HyperliquidTransaction:ApproveAgent": [
        { name: "hyperliquidChain", type: "string" }, { name: "agentAddress", type: "address" },
        { name: "agentName", type: "string" }, { name: "nonce", type: "uint64" }] },
      primaryType: "HyperliquidTransaction:ApproveAgent",
      message: { hyperliquidChain: action.hyperliquidChain, agentAddress: action.agentAddress,
                 agentName: action.agentName, nonce: action.nonce },
    };
  }

  function splitSig(ethers, sig) {
    const s = ethers.Signature.from(sig);
    return { r: s.r, s: s.s, v: s.v };
  }

  root.HL = { msgpackEvmUserModify, actionHash, l1TypedData, approveAgentAction, approveAgentTypedData, splitSig };
  if (typeof module !== "undefined") module.exports = root.HL;
})(typeof window !== "undefined" ? window : globalThis);

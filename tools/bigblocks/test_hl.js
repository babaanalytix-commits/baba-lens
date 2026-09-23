// node tools/bigblocks/test_hl.js  -> prints the page's payload bytes and signatures for fixed TEST
// keys as JSON. tools/bigblocks/test_hl.py produces the same with the official SDK and compares.
const ethers = require("./vendor/ethers.umd.min.js");
const HL = require("./hl.js");
(async () => {
  const user = new ethers.Wallet("0x" + "11".repeat(32));   // test keys, never funded
  const agent = new ethers.Wallet("0x" + "22".repeat(32));
  const nonce = 1790000000000;
  const out = { user: user.address, agent: agent.address, nonce };
  for (const on of [true, false]) {
    const td = HL.l1TypedData(ethers, on, nonce);
    out["msgpack_" + on] = ethers.hexlify(HL.msgpackEvmUserModify(on));
    out["hash_" + on] = td.message.connectionId;
    out["l1sig_" + on] = HL.splitSig(ethers, await agent.signTypedData(td.domain, td.types, td.message));
  }
  const act = HL.approveAgentAction(agent.address, "lens valid_until 1790003600000", nonce, "0x3e7");
  const atd = HL.approveAgentTypedData(act);
  out.approve_sig = HL.splitSig(ethers, await user.signTypedData(atd.domain, atd.types, atd.message));
  console.log(JSON.stringify(out));
})();

# PoolLens v1 — deployment record

**Deployed 2026-09-23 on all four chains; see the README for the transactions.** What follows is the
proposal as it was signed. Yomi signs all four chains from the builder wallet
`0xcCf8f9e4c7Ce491f379a3Fe26718b036AC752De8`, on the iMac, in one sitting (CANON §6c).

## What gets sent — identical on every chain

| | |
|---|---|
| to | `0x4e59b44847b379578588920cA78FbF26c0B4956C`, the Arachnid deterministic deployment proxy |
| value | 0 |
| data | `salt ‖ initcode`, 14,184 bytes. Rebuild it with `forge build` from this commit; it is not stored in the repo. |
| salt | `keccak256("baba-lens/PoolLens/v1")` = `0x27bdeab598645b4f0d0f166e7fbabf7a1a8679312ca89fe5431af7e38d242d2f` |
| initcode keccak | `0x5598cd26a71ac46bac31dd7010546dac8ba226fd642528aab25dbc7f340f9572` |
| **lens address** | **`0xADebbd5825B033bCD4B84FB019AbFC673406d902`**, the same on all four chains |

The address follows from the exact initcode. **Any change to the source, even a comment, or to
the compiler settings in `foundry.toml`, changes it.** Rebuild and re-run `deploy_check.py pre`
at the sitting; don't reuse numbers from this page if the commit has moved.

### How the deployer was confirmed on each chain (2026-09-23)

`eth_getCode(0x4e59…956C)` returned the exact 69-byte proxy runtime on two RPCs per chain:

| chain | chainId | RPC 1 | RPC 2 |
|---|---|---|---|
| Base | 8453 | mainnet.base.org ✓ | base.drpc.org ✓ |
| HyperEVM | 999 | rpc.hyperliquid.xyz/evm ✓ | hyperliquid.drpc.org ✓ |
| Arc | 5042 | rpc.mainnet.arc.io ✓ | arc.drpc.org ✓ |
| Robinhood Chain | **4663** | rpc.mainnet.chain.robinhood.com ✓ | robinhood.drpc.org ✓ |

Robinhood Chain's chainId was read from both RPCs, as the order requires; neither value comes
from a blog post. The lens address has no code on any of the four chains.

## Gas and funding (estimated 2026-09-23 with `eth_estimateGas` from the builder wallet)

| chain | gas | gas price | ≈ cost | builder balance | action before the sitting |
|---|---|---|---|---|---|
| Base | 3,163,764 | 0.006 gwei | 0.0000190 ETH L2 + ≤ 0.00000038 ETH L1 data | **0** | **send 0.0005 ETH** (minimum 0.0001) |
| HyperEVM | 3,163,764 | 0.10 gwei (`eth_bigBlockGasPrice`) | ≈ 0.00032 HYPE | 0.00437 HYPE | none; switch to big blocks first (below) |
| Arc | 3,163,764 | 20.1 gwei | ≈ 0.064 USDC | 200.54 USDC | none |
| Robinhood Chain | 3,222,164 | 0.053 gwei | ≈ 0.00017 ETH | **0** | **send 0.002 ETH** (minimum 0.0005) |

## HyperEVM: the deploy does not fit a small block

- Hyperliquid docs, *HyperEVM › Dual-block architecture* (read 2026-09-23): "Fast block duration is
  set to 1 seconds with a 3M gas limit. Slow block duration is set to 1 minute with a 30M gas
  limit." The live chain agrees: `gasLimit` was 3,000,000 on 10 of 10 recent blocks.
- The deploy needs **3,163,764** gas, which is above 3M. Compiler settings alone got it no lower
  than about 3.09M (runtime 13,751 bytes with `optimizer_runs=1`, no metadata hash). Going further
  would mean dropping functions the order requires.
- The same docs say big blocks are reached by the action
  `{"type": "evmUserModify", "usingBigBlocks": true}`, which "requires an existing Core user to send",
  and that an address becomes a Core user "by receiving a Core asset such as USDC".
- The builder wallet became a Core user on 2026-09-23 by receiving USDC on HyperCore.

**A path that keeps the key in the wallet: `tools/bigblocks/` (Deploy Desk).** A local page. The wallet, browser or hardware,
signs everything, and no private key is typed, pasted or exported.
1. `python3 -m http.server 8799 --bind 127.0.0.1` from the repo root, then open
   `http://127.0.0.1:8799/tools/bigblocks/` in the browser that has the wallet. Connect the builder
   wallet on HyperEVM (chain 999).
2. **Temporary agent.** The page generates a random agent key in memory. The wallet signs
   `ApproveAgent` (typed data it shows field by field: agent address, name `lensdeploy valid_until <now+60 min>`,
   nonce, "Mainnet"). The agent expires by itself; Hyperliquid's docs allow `valid_until` up to 180 days.
3. **Big blocks on.** The agent signs `evmUserModify {usingBigBlocks: true}`. The page confirms the
   switch by reading `eth_usingBigBlocks` from the chain, not from the API's reply.
4. **Deploy.** Run `deploy_check.py pre` on what the wallet shows, then confirm on GO. The deploy lands in
   the next big block, within about a minute.
5. **Big blocks off**, then close the tab. The agent key goes with it and expires anyway.

The page's payloads are byte-identical to the official Python SDK's for both switch directions and for
`ApproveAgent` (`tools/bigblocks/test_hl.py`, fixed test keys; it goes red on a one-byte change). Signed
requests from throwaway keys were rejected by Hyperliquid's live `/exchange` with the signer address
the server recovered, and it matched the throwaway address, so the server hashes these requests exactly
as the page does. The page was not run with a real wallet here, so the first real run is the sitting.
`ethers` 6.17.0 is vendored from the npm tarball after an integrity check against the registry's
sha512 (`tools/bigblocks/vendor/ETHERS_PROVENANCE.txt`).

The third-party site `hyperevm-block-toggle.vercel.app` uses the same approve-agent pattern. Here the
code that generates and holds the agent key is ours and local, instead of a third-party bundle.

## At the sitting, per chain

```bash
forge build
python3 acceptance/deploy_check.py pre <base|hyperevm|arc|robinhood> <TO-AS-THE-WALLET-SHOWS-IT> <CALLDATA-AS-THE-WALLET-SHOWS-IT>
```

Sign only on `GO`. The check compares the destination and calldata the wallet displays, in full,
against the build. It also confirms the destination holds the deployer's code on that chain, on
two RPCs. On 2026-09-23 it said STOP for the codeless `0x8004a169…` address from the ERC-8004
incident, for a destination one character off, and for calldata one byte off.

After each transaction:

```bash
python3 acceptance/deploy_check.py post <chain>   # deployed code == the runtime the acceptance ran, on two RPCs
```

## After deployment

- Publish source on each chain's explorer (`forge verify-contract`, which needs an explorer API key
  per chain). Product copy says **"source published"** and nothing stronger (CANON §6d).
- Re-run `acceptance/run.py base` and `acceptance/run.py hyperevm` against the deployed address.
- Arc and Robinhood Chain need their own acceptance runs against positions there; none exist yet.
- **Robinhood Chain has only one RPC that serves pinned history today.** `robinhood.drpc.org`
  answers "Unknown state" even two blocks back, so a two-RPC pinned run there needs a second RPC
  (for example Alchemy's Robinhood endpoint, which needs a key).

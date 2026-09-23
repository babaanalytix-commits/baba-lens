// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

/// Acceptance-only probe, never deployed. Reads the lens and the oracle precompile in ONE call, so
/// both answers come from the same block even on an RPC that can only execute at latest.
contract CoreProbe {
    function probe(address lens, uint32 idx)
        external
        view
        returns (bytes memory lensRet, bool directOk, bytes memory directRet, uint256 blockNumber)
    {
        (, lensRet) = lens.staticcall(abi.encodeWithSignature("corePrice(uint32)", idx));
        (directOk, directRet) = address(0x0000000000000000000000000000000000000807).staticcall{gas: 200_000}(abi.encode(idx));
        blockNumber = block.number;
    }
}

// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {Script} from "forge-std/Script.sol";
import {AgentLanceMarket} from "../src/AgentLanceMarket.sol";

/// @notice Use only after independent registry/RPC/key qualification and explicit deployment authorization.
/// @dev Supply the broadcaster through Foundry's encrypted keystore (--account); never a key argument.
contract DeployMarket is Script {
    function run() external returns (AgentLanceMarket market) {
        address registry = vm.envAddress("AGENTLANCE_L3_IDENTITY_REGISTRY");
        address validator = vm.envAddress("AGENTLANCE_L3_VALIDATOR");
        bytes32 codeHash = vm.envBytes32("AGENTLANCE_L3_IDENTITY_CODE_HASH");
        require(block.chainid == 10143, "Monad testnet only");
        require(registry.code.length != 0 && registry.codehash == codeHash, "Registry code changed");
        require(validator != address(0), "Validator required");
        vm.startBroadcast();
        market = new AgentLanceMarket(registry, validator);
        vm.stopBroadcast();
    }
}

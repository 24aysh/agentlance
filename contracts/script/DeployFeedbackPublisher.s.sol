// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {Script} from "forge-std/Script.sol";
import {FeedbackPublisher} from "../src/FeedbackPublisher.sol";

/// @notice Explicit operator deployment; signing is supplied by Foundry's keystore CLI.
contract DeployFeedbackPublisher is Script {
    function run() external returns (FeedbackPublisher publisher) {
        require(block.chainid == 10143 || block.chainid == 31337, "Unreviewed network");
        address market = vm.envAddress("AGENTLANCE_MARKET");
        address identity = vm.envAddress("AGENTLANCE_IDENTITY_REGISTRY");
        address reputation = vm.envAddress("AGENTLANCE_REPUTATION_REGISTRY");
        vm.startBroadcast();
        publisher = new FeedbackPublisher(market, identity, reputation);
        vm.stopBroadcast();
    }
}

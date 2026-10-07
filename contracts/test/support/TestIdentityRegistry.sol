// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

/// @dev Explicit local fixture, not an ERC-8004 deployment or a wallet-control verifier.
contract TestIdentityRegistry {
    mapping(uint256 => address) private owners;
    mapping(uint256 => address) private wallets;
    bool private unavailable;

    function setIdentity(uint256 agentId, address owner, address wallet) external {
        owners[agentId] = owner;
        wallets[agentId] = wallet;
    }

    function setUnavailable(bool value) external {
        unavailable = value;
    }

    function ownerOf(uint256 agentId) external view returns (address) {
        require(!unavailable && owners[agentId] != address(0));
        return owners[agentId];
    }

    function getAgentWallet(uint256 agentId) external view returns (address) {
        require(!unavailable && owners[agentId] != address(0));
        return wallets[agentId];
    }

    function transferIdentity(uint256 agentId, address newOwner) external {
        require(msg.sender == owners[agentId] && newOwner != address(0));
        owners[agentId] = newOwner;
        delete wallets[agentId];
    }

    function setWallet(uint256 agentId, address wallet) external {
        require(msg.sender == owners[agentId]);
        wallets[agentId] = wallet;
    }
}

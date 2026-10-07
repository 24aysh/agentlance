// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

/// @dev Explicit local fixture, not an ERC-8004 deployment or a wallet-control verifier.
contract TestIdentityRegistry {
    mapping(uint256 => address) private owners;
    mapping(uint256 => address) private wallets;
    mapping(uint256 => string) private uris;
    bool private unavailable;

    event Registered(uint256 indexed agentId, string agentURI, address indexed owner);
    event URIUpdated(uint256 indexed agentId, string newURI, address indexed updatedBy);
    event MetadataSet(
        uint256 indexed agentId,
        string indexed indexedMetadataKey,
        string metadataKey,
        bytes metadataValue
    );
    event Transfer(address indexed from, address indexed to, uint256 indexed tokenId);

    function setIdentity(uint256 agentId, address owner, address wallet) external {
        owners[agentId] = owner;
        wallets[agentId] = wallet;
        emit Registered(agentId, uris[agentId], owner);
        emit MetadataSet(agentId, "agentWallet", "agentWallet", abi.encodePacked(wallet));
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

    function tokenURI(uint256 agentId) external view returns (string memory) {
        require(!unavailable && owners[agentId] != address(0));
        return uris[agentId];
    }

    function setURI(uint256 agentId, string calldata uri) external {
        require(msg.sender == owners[agentId]);
        uris[agentId] = uri;
        emit URIUpdated(agentId, uri, msg.sender);
    }

    function transferIdentity(uint256 agentId, address newOwner) external {
        require(msg.sender == owners[agentId] && newOwner != address(0));
        owners[agentId] = newOwner;
        delete wallets[agentId];
        emit Transfer(msg.sender, newOwner, agentId);
    }

    function setWallet(uint256 agentId, address wallet) external {
        require(msg.sender == owners[agentId]);
        wallets[agentId] = wallet;
        emit MetadataSet(agentId, "agentWallet", "agentWallet", abi.encodePacked(wallet));
    }
}

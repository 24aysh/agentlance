// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {IAgentLanceMarket} from "../../src/IAgentLanceMarket.sol";

contract SignatureOwner {
    uint256 private mode;
    bytes32 private expectedDigest;

    function configure(uint256 value, bytes32 digest) external {
        mode = value;
        expectedDigest = digest;
    }

    function isValidSignature(bytes32 digest, bytes calldata) external returns (bytes4) {
        require(digest == expectedDigest);
        uint256 selected = mode;
        if (selected == 1) revert();
        if (selected == 7) {
            mode = 0;
            return 0x1626ba7e;
        }
        assembly ("memory-safe") {
            mstore(0, shl(224, 0x1626ba7e))
            switch selected
            case 2 { return(0, 4) }
            case 3 { return(0, 64) }
            case 4 {
                mstore8(31, 1)
                return(0, 32)
            }
            case 5 { return(0, 1000000) }
            case 6 { for {} 1 {} {} }
            default { return(0, 32) }
        }
    }
}

contract ReentrantReceiver {
    IAgentLanceMarket private immutable market;
    bytes[] private callbacks;
    uint256 public rejected;

    constructor(IAgentLanceMarket market_, bytes[] memory payloads) {
        market = market_;
        callbacks = payloads;
    }

    function withdraw(address receiver, uint256 amount) external {
        market.withdrawCredit(receiver, amount);
    }

    receive() external payable {
        for (uint256 i; i < callbacks.length; i++) {
            (bool success, bytes memory returned) = address(market).call(callbacks[i]);
            require(
                !success
                    && keccak256(returned)
                        == keccak256(
                            abi.encodeWithSelector(
                                IAgentLanceMarket.ProtocolError.selector, uint16(38)
                            )
                        )
            );
            rejected++;
        }
    }
}

contract RevertingReceiver {
    receive() external payable {
        revert();
    }

    function withdraw(IAgentLanceMarket market, address receiver, uint256 amount) external {
        market.withdrawCredit(receiver, amount);
    }
}

contract ForcedDonation {
    constructor(address payable destination) payable {
        selfdestruct(destination);
    }
}

contract MalformedRegistry {
    address private owner;
    address private wallet;
    uint256 private mode;

    constructor(address owner_, address wallet_) {
        owner = owner_;
        wallet = wallet_;
    }

    function setMode(uint256 value) external {
        mode = value;
    }

    fallback() external {
        address result = msg.sig == bytes4(keccak256("ownerOf(uint256)")) ? owner : wallet;
        uint256 selected = mode;
        assembly ("memory-safe") {
            mstore(0, result)
            switch selected
            case 1 { revert(0, 0) }
            case 2 { return(0, 31) }
            case 3 { return(0, 64) }
            case 4 {
                mstore(0, or(result, shl(160, 1)))
                return(0, 32)
            }
            case 5 { return(0, 1000000) }
            case 6 {
                mstore(0, 0)
                return(0, 32)
            }
            default { return(0, 32) }
        }
    }
}

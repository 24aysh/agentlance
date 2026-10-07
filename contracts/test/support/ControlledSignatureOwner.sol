// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";

/// @dev Test double for a previously qualified, ECDSA-controlled ERC-1271 identity owner.
contract ControlledSignatureOwner {
    address private immutable controller;

    constructor(address controller_) {
        controller = controller_;
    }

    function isValidSignature(bytes32 hash, bytes memory signature) external view returns (bytes4) {
        (address signer, ECDSA.RecoverError error,) = ECDSA.tryRecover(hash, signature);
        return error == ECDSA.RecoverError.NoError && signer == controller
            ? bytes4(0x1626ba7e)
            : bytes4(0xffffffff);
    }
}

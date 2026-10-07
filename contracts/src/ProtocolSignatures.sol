// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {ProtocolTypes as P} from "./ProtocolTypes.sol";

library ProtocolSignatures {
    bytes32 internal constant DOMAIN_TYPEHASH = keccak256(
        "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
    );
    bytes32 internal constant BID_TYPEHASH = keccak256(
        "BidPermit(uint64 taskId,address identityRegistry,uint256 agentId,address owner,address executionSigner,address payout,uint96 bidAtoms,bytes32 profileDigest,uint256 nonce,uint64 expiry)"
    );
    bytes32 internal constant VERDICT_TYPEHASH = keccak256(
        "ValidationVerdict(uint64 taskId,address identityRegistry,uint256 agentId,uint8 awardId,bytes32 resultDigest,bytes32 validationPolicyDigest,uint8 verdict,bytes32 evidenceDigest,address validator,uint256 nonce,uint64 expiry)"
    );

    function domainSeparator(uint256 chainId, address market) internal pure returns (bytes32) {
        return keccak256(
            abi.encode(DOMAIN_TYPEHASH, keccak256("AgentLance"), keccak256("1"), chainId, market)
        );
    }

    function bidStructHash(P.BidPermit memory permit) internal pure returns (bytes32) {
        return keccak256(
            abi.encode(
                BID_TYPEHASH,
                permit.taskRef.taskId,
                permit.agentRef.identityRegistry,
                permit.agentRef.agentId,
                permit.owner,
                permit.executionSigner,
                permit.payout,
                permit.bidAtoms,
                permit.profileDigest,
                permit.nonce,
                permit.expiry
            )
        );
    }

    function verdictStructHash(P.ValidationRecord memory record) internal pure returns (bytes32) {
        return keccak256(
            abi.encode(
                VERDICT_TYPEHASH,
                record.executionRef.taskRef.taskId,
                record.agentRef.identityRegistry,
                record.agentRef.agentId,
                record.executionRef.awardId,
                record.resultDigest,
                record.validationPolicyDigest,
                record.verdict,
                record.evidence.digest,
                record.validator,
                record.nonce,
                record.expiry
            )
        );
    }

    function typedDigest(bytes32 domain, bytes32 structHash) internal pure returns (bytes32) {
        return keccak256(abi.encodePacked(hex"1901", domain, structHash));
    }

    function verifyEoa(bytes32 digest, bytes memory signature, address signer)
        internal
        pure
        returns (bool)
    {
        if (signature.length != 65) return false;
        (address recovered, ECDSA.RecoverError error,) = ECDSA.tryRecover(digest, signature);
        return error == ECDSA.RecoverError.NoError && recovered != address(0) && recovered == signer;
    }

    function verifyBidPermit(bytes32 digest, bytes memory signature, address owner)
        internal
        view
        returns (bool)
    {
        if (owner.code.length == 0) return verifyEoa(digest, signature, owner);
        bytes memory input = abi.encodeWithSelector(bytes4(0x1626ba7e), digest, signature);
        bool success;
        uint256 size;
        bytes32 word;
        // Never allocate returndata: an untrusted owner can return a bomb.
        assembly ("memory-safe") {
            let output := mload(0x40)
            success := staticcall(50000, owner, add(input, 32), mload(input), output, 32)
            size := returndatasize()
            word := mload(output)
        }
        return success && size == 32 && word == bytes32(bytes4(0x1626ba7e));
    }
}

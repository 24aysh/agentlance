// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {ProtocolTypes as P} from "../../src/ProtocolTypes.sol";
import {ProtocolMath as Math} from "../../src/ProtocolMath.sol";
import {ProtocolSignatures as Signatures} from "../../src/ProtocolSignatures.sol";

/// @dev Test-only arbitrary-p projection; the deployed market never accepts caller-supplied p.
contract ConformanceHarness {
    error ProtocolError(uint16 code);

    function evaluateAuction(
        uint96 budget,
        uint96 numerator,
        uint96 denominator,
        P.Bid[] memory bids
    ) external pure returns (P.Allocation memory allocation) {
        if (budget == 0) revert ProtocolError(P.INVALID_BUDGET);
        if (numerator == 0 || denominator == 0 || Math.gcd(numerator, denominator) != 1) {
            revert ProtocolError(P.INVALID_ALPHA);
        }
        P.Bid memory best;
        P.Bid memory second;
        uint256 count;
        for (uint256 i; i < bids.length; ++i) {
            P.Bid memory bid = bids[i];
            if (bid.p > 1_000_000) revert ProtocolError(P.INVALID_RANGE);
            for (uint256 j; j < i; ++j) {
                if (
                    keccak256(abi.encode(bids[j].offer.agentRef))
                        == keccak256(abi.encode(bid.offer.agentRef))
                ) {
                    revert ProtocolError(P.DUPLICATE_AGENT);
                }
            }
            if (bid.offer.bidAtoms > budget) revert ProtocolError(P.BID_OVER_BUDGET);
            bid.score = Math.calculateScore(bid.p, numerator, denominator, bid.offer.bidAtoms);
            if (count == 0 || Math.precedes(bid, best)) {
                second = best;
                best = bid;
            } else if (count == 1 || Math.precedes(bid, second)) {
                second = bid;
            }
            ++count;
        }
        allocation.outcome = P.OUTCOME_UNALLOCATED;
        if (count == 0 || best.score <= 0) return allocation;
        allocation.outcome = P.OUTCOME_AWARDED;
        allocation.winner = P.OptionalAgentRef(true, best.offer.agentRef);
        allocation.winningScore = P.OptionalInt256(true, best.score);
        int256 threshold = count > 1 && second.score > 0 ? second.score : int256(0);
        allocation.secondScore = P.OptionalInt256(true, threshold);
        uint96 price = Math.calculatePrice(best.p, threshold, numerator, denominator);
        allocation.criticalAtoms = P.OptionalUint96(true, price);
        allocation.reservedAtoms = price < budget ? price : budget;
    }

    function probability(uint64 successes, uint64 failures) external pure returns (uint32) {
        return Math.calculateProbability(successes, failures);
    }

    function bidHashes(
        uint256 chainId,
        address market,
        P.BidPermit memory permit,
        bytes memory signature
    ) external pure returns (bytes32, bytes32, bytes32, bytes32, bool) {
        bytes32 domain = Signatures.domainSeparator(chainId, market);
        bytes32 structure = Signatures.bidStructHash(permit);
        bytes32 digest = Signatures.typedDigest(domain, structure);
        return (
            Signatures.BID_TYPEHASH,
            domain,
            structure,
            digest,
            Signatures.verifyEoa(digest, signature, permit.owner)
        );
    }

    function verdictHashes(uint256 chainId, address market, P.ValidationRecord memory record)
        external
        pure
        returns (bytes32, bytes32, bytes32, bytes32, bool)
    {
        bytes32 domain = Signatures.domainSeparator(chainId, market);
        bytes32 structure = Signatures.verdictStructHash(record);
        bytes32 digest = Signatures.typedDigest(domain, structure);
        return (
            Signatures.VERDICT_TYPEHASH,
            domain,
            structure,
            digest,
            Signatures.verifyEoa(digest, record.signature, record.validator)
        );
    }

    function verifyDigest(
        bytes32 domain,
        bytes32 structHash,
        bytes memory signature,
        address signer
    ) external pure returns (bool) {
        return Signatures.verifyEoa(Signatures.typedDigest(domain, structHash), signature, signer);
    }

    function echoAgentRef(P.AgentRef memory value) external pure returns (P.AgentRef memory) {
        return value;
    }

    function echoTaskRef(P.TaskRef memory value) external pure returns (P.TaskRef memory) {
        return value;
    }

    function echoTaskSpec(P.TaskSpec memory value) external pure returns (P.TaskSpec memory) {
        return value;
    }

    function echoBid(P.Bid memory value) external pure returns (P.Bid memory) {
        return value;
    }

    function echoAllocation(P.Allocation memory value) external pure returns (P.Allocation memory) {
        return value;
    }

    function echoExecutionRef(P.ExecutionRef memory value)
        external
        pure
        returns (P.ExecutionRef memory)
    {
        return value;
    }

    function echoResultCommitment(P.ResultCommitment memory value)
        external
        pure
        returns (P.ResultCommitment memory)
    {
        return value;
    }

    function echoValidationRecord(P.ValidationRecord memory value)
        external
        pure
        returns (P.ValidationRecord memory)
    {
        return value;
    }

    function echoSettlementReceipt(P.SettlementReceipt memory value)
        external
        pure
        returns (P.SettlementReceipt memory)
    {
        return value;
    }

    function hashContent(bytes memory value) external pure returns (bytes32) {
        return keccak256(value);
    }
}

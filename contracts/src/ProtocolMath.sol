// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {ProtocolTypes as P} from "./ProtocolTypes.sol";

/// @dev Integer market mathematics; amounts are atoms and p has denominator Q.
library ProtocolMath {
    uint256 internal constant Q = 1_000_000;

    function gcd(uint256 a, uint256 b) internal pure returns (uint256) {
        while (b != 0) {
            (a, b) = (b, a % b);
        }
        return a;
    }

    function calculateProbability(uint64 successes, uint64 failures)
        internal
        pure
        returns (uint32)
    {
        return uint32(Q * (1 + uint256(successes)) / (2 + uint256(successes) + failures));
    }

    function calculateScore(uint32 p, uint96 alphaNum, uint96 alphaDen, uint96 bidAtoms)
        internal
        pure
        returns (int256)
    {
        // The largest product is below 2^212; both signed conversions are exact.
        return int256(uint256(p) * alphaDen) - int256(Q * uint256(alphaNum) * bidAtoms);
    }

    function precedes(P.Bid memory left, P.Bid memory right) internal pure returns (bool) {
        if (left.score != right.score) return left.score > right.score;
        P.AgentRef memory a = left.offer.agentRef;
        P.AgentRef memory b = right.offer.agentRef;
        if (a.chainId != b.chainId) return a.chainId < b.chainId;
        if (a.identityRegistry != b.identityRegistry) {
            return a.identityRegistry < b.identityRegistry;
        }
        return a.agentId < b.agentId;
    }

    function calculatePrice(uint32 p, int256 secondScore, uint96 alphaNum, uint96 alphaDen)
        internal
        pure
        returns (uint96)
    {
        return uint96((uint256(p) * alphaDen - uint256(secondScore)) / (Q * alphaNum));
    }
}

// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {MarketTestBase} from "./support/MarketTestBase.sol";
import {ProtocolTypes as P} from "../src/ProtocolTypes.sol";
import {ProtocolSignatures as Signatures} from "../src/ProtocolSignatures.sol";

contract MarketPropertiesTest is MarketTestBase {
    function testFuzzBestTwoEqualsIndependentFullSort(uint96[8] memory amounts, uint256 offset)
        public
    {
        createRoot();
        int256[8] memory scores;
        uint256[8] memory ids;
        P.OptionalBidPermit memory permit;
        P.OptionalSignature memory signature;
        for (uint256 i; i < 8; i++) {
            uint256 id = (i + offset % 8) % 8;
            uint96 amount = amounts[i] % 101;
            registry.setIdentity(id, owner, PAYOUT);
            P.BidOffer memory value = offer(1, id);
            value.payout = PAYOUT;
            value.bidAtoms = amount;
            vm.prank(owner);
            market.submitBid(value, permit, signature);
            scores[i] = 50_000_000 - int256(uint256(amount) * 1_000_000);
            ids[i] = id;
        }
        // Independent full sort is test-only; production touches two candidates at close.
        for (uint256 i; i < 8; i++) {
            for (uint256 j = i + 1; j < 8; j++) {
                if (scores[j] > scores[i] || (scores[j] == scores[i] && ids[j] < ids[i])) {
                    (scores[i], scores[j]) = (scores[j], scores[i]);
                    (ids[i], ids[j]) = (ids[j], ids[i]);
                }
            }
        }
        vm.warp(1200);
        market.allocateTask(taskRef(1));
        P.Allocation memory closed = market.readTask(taskRef(1)).allocation.value;
        if (scores[0] <= 0) {
            assertEq(closed.outcome, P.OUTCOME_UNALLOCATED);
            assertFalse(closed.winner.present);
            assertEq(market.readSettlement(taskRef(1)).value.refundAtoms, 100);
        } else {
            int256 second = scores[1] > 0 ? scores[1] : int256(0);
            uint256 critical = uint256(50_000_000 - second) / 1_000_000;
            assertEq(closed.winner.value.agentId, ids[0]);
            assertEq(closed.winningScore.value, scores[0]);
            assertEq(closed.secondScore.value, second);
            assertEq(closed.criticalAtoms.value, critical);
            assertEq(closed.reservedAtoms, critical < 100 ? critical : 100);
        }
    }

    function testFuzzEoaDigestAndSignerIsolation(bytes32 digest, uint256 salt) public view {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(OWNER_KEY, digest);
        bytes memory signature = abi.encodePacked(r, s, v);
        assertTrue(Signatures.verifyEoa(digest, signature, owner));
        assertFalse(Signatures.verifyEoa(digest, signature, validator));
        assertFalse(Signatures.verifyEoa(keccak256(abi.encode(digest, salt)), signature, owner));
    }
}

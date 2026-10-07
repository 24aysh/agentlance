// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {MarketTestBase} from "./support/MarketTestBase.sol";
import {ProtocolTypes as P} from "../src/ProtocolTypes.sol";
import {ProtocolMath} from "../src/ProtocolMath.sol";
import {ProtocolSignatures as Signatures} from "../src/ProtocolSignatures.sol";

contract ReputationCheckpointsTest is MarketTestBase {
    function laterTerms() internal view returns (P.TaskTerms memory value) {
        value = terms();
        value.biddingClose = 1400;
        value.allocationBy = 1500;
        value.acceptBy = 1620;
        value.resultBy = 2000;
        value.validationBy = 2400;
    }

    function testSameBlockBeforeAfterSettlementAndNextBlockVisibility() public {
        runTo(P.STATE_SUBMITTED);
        vm.roll(20);
        vm.prank(REQUESTER);
        market.createTask{value: 100}(laterTerms());
        market.settleVerdict(signVerdict(verdict(1, 7, true)));
        vm.prank(REQUESTER);
        market.createTask{value: 100}(laterTerms());
        directBid(2, 7);
        directBid(3, 7);
        assertEq(market.readBid(taskRef(2), agentRef(7)).value.p, 500000);
        assertEq(market.readBid(taskRef(3), agentRef(7)).value.p, 500000);
        assertEq(market.readTask(taskRef(2)).task.reputationSnapshotBlock, 19);
        assertEq(market.readTask(taskRef(3)).task.reputationSnapshotBlock, 19);
        vm.roll(21);
        vm.prank(REQUESTER);
        market.createTask{value: 100}(laterTerms());
        directBid(4, 7);
        assertEq(market.readBid(taskRef(4), agentRef(7)).value.p, 666666);
        assertEq(market.readBid(taskRef(4), agentRef(7)).value.counters.successes, 1);
        vm.warp(1400);
        for (uint64 id = 2; id <= 3; id++) {
            market.allocateTask(taskRef(id));
            vm.prank(SIGNER);
            market.acceptAward(P.ExecutionRef(taskRef(id), 1), 0);
            vm.prank(SIGNER);
            market.submitResult(P.ExecutionRef(taskRef(id), 1), result(id).artifact);
            market.settleVerdict(signVerdict(verdict(id, 7, true)));
        }
        assertEq(market.checkpointCount(7, "structured-output-v1"), 2);
        (uint64 oldSuccesses,) = market.readCounters(agentRef(7), "structured-output-v1", 20);
        (uint64 latestSuccesses,) =
            market.readCounters(agentRef(7), "structured-output-v1", type(uint64).max);
        assertEq(oldSuccesses, 1);
        assertEq(latestSuccesses, 3);
        assertEq(market.readBid(taskRef(4), agentRef(7)).value.counters.successes, 1);
    }

    function testNamespaceCeilingDoesNotBlockFinalObservation() public {
        runTo(P.STATE_SUBMITTED);
        market.seedTaskCount(type(uint64).max);
        market.seedCheckpoint(7, "structured-output-v1", 9, type(uint64).max - 1, 0);
        vm.roll(11);
        market.settleVerdict(signVerdict(verdict(1, 7, true)));
        (uint64 successes, uint64 failures) =
            market.readCounters(agentRef(7), "structured-output-v1", 11);
        assertEq(successes, type(uint64).max);
        assertEq(failures, 0);
        assertEq(ProtocolMath.calculateProbability(successes, failures), 999999);
        vm.warp(1000);
        vm.expectRevert(errorBytes(P.TASK_LIMIT));
        market.createTask{value: 100}(terms());
        vm.prank(PAYOUT);
        market.withdrawCredit(OTHER, 50);
        assertEq(market.readCredit(PAYOUT), 0);
    }

    function testImpossibleCounterOverflowRollsBackVerdictNonceAndMoney() public {
        runTo(P.STATE_SUBMITTED);
        market.seedCheckpoint(7, "structured-output-v1", 9, 1, 0);
        P.ValidationRecord memory record = signVerdict(verdict(1, 7, true));
        vm.expectRevert(errorBytes(P.INVALID_RANGE));
        market.settleVerdict(record);
        assertFalse(market.isNonceUsed(validator, Signatures.VERDICT_TYPEHASH, record.nonce));
        assertEq(market.readTask(taskRef(1)).status, P.STATE_SUBMITTED);
        assertEq(market.readCredit(PAYOUT), 0);
        (, uint256 deposited, uint256 escrow, uint256 credit, uint256 withdrawn) =
            market.readAccounting();
        assertEq(deposited, 100);
        assertEq(escrow, 100);
        assertEq(credit, 0);
        assertEq(withdrawn, 0);
    }

    function testFuzzProbabilityBounds(uint64 successes, uint64 failures) public pure {
        uint32 p = ProtocolMath.calculateProbability(successes, failures);
        assertLe(p, 1_000_000);
        assertEq(
            p, uint256(1_000_000) * (uint256(successes) + 1) / (uint256(successes) + failures + 2)
        );
    }
}

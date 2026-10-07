// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {MarketFixtureBase} from "./MarketLifecycle.t.sol";
import {ProtocolTypes as P} from "../src/ProtocolTypes.sol";
import {IAgentLanceMarket} from "../src/IAgentLanceMarket.sol";
import {Vm} from "forge-std/Vm.sol";

contract MarketLayerOneTest is MarketFixtureBase {
    function testLayerOneTreeSuccess() public {
        runTreeGolden(15, false);
    }

    function testLayerOneTreeParentTimeout() public {
        runTreeGolden(16, true);
    }

    function runTreeGolden(uint256 index, bool timeout) internal {
        string memory json = vm.readFile("specs/fixtures/layer-1.json");
        string memory path = string.concat(".cases[", vm.toString(index), "]");
        string memory initial = string.concat(path, ".initial");
        string memory expected = string.concat(path, ".expected");
        P.TaskTerms memory rootTerms = terms();
        rootTerms.budgetAtoms = uint96(numberAt(json, string.concat(initial, ".rootBudget")));
        rootTerms.alphaDen = uint96(numberAt(json, string.concat(initial, ".rootAlphaDen")));
        registry.setIdentity(8, owner, OTHER);
        vm.recordLogs();
        vm.prank(REQUESTER);
        market.createTask{value: rootTerms.budgetAtoms}(rootTerms);
        P.OptionalBidPermit memory permit;
        P.OptionalSignature memory signature;
        P.BidOffer memory first = offer(1, 7);
        first.bidAtoms = uint96(numberAt(json, string.concat(initial, ".rootBids[0]")));
        vm.prank(owner);
        market.submitBid(first, permit, signature);
        P.BidOffer memory second = offer(1, 8);
        second.payout = OTHER;
        second.bidAtoms = uint96(numberAt(json, string.concat(initial, ".rootBids[1]")));
        vm.prank(owner);
        market.submitBid(second, permit, signature);
        vm.warp(rootTerms.biddingClose);
        market.allocateTask(taskRef(1));
        vm.prank(SIGNER);
        market.acceptAward(
            P.ExecutionRef(taskRef(1), 1),
            uint96(numberAt(json, string.concat(initial, ".ownReserve")))
        );
        P.TaskTerms memory child = terms();
        child.refundAddress = SIGNER;
        child.budgetAtoms = uint96(numberAt(json, string.concat(initial, ".childBudget")));
        child.alphaDen = uint96(numberAt(json, string.concat(initial, ".childAlphaDen")));
        child.biddingClose = 1500;
        child.allocationBy = 1600;
        child.acceptBy = 1720;
        child.resultBy = 2000;
        child.validationBy = 2200;
        child.delegation = P.DelegationLimits(1, 0);
        vm.prank(SIGNER);
        market.createChildTask{value: child.budgetAtoms}(taskRef(1), child);
        P.BidOffer memory childOffer = offer(2, 9);
        childOffer.bidAtoms = uint96(numberAt(json, string.concat(initial, ".childBid")));
        vm.prank(owner);
        market.submitBid(childOffer, permit, signature);
        vm.warp(child.biddingClose);
        market.allocateTask(taskRef(2));
        vm.prank(SIGNER);
        market.acceptAward(P.ExecutionRef(taskRef(2), 1), 0);
        vm.prank(SIGNER);
        market.submitResult(P.ExecutionRef(taskRef(2), 1), result(2).artifact);
        market.settleVerdict(signVerdict(verdict(2, 9, true)));
        if (timeout) {
            vm.warp(rootTerms.resultBy);
            market.expireTask(taskRef(1));
        } else {
            vm.prank(SIGNER);
            market.submitResult(P.ExecutionRef(taskRef(1), 1), result(1).artifact);
            market.settleVerdict(signVerdict(verdict(1, 7, true)));
        }
        P.SettlementReceipt memory parentReceipt = market.readSettlement(taskRef(1)).value;
        P.SettlementReceipt memory childReceipt = market.readSettlement(taskRef(2)).value;
        assertEq(parentReceipt.paidAtoms, numberAt(json, string.concat(expected, ".rootPaid")));
        assertEq(parentReceipt.refundAtoms, numberAt(json, string.concat(expected, ".rootRefund")));
        assertEq(childReceipt.paidAtoms, numberAt(json, string.concat(expected, ".childPaid")));
        assertEq(childReceipt.refundAtoms, numberAt(json, string.concat(expected, ".childRefund")));
        address[4] memory recipients = [PAYOUT, REQUESTER, CHILD_PAYOUT, SIGNER];
        for (uint256 i; i < recipients.length; i++) {
            uint256 credit = market.readCredit(recipients[i]);
            if (credit != 0) {
                vm.prank(recipients[i]);
                market.withdrawCredit(OTHER, credit);
            }
        }
        (,, uint256 escrow, uint256 credits, uint256 withdrawn) = market.readAccounting();
        assertEq(escrow, 0);
        assertEq(credits, 0);
        assertEq(withdrawn, numberAt(json, string.concat(expected, ".withdrawnAtoms")));
        Vm.Log[] memory logs = vm.getRecordedLogs();
        string[] memory events = vm.parseJsonStringArray(json, string.concat(path, ".events"));
        assertEq(logs.length, events.length);
        for (uint256 i; i < events.length; i++) {
            assertEq(logs[i].topics[0], eventTopic(events[i]));
        }
    }
}

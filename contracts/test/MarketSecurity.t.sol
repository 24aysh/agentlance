// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {MarketTestBase} from "./support/MarketTestBase.sol";
import {
    ReentrantReceiver,
    RevertingReceiver,
    ForcedDonation,
    MalformedRegistry
} from "./support/HostileActors.sol";
import {ProtocolTypes as P} from "../src/ProtocolTypes.sol";
import {IAgentLanceMarket} from "../src/IAgentLanceMarket.sol";
import {AgentLanceMarket} from "../src/AgentLanceMarket.sol";
import {MarketHarness} from "./support/MarketHarness.sol";
import {TestIdentityRegistry} from "./support/TestIdentityRegistry.sol";

contract MarketSecurityTest is MarketTestBase {
    function testWithdrawalCrossCommandReentrancy() public {
        bytes[] memory payloads = new bytes[](10);
        P.OptionalBidPermit memory permit;
        P.OptionalSignature memory signature;
        payloads[0] = abi.encodeCall(IAgentLanceMarket.createTask, (terms()));
        payloads[1] = abi.encodeCall(IAgentLanceMarket.submitBid, (offer(1, 7), permit, signature));
        payloads[2] = abi.encodeCall(IAgentLanceMarket.allocateTask, (taskRef(1)));
        payloads[3] =
            abi.encodeCall(IAgentLanceMarket.acceptAward, (P.ExecutionRef(taskRef(1), 1), 0));
        payloads[4] = abi.encodeCall(IAgentLanceMarket.createChildTask, (taskRef(1), terms()));
        payloads[5] = abi.encodeCall(
            IAgentLanceMarket.submitResult, (P.ExecutionRef(taskRef(1), 1), result(1).artifact)
        );
        payloads[6] =
            abi.encodeCall(IAgentLanceMarket.settleVerdict, (signVerdict(verdict(1, 7, true))));
        payloads[7] = abi.encodeCall(IAgentLanceMarket.expireTask, (taskRef(1)));
        payloads[8] = abi.encodeCall(IAgentLanceMarket.cancelTask, (taskRef(1)));
        payloads[9] = abi.encodeCall(IAgentLanceMarket.withdrawCredit, (OTHER, 1));
        ReentrantReceiver receiver = new ReentrantReceiver(market, payloads);
        P.TaskTerms memory value = terms();
        value.refundAddress = address(receiver);
        vm.prank(REQUESTER);
        market.createTask{value: 100}(value);
        vm.prank(REQUESTER);
        market.cancelTask(taskRef(1));
        receiver.withdraw(address(receiver), 100);
        assertEq(receiver.rejected(), 10);
        assertEq(market.readCredit(address(receiver)), 0);
        (uint64 count, uint256 deposited, uint256 escrow, uint256 credit, uint256 withdrawn) =
            market.readAccounting();
        assertEq(count, 1);
        assertEq(deposited, 100);
        assertEq(escrow, 0);
        assertEq(credit, 0);
        assertEq(withdrawn, 100);
    }

    function testHostileRecipientCannotBlockSettlementAndCanChooseReceiver() public {
        RevertingReceiver receiver = new RevertingReceiver();
        P.TaskTerms memory value = terms();
        value.refundAddress = address(receiver);
        vm.prank(REQUESTER);
        market.createTask{value: 100}(value);
        vm.prank(REQUESTER);
        market.cancelTask(taskRef(1));
        vm.expectRevert(errorBytes(P.TRANSFER_FAILED));
        receiver.withdraw(market, address(receiver), 100);
        assertEq(market.readCredit(address(receiver)), 100);
        receiver.withdraw(market, OTHER, 100);
        assertEq(market.readCredit(address(receiver)), 0);
        (,,, uint256 credit, uint256 withdrawn) = market.readAccounting();
        assertEq(credit, 0);
        assertEq(withdrawn, 100);
    }

    function testForcedBalanceIsNotCredit() public {
        createRoot();
        new ForcedDonation{value: 17}(payable(address(market)));
        (, uint256 deposited, uint256 escrow, uint256 credit, uint256 withdrawn) =
            market.readAccounting();
        assertEq(address(market).balance, 117);
        assertEq(deposited, escrow + credit + withdrawn);
        assertEq(deposited, 100);
        (bool accepted,) = address(market).call{value: 1}("");
        assertFalse(accepted);
    }

    function testAliasCreditAndZeroPaymentSuccess() public {
        P.TaskTerms memory value = terms();
        value.refundAddress = PAYOUT;
        vm.prank(REQUESTER);
        market.createTask{value: 100}(value);
        directBid(1, 7);
        vm.warp(1200);
        market.allocateTask(taskRef(1));
        vm.prank(SIGNER);
        market.acceptAward(P.ExecutionRef(taskRef(1), 1), 0);
        vm.prank(SIGNER);
        market.submitResult(P.ExecutionRef(taskRef(1), 1), result(1).artifact);
        market.settleVerdict(signVerdict(verdict(1, 7, true)));
        assertEq(market.readCredit(PAYOUT), 100);
        vm.warp(1000);
        value = terms();
        value.alphaNum = 1;
        value.alphaDen = 1;
        vm.prank(REQUESTER);
        market.createTask{value: 100}(value);
        P.BidOffer memory zero = offer(2, 7);
        zero.bidAtoms = 0;
        P.OptionalBidPermit memory permit;
        P.OptionalSignature memory signature;
        vm.prank(owner);
        market.submitBid(zero, permit, signature);
        vm.warp(1200);
        market.allocateTask(taskRef(2));
        assertEq(market.readTask(taskRef(2)).allocation.value.reservedAtoms, 0);
        vm.prank(SIGNER);
        market.acceptAward(P.ExecutionRef(taskRef(2), 1), 0);
        vm.prank(SIGNER);
        market.submitResult(P.ExecutionRef(taskRef(2), 1), result(2).artifact);
        market.settleVerdict(signVerdict(verdict(2, 7, true)));
        P.SettlementReceipt memory receipt = market.readSettlement(taskRef(2)).value;
        assertEq(receipt.reason, P.REASON_SUCCESS);
        assertEq(receipt.paidAtoms, 0);
        assertEq(receipt.refundAtoms, 100);
        (uint64 successes,) = market.readCounters(agentRef(7), "structured-output-v1", 10);
        assertEq(successes, 2);
    }

    function testAbsentWrapperHiddenFieldsAndZeroAgentDigest() public {
        P.TaskTerms memory value = terms();
        value.retryOf.value.taskId = 1;
        vm.expectRevert(errorBytes(P.INVALID_ENCODING));
        market.createTask{value: 100}(value);
        createRoot();
        registry.setIdentity(0, owner, PAYOUT);
        P.BidOffer memory zero = offer(1, 0);
        P.OptionalBidPermit memory permit;
        P.OptionalSignature memory signature;
        permit.value.nonce = 1;
        vm.expectRevert(errorBytes(P.INVALID_ENCODING));
        market.submitBid(zero, permit, signature);
        permit.value.nonce = 0;
        signature.value = hex"01";
        vm.expectRevert(errorBytes(P.INVALID_ENCODING));
        market.submitBid(zero, permit, signature);
        signature.value = hex"";
        vm.prank(owner);
        market.submitBid(zero, permit, signature);
        assertTrue(market.readBid(taskRef(1), agentRef(0)).present);
        assertEq(market.readBid(taskRef(1), agentRef(0)).value.offer.profileDigest, bytes32(0));
    }

    function testTermsBoundaryErrorsAndRollback() public {
        P.TaskTerms memory value = terms();
        value.alphaDen = 2;
        value.alphaNum = 2;
        vm.expectRevert(errorBytes(P.INVALID_ALPHA));
        market.createTask{value: 100}(value);
        value = terms();
        value.refundAddress = address(0);
        vm.expectRevert(errorBytes(P.INVALID_RANGE));
        market.createTask{value: 100}(value);
        value = terms();
        value.delegation.maxChildren = 5;
        vm.expectRevert(errorBytes(P.INVALID_RANGE));
        market.createTask{value: 100}(value);
        value = terms();
        value.delegation = P.DelegationLimits(0, 1);
        vm.expectRevert(errorBytes(P.INVALID_TERMS));
        market.createTask{value: 100}(value);
        value = terms();
        value.refundAddress = validator;
        vm.expectRevert(errorBytes(P.VALIDATOR_CONFLICT));
        market.createTask{value: 100}(value);
        value = terms();
        value.acceptBy = value.allocationBy + 119;
        vm.expectRevert(errorBytes(P.INVALID_DEADLINES));
        market.createTask{value: 100}(value);
        value = terms();
        value.input.uri = "http://insecure.invalid";
        vm.expectRevert(errorBytes(P.INVALID_ENCODING));
        market.createTask{value: 100}(value);
        value = terms();
        value.policyVersion = 2;
        vm.expectRevert(errorBytes(P.UNSUPPORTED_POLICY));
        market.createTask{value: 100}(value);
        value = terms();
        value.asset.symbol = "ETH";
        vm.expectRevert(errorBytes(P.INVALID_ASSET));
        market.createTask{value: 100}(value);
        value = terms();
        value.budgetAtoms = 0;
        vm.expectRevert(errorBytes(P.INVALID_BUDGET));
        market.createTask(value);
        (uint64 count, uint256 deposited,,,) = market.readAccounting();
        assertEq(count, 0);
        assertEq(deposited, 0);
    }

    function testConstructorNamespaceAndRoleFailures() public {
        vm.expectRevert(errorBytes(P.INVALID_RANGE));
        new AgentLanceMarket(address(0), validator);
        vm.expectRevert(errorBytes(P.IDENTITY_UNAVAILABLE));
        new AgentLanceMarket(OTHER, validator);
        createRoot();
        P.TaskRef memory bad = taskRef(1);
        bad.chainId++;
        vm.expectRevert(errorBytes(P.UNKNOWN_TASK));
        market.readTask(bad);
        P.AgentRef memory badAgent = agentRef(7);
        badAgent.chainId++;
        vm.expectRevert(errorBytes(P.IDENTITY_UNAVAILABLE));
        market.readBid(taskRef(1), badAgent);
        vm.expectRevert(errorBytes(P.UNSUPPORTED_POLICY));
        market.readCounters(agentRef(7), "other", 10);
        vm.expectRevert(errorBytes(P.INVALID_RANGE));
        market.readCredit(address(0));
        vm.expectRevert(errorBytes(P.INVALID_RANGE));
        market.isNonceUsed(address(0), bytes32(0), 0);
        assertFalse(market.isNonceUsed(owner, bytes32(uint256(9)), 0));
        vm.expectRevert(errorBytes(P.INVALID_RANGE));
        market.withdrawCredit(OTHER, 0);
        vm.expectRevert(errorBytes(P.INVALID_RANGE));
        market.withdrawCredit(address(0), 1);
        vm.expectRevert(errorBytes(P.INSUFFICIENT_CREDIT));
        market.withdrawCredit(OTHER, 1);
    }

    function testRegistryUnavailableAndWrongRoleDoNotAdmit() public {
        createRoot();
        P.OptionalBidPermit memory permit;
        P.OptionalSignature memory signature;
        vm.prank(OTHER);
        vm.expectRevert(errorBytes(P.UNAUTHORIZED));
        market.submitBid(offer(1, 7), permit, signature);
        registry.setUnavailable(true);
        vm.prank(owner);
        vm.expectRevert(errorBytes(P.IDENTITY_UNAVAILABLE));
        market.submitBid(offer(1, 7), permit, signature);
        registry.setUnavailable(false);
        registry.setIdentity(7, owner, OTHER);
        vm.prank(owner);
        vm.expectRevert(errorBytes(P.WALLET_MISMATCH));
        market.submitBid(offer(1, 7), permit, signature);
        registry.setIdentity(7, owner, PAYOUT);
        P.BidOffer memory value = offer(1, 7);
        value.executionSigner = validator;
        vm.prank(owner);
        vm.expectRevert(errorBytes(P.VALIDATOR_CONFLICT));
        market.submitBid(value, permit, signature);
        assertFalse(market.readBid(taskRef(1), agentRef(7)).present);
    }

    function testRegistryMalformedResponsesAreBounded() public {
        MalformedRegistry malformed = new MalformedRegistry(owner, PAYOUT);
        registry = TestIdentityRegistry(address(malformed));
        market = new MarketHarness(address(registry), validator);
        createRoot();
        P.OptionalBidPermit memory permit;
        P.OptionalSignature memory signature;
        for (uint256 mode = 1; mode <= 6; mode++) {
            malformed.setMode(mode);
            vm.prank(owner);
            vm.expectRevert(errorBytes(P.IDENTITY_UNAVAILABLE));
            market.submitBid(offer(1, 7), permit, signature);
            assertFalse(market.readBid(taskRef(1), agentRef(7)).present);
        }
        malformed.setMode(0);
        directBid(1, 7);
        malformed.setMode(1);
        vm.warp(1200);
        market.allocateTask(taskRef(1));
        vm.warp(1500);
        market.expireTask(taskRef(1));
        assertEq(market.readSettlement(taskRef(1)).value.reason, P.REASON_NO_SHOW);
    }
}

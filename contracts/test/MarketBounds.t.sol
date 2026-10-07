// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {MarketTestBase} from "./support/MarketTestBase.sol";
import {MarketHarness} from "./support/MarketHarness.sol";
import {AgentLanceMarket} from "../src/AgentLanceMarket.sol";
import {ProtocolTypes as P} from "../src/ProtocolTypes.sol";

contract MaximumSignatureOwner {
    function isValidSignature(bytes32, bytes calldata signature) external pure returns (bytes4) {
        return signature.length == 4096 ? bytes4(0x1626ba7e) : bytes4(0);
    }
}

contract MarketBoundsTest is MarketTestBase {
    struct GasObservation {
        uint256 allocationGas;
        uint256 allocationReads;
        uint256 allocationWrites;
        uint256 settlementGas;
        uint256 settlementReads;
        uint256 settlementWrites;
    }

    function freshProductionMarket() internal {
        // Reuse fixture helpers' ABI while executing exactly the production bytecode, with no seeding.
        market = MarketHarness(address(new AgentLanceMarket(address(registry), validator)));
    }

    function observeMarket(uint256 bidders) internal returns (GasObservation memory observed) {
        vm.warp(1000);
        freshProductionMarket();
        P.TaskRef memory ref = createRoot();
        for (uint256 index; index < bidders; ++index) {
            uint256 id = index + 7;
            registry.setIdentity(id, owner, id == 9 ? CHILD_PAYOUT : PAYOUT);
            uint256 beforeBid = gasleft();
            directBid(ref.taskId, id);
            if (index < 2) {
                emit log_named_uint(
                    index == 0 ? "firstBidGas" : "subsequentBidGas", beforeBid - gasleft()
                );
            }
        }
        registry.setUnavailable(true);
        vm.warp(1200);
        vm.record();
        uint256 before = gasleft();
        market.allocateTask(ref);
        observed.allocationGas = before - gasleft();
        (bytes32[] memory reads, bytes32[] memory writes) = vm.accesses(address(market));
        observed.allocationReads = reads.length;
        observed.allocationWrites = writes.length;
        P.TaskView memory view_ = market.readTask(ref);
        assertEq(view_.winningBid.value.offer.agentRef.agentId, 7);
        uint96 reserve = bidders == 1 ? 50 : 20;
        assertEq(view_.allocation.value.reservedAtoms, reserve);
        vm.prank(SIGNER);
        market.acceptAward(P.ExecutionRef(ref, 1), reserve);
        vm.prank(SIGNER);
        market.submitResult(P.ExecutionRef(ref, 1), result(ref.taskId).artifact);
        P.ValidationRecord memory record = signVerdict(verdict(ref.taskId, 7, true));
        vm.record();
        before = gasleft();
        market.settleVerdict(record);
        observed.settlementGas = before - gasleft();
        (reads, writes) = vm.accesses(address(market));
        observed.settlementReads = reads.length;
        observed.settlementWrites = writes.length;
        assertEq(market.readSettlement(ref).value.paidAtoms, reserve);
        (, uint256 deposits, uint256 escrow, uint256 credits, uint256 withdrawn) =
            market.readAccounting();
        assertEq(deposits, credits + escrow + withdrawn);
        assertEq(escrow, 0);
        registry.setUnavailable(false);
        emit log_named_uint("bidders", bidders);
        emit log_named_uint("allocationGas", observed.allocationGas);
        emit log_named_uint("allocationReadSlots", observed.allocationReads);
        emit log_named_uint("settlementGas", observed.settlementGas);
        emit log_named_uint("settlementReadSlots", observed.settlementReads);
    }

    function checkGrowth(uint256 count) internal {
        GasObservation memory baseline = observeMarket(2);
        GasObservation memory measured = observeMarket(count);
        assertEq(measured.allocationReads, baseline.allocationReads);
        assertEq(measured.allocationWrites, baseline.allocationWrites);
        assertEq(measured.settlementReads, baseline.settlementReads);
        assertEq(measured.settlementWrites, baseline.settlementWrites);
        assertLe(measured.allocationGas, baseline.allocationGas + 5000);
        assertLe(measured.settlementGas, baseline.settlementGas + 5000);
    }

    function testAllocationGasOneBid() public {
        observeMarket(1);
    }

    function testAllocationGasTwoBids() public {
        observeMarket(2);
    }

    function testAllocationGasTenBids() public {
        checkGrowth(10);
    }

    function testAllocationGasSixteenBids() public {
        checkGrowth(16);
    }

    function testAllocationGasHundredBids() public {
        checkGrowth(100);
    }

    function testAllocationGas256Bids() public {
        checkGrowth(256);
    }

    function testAllocationGasThousandBids() public {
        checkGrowth(1000);
    }

    function testAllocationGas1024Bids() public {
        checkGrowth(1024);
    }

    function maximumUri() internal pure returns (string memory) {
        bytes memory uri = new bytes(2048);
        bytes memory prefix = bytes("ipfs://");
        for (uint256 index; index < uri.length; ++index) {
            uri[index] = index < prefix.length ? prefix[index] : bytes1("a");
        }
        return string(uri);
    }

    function runMaximumWireScenario() internal returns (uint256[4] memory measured) {
        freshProductionMarket();
        string memory uri = maximumUri();
        P.TaskTerms memory value = terms();
        value.input.uri = uri;
        value.outputSchema.uri = uri;
        value.validationPolicy.uri = uri;
        vm.prank(REQUESTER);
        uint256 before = gasleft();
        P.TaskRef memory ref = market.createTask{value: 100}(value);
        measured[0] = before - gasleft();
        emit log_named_uint("maximumCreationGas", measured[0]);
        MaximumSignatureOwner contractOwner = new MaximumSignatureOwner();
        registry.setIdentity(7, address(contractOwner), PAYOUT);
        P.BidOffer memory bidOffer = offer(ref.taskId, 7);
        P.BidPermit memory permit = P.BidPermit(
            ref, agentRef(7), address(contractOwner), SIGNER, PAYOUT, 20, bytes32(0), 42, 1100
        );
        before = gasleft();
        market.submitBid(
            bidOffer, P.OptionalBidPermit(true, permit), P.OptionalSignature(true, new bytes(4096))
        );
        measured[1] = before - gasleft();
        emit log_named_uint("maximum1271PermitAdmissionGas", measured[1]);
        vm.warp(1200);
        market.allocateTask(ref);
        vm.prank(SIGNER);
        market.acceptAward(P.ExecutionRef(ref, 1), 50);
        vm.prank(SIGNER);
        before = gasleft();
        market.submitResult(P.ExecutionRef(ref, 1), P.ContentRef(uri, ARTIFACT));
        measured[2] = before - gasleft();
        emit log_named_uint("maximumResultGas", measured[2]);
        P.ValidationRecord memory record = verdict(ref.taskId, 7, true);
        record.evidence.uri = uri;
        record = signVerdict(record);
        before = gasleft();
        market.settleVerdict(record);
        measured[3] = before - gasleft();
        emit log_named_uint("maximumVerdictGas", measured[3]);
        P.TaskView memory read = market.readTask(ref);
        bytes memory encoded = abi.encode(read);
        P.TaskView memory decoded = abi.decode(encoded, (P.TaskView));
        assertEq(abi.encode(decoded), encoded);
        assertEq(bytes(decoded.task.terms.input.uri).length, 2048);
        assertEq(bytes(decoded.result.value.artifact.uri).length, 2048);
        assertEq(bytes(decoded.receipt.value.validation.value.evidence.uri).length, 2048);
        assertEq(decoded.receipt.value.validation.value.signature.length, 65);
        assertEq(decoded.receipt.value.paidAtoms, 50);
        assertEq(decoded.receipt.value.refundAtoms, 50);
        assertEq(decoded.receipt.value.reason, P.REASON_SUCCESS);
        assertEq(market.readCredit(PAYOUT) + market.readCredit(REQUESTER), 100);
        assertTrue(
            market.isNonceUsed(
                address(contractOwner),
                keccak256(
                    "BidPermit(uint64 taskId,address identityRegistry,uint256 agentId,address owner,address executionSigner,address payout,uint96 bidAtoms,bytes32 profileDigest,uint256 nonce,uint64 expiry)"
                ),
                42
            )
        );
        emit log_named_uint("maximumTaskViewAbiBytes", encoded.length);
        emit log_named_uint(
            "maximumSettlementReceiptAbiBytes", abi.encode(decoded.receipt.value).length
        );
    }

    function testMaximumWireRecordsAndContractSignature() public {
        runMaximumWireScenario();
    }

    function testMaximumWireTransactionGasLimits() public {
        uint256[4] memory measured = runMaximumWireScenario();
        for (uint256 index; index < measured.length; ++index) {
            assertLt(measured[index], 30_000_000);
        }
    }

    function awardAndAccept(
        P.TaskRef memory ref,
        uint256 agentId,
        uint64 biddingClose,
        uint96 ownReserve
    ) internal {
        directBid(ref.taskId, agentId);
        vm.warp(biddingClose);
        market.allocateTask(ref);
        vm.prank(SIGNER);
        market.acceptAward(P.ExecutionRef(ref, 1), ownReserve);
    }

    function testDepthTwoSettlementOnlyUpdatesImmediateParent() public {
        freshProductionMarket();
        P.TaskTerms memory rootTerms = terms();
        rootTerms.budgetAtoms = 400;
        rootTerms.alphaDen = 400;
        rootTerms.resultBy = 6000;
        rootTerms.validationBy = 7000;
        vm.prank(REQUESTER);
        P.TaskRef memory root = market.createTask{value: 400}(rootTerms);
        awardAndAccept(root, 7, 1200, 20);
        P.TaskTerms memory childTerms = terms();
        childTerms.refundAddress = SIGNER;
        childTerms.biddingClose = 1500;
        childTerms.allocationBy = 1600;
        childTerms.acceptBy = 1720;
        childTerms.resultBy = 4000;
        childTerms.validationBy = 4500;
        vm.prank(SIGNER);
        P.TaskRef memory child = market.createChildTask{value: 100}(root, childTerms);
        awardAndAccept(child, 9, 1500, 10);
        P.TaskTerms memory grandchildTerms = terms();
        grandchildTerms.refundAddress = SIGNER;
        grandchildTerms.budgetAtoms = 30;
        grandchildTerms.alphaDen = 30;
        grandchildTerms.biddingClose = 1900;
        grandchildTerms.allocationBy = 2000;
        grandchildTerms.acceptBy = 2120;
        grandchildTerms.resultBy = 2400;
        grandchildTerms.validationBy = 2600;
        grandchildTerms.delegation.maxChildren = 0;
        vm.prank(SIGNER);
        P.TaskRef memory grandchild = market.createChildTask{value: 30}(child, grandchildTerms);
        vm.warp(6000);
        market.expireTask(root);
        P.SettlementReceipt memory rootReceipt = market.readSettlement(root).value;
        bytes memory rootBeforeGrandchild = abi.encode(market.readTask(root));
        market.expireTask(grandchild);
        assertEq(abi.encode(market.readTask(root)), rootBeforeGrandchild);
        P.TaskView memory childAfter = market.readTask(child);
        assertEq(childAfter.activeChildren, 0);
        assertEq(childAfter.reservedChildBudgets, 0);
        assertEq(childAfter.childrenCreated, 1);
        market.expireTask(child);
        P.TaskView memory rootAfter = market.readTask(root);
        assertEq(rootAfter.activeChildren, 0);
        assertEq(rootAfter.reservedChildBudgets, 0);
        assertEq(rootAfter.childrenCreated, 1);
        assertEq(abi.encode(rootAfter.receipt.value), abi.encode(rootReceipt));
        assertEq(rootAfter.status, P.STATE_SETTLED);
        assertEq(market.readCredit(REQUESTER), 400);
        assertEq(market.readCredit(SIGNER), 130);
        vm.expectRevert(errorBytes(P.ALREADY_SETTLED));
        market.expireTask(grandchild);
        assertEq(market.readCredit(SIGNER), 130);
    }
}

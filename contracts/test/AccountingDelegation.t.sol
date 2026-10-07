// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {MarketFixtureBase} from "./MarketLifecycle.t.sol";
import {ProtocolTypes as P} from "../src/ProtocolTypes.sol";
import {IAgentLanceMarket} from "../src/IAgentLanceMarket.sol";
import {Vm} from "forge-std/Vm.sol";

contract AccountingDelegationTest is MarketFixtureBase {
    function childTerms(uint96 budget, uint64 validationBy)
        internal
        view
        returns (P.TaskTerms memory value)
    {
        value = terms();
        value.budgetAtoms = budget;
        value.alphaDen = 60;
        value.refundAddress = SIGNER;
        value.biddingClose = 1500;
        value.allocationBy = 1600;
        value.acceptBy = 1720;
        value.resultBy = 2000;
        value.validationBy = validationBy;
        value.delegation = P.DelegationLimits(2, 0);
    }

    function projectedChild(uint64 id, uint96 budget, uint96 paid, uint8 status)
        internal
        view
        returns (P.TaskView memory value)
    {
        value.task = spec(id);
        value.task.parentRef = P.OptionalTaskRef(true, taskRef(1));
        value.task.rootRef = taskRef(1);
        value.task.requester = SIGNER;
        value.task.depth = 1;
        value.task.terms = childTerms(budget, 2800);
        value.status = status;
        if (status != P.STATE_OPEN) {
            value.allocation = P.OptionalAllocation(true, allocation(id, 9, paid));
            value.result = P.OptionalResultCommitment(true, result(id));
        }
        if (status == P.STATE_SETTLED) {
            value.receipt.present = true;
            value.receipt.value.reason = paid == 0 ? P.REASON_VALIDATION_FAILED : P.REASON_SUCCESS;
        }
    }

    function seedDelegation(string memory json, string memory path) internal {
        P.TaskView memory parent;
        parent.task = spec(1);
        parent.task.terms.resultBy = uint64(numberAt(json, string.concat(path, ".resultBy")));
        parent.task.terms.validationBy = 4000;
        parent.task.terms.alphaDen = 200;
        parent.task.depth = uint32(vm.parseJsonUint(json, string.concat(path, ".depth")));
        parent.task.terms.delegation = P.DelegationLimits(
            uint32(vm.parseJsonUint(json, string.concat(path, ".maxDepth"))),
            uint32(vm.parseJsonUint(json, string.concat(path, ".maxChildren")))
        );
        parent.status = stateCode(textAt(json, string.concat(path, ".state")));
        parent.allocation = P.OptionalAllocation(
            true, allocation(1, 7, uint96(numberAt(json, string.concat(path, ".price"))))
        );
        parent.ownWorkReserveAtoms = uint96(numberAt(json, string.concat(path, ".ownReserve")));
        parent.reservedChildBudgets =
            uint96(numberAt(json, string.concat(path, ".reservedChildren")));
        parent.committedChildPayouts = uint96(numberAt(json, string.concat(path, ".paidChildren")));
        parent.childrenCreated =
            uint32(vm.parseJsonUint(json, string.concat(path, ".childrenCreated")));
        parent.activeChildren =
            uint32(vm.parseJsonUint(json, string.concat(path, ".activeChildren")));
        if (parent.status == P.STATE_SETTLED) {
            parent.receipt.present = true;
            parent.receipt.value.reason = P.REASON_EXECUTION_TIMEOUT;
        }
        market.seedTask(parent);
        market.seedBid(1, bid(1, 7));
        uint256 childPaid = numberAt(json, string.concat(path, ".childPayout"));
        uint256 childRefund = numberAt(json, string.concat(path, ".childRefund"));
        uint256 childEscrow = numberAt(json, string.concat(path, ".childEscrow"));
        if (parent.activeChildren != 0) {
            market.seedTask(projectedChild(2, uint96(childEscrow), 0, P.STATE_SUBMITTED));
            market.seedBid(2, bid(2, 9));
        } else if (childPaid + childRefund != 0) {
            market.seedTask(
                projectedChild(
                    2, uint96(childPaid + childRefund), uint96(childPaid), P.STATE_SETTLED
                )
            );
        }
        uint256 parentPaid = numberAt(json, string.concat(path, ".parentPayoutCredit"));
        uint256 parentRefund = numberAt(json, string.concat(path, ".parentRefundCredit"));
        uint256 parentEscrow = numberAt(json, string.concat(path, ".parentEscrow"));
        market.seedCredit(REQUESTER, parentRefund);
        market.seedCredit(PAYOUT, parentPaid);
        market.seedCredit(SIGNER, childRefund);
        market.seedCredit(CHILD_PAYOUT, childPaid);
        uint256 credit = parentPaid + parentRefund + childPaid + childRefund;
        uint256 escrow = parentEscrow + childEscrow;
        market.seedAccounting(credit + escrow, escrow, credit, 0);
        vm.deal(address(market), credit + escrow);
    }

    function checkDelegationProjection(string memory json, string memory path, uint256 managerFunds)
        internal
        view
    {
        P.TaskView memory parent = market.readTask(taskRef(1));
        assertEq(parent.status, stateCode(textAt(json, string.concat(path, ".state"))));
        assertEq(
            parent.reservedChildBudgets, numberAt(json, string.concat(path, ".reservedChildren"))
        );
        assertEq(parent.committedChildPayouts, numberAt(json, string.concat(path, ".paidChildren")));
        assertEq(
            parent.childrenCreated, vm.parseJsonUint(json, string.concat(path, ".childrenCreated"))
        );
        assertEq(
            parent.activeChildren, vm.parseJsonUint(json, string.concat(path, ".activeChildren"))
        );
        assertEq(parent.ownWorkReserveAtoms, numberAt(json, string.concat(path, ".ownReserve")));
        assertEq(
            parent.allocation.value.reservedAtoms, numberAt(json, string.concat(path, ".price"))
        );
        assertEq(parent.task.depth, vm.parseJsonUint(json, string.concat(path, ".depth")));
        assertEq(
            parent.task.terms.delegation.maxDepth,
            vm.parseJsonUint(json, string.concat(path, ".maxDepth"))
        );
        assertEq(
            parent.task.terms.delegation.maxChildren,
            vm.parseJsonUint(json, string.concat(path, ".maxChildren"))
        );
        assertEq(parent.task.terms.resultBy, numberAt(json, string.concat(path, ".resultBy")));
        assertEq(managerFunds, numberAt(json, string.concat(path, ".managerFunds")));
        assertEq(
            parent.status == P.STATE_SETTLED ? 0 : parent.task.terms.budgetAtoms,
            numberAt(json, string.concat(path, ".parentEscrow"))
        );
        assertEq(
            market.readCredit(PAYOUT), numberAt(json, string.concat(path, ".parentPayoutCredit"))
        );
        assertEq(
            market.readCredit(REQUESTER), numberAt(json, string.concat(path, ".parentRefundCredit"))
        );
        assertEq(
            market.readCredit(CHILD_PAYOUT), numberAt(json, string.concat(path, ".childPayout"))
        );
        assertEq(market.readCredit(SIGNER), numberAt(json, string.concat(path, ".childRefund")));
        (uint64 count,,,,) = market.readAccounting();
        uint256 childEscrow;
        for (uint64 id = 2; id <= count; id++) {
            P.TaskView memory child = market.readTask(taskRef(id));
            if (child.status != P.STATE_SETTLED) childEscrow += child.task.terms.budgetAtoms;
        }
        assertEq(childEscrow, numberAt(json, string.concat(path, ".childEscrow")));
    }

    function runDelegationGolden(uint256 index) internal {
        string memory json = vm.readFile("specs/fixtures/delegation.json");
        string memory path = string.concat(".cases[", vm.toString(index), "]");
        string memory initial = string.concat(path, ".initial");
        string memory action = string.concat(path, ".action");
        seedDelegation(json, initial);
        bytes32 operation = keccak256(bytes(textAt(json, string.concat(action, ".operation"))));
        uint256 funds = numberAt(json, string.concat(initial, ".managerFunds"));
        bytes memory payload;
        uint256 value;
        address caller = OTHER;
        bool creation =
            operation == keccak256("createChildTask") || operation == keccak256("retryChild");
        if (creation) {
            vm.warp(1400);
            P.TaskTerms memory child = childTerms(
                uint96(numberAt(json, string.concat(action, ".budget"))),
                uint64(numberAt(json, string.concat(action, ".childValidationBy")))
            );
            if (vm.keyExistsJson(json, string.concat(action, ".childMaxDepth"))) {
                child.delegation.maxDepth =
                    uint32(vm.parseJsonUint(json, string.concat(action, ".childMaxDepth")));
            }
            (uint64 count,,,,) = market.readAccounting();
            if (operation == keccak256("retryChild") && count == 2) {
                child.retryOf = P.OptionalTaskRef(true, taskRef(2));
            }
            if (
                vm.keyExistsJson(json, string.concat(action, ".retryOutcome"))
                    || vm.keyExistsJson(json, string.concat(action, ".retryParent"))
            ) {
                P.TaskView memory prior = projectedChild(2, 30, 25, P.STATE_SETTLED);
                if (vm.keyExistsJson(json, string.concat(action, ".retryParent"))) {
                    prior.task.parentRef.value.taskId = 99;
                    prior.receipt.value.reason = P.REASON_VALIDATION_FAILED;
                }
                market.seedTask(prior);
                child.retryOf = P.OptionalTaskRef(true, taskRef(2));
            }
            if (index == 14) {
                vm.expectRevert(errorBytes(P.DEPTH_LIMIT));
                market.projectChildEnvelope(1, child);
                checkDelegationProjection(json, string.concat(path, ".expected"), funds);
                return;
            }
            value = vm.keyExistsJson(json, string.concat(action, ".valueAtoms"))
                ? numberAt(json, string.concat(action, ".valueAtoms"))
                : child.budgetAtoms;
            caller = actor(textAt(json, string.concat(action, ".actor")));
            payload = abi.encodeCall(IAgentLanceMarket.createChildTask, (taskRef(1), child));
        } else if (operation == keccak256("expireParent")) {
            vm.warp(numberAt(json, string.concat(initial, ".resultBy")));
            payload = abi.encodeCall(IAgentLanceMarket.expireTask, (taskRef(1)));
        } else {
            bytes32 reason = keccak256(bytes(textAt(json, string.concat(action, ".reason"))));
            if (reason == keccak256("SUCCESS")) {
                vm.warp(2100);
                P.TaskView memory child = market.readTask(taskRef(2));
                child.allocation.value.reservedAtoms =
                    uint96(numberAt(json, string.concat(action, ".price")));
                market.seedTask(child);
                payload = abi.encodeCall(
                    IAgentLanceMarket.settleVerdict, (signVerdict(verdict(2, 9, true)))
                );
            } else if (reason == keccak256("UNALLOCATED")) {
                vm.warp(1500);
                market.seedTask(
                    projectedChild(
                        2,
                        uint96(numberAt(json, string.concat(initial, ".childEscrow"))),
                        0,
                        P.STATE_OPEN
                    )
                );
                market.clearBids(2);
                payload = abi.encodeCall(IAgentLanceMarket.allocateTask, (taskRef(2)));
            } else {
                vm.warp(3100);
                payload = abi.encodeCall(IAgentLanceMarket.expireTask, (taskRef(2)));
            }
        }
        vm.recordLogs();
        vm.prank(caller);
        (bool success, bytes memory returned) = address(market).call{value: value}(payload);
        string memory error = textAt(json, string.concat(path, ".error"));
        assertEq(success, bytes(error).length == 0, textAt(json, string.concat(path, ".id")));
        if (!success) {
            assertEq(
                returned,
                errorBytes(
                    uint16(
                        vm.parseJsonUint(
                            vm.readFile("specs/catalog.json"), string.concat(".errors.", error)
                        )
                    )
                )
            );
        }
        Vm.Log[] memory logs = vm.getRecordedLogs();
        string[] memory expectedEvents =
            vm.parseJsonStringArray(json, string.concat(path, ".events"));
        assertEq(logs.length, expectedEvents.length);
        for (uint256 i; i < expectedEvents.length; i++) {
            assertEq(logs[i].topics[0], eventTopic(expectedEvents[i]));
        }
        if (success && creation) funds -= numberAt(json, string.concat(action, ".budget"));
        checkDelegationProjection(json, string.concat(path, ".expected"), funds);
    }

    function testDelegationCreateChild() public {
        runDelegationGolden(0);
    }

    function testDelegationEnvelopeExactBoundary() public {
        runDelegationGolden(1);
    }

    function testDelegationSuccessReleasesUnused() public {
        runDelegationGolden(2);
    }

    function testDelegationUnallocatedReleasesEnvelope() public {
        runDelegationGolden(3);
    }

    function testDelegationRetryConsumesNewSlot() public {
        runDelegationGolden(4);
    }

    function testDelegationParentFailsAfterChildPaid() public {
        runDelegationGolden(5);
    }

    function testDelegationLateChildSettlement() public {
        runDelegationGolden(6);
    }

    function testDelegationEnvelopeOneOver() public {
        runDelegationGolden(7);
    }

    function testDelegationDeadlineOneOver() public {
        runDelegationGolden(8);
    }

    function testDelegationLifetimeCount() public {
        runDelegationGolden(9);
    }

    function testDelegationDepthCap() public {
        runDelegationGolden(10);
    }

    function testDelegationParentNotRunning() public {
        runDelegationGolden(11);
    }

    function testDelegationWrongManager() public {
        runDelegationGolden(12);
    }

    function testDelegationNoParentAdvance() public {
        runDelegationGolden(13);
    }

    function testDelegationChildPermissionsCannotExpand() public {
        runDelegationGolden(14);
    }

    function testDelegationRetrySuccessRejected() public {
        runDelegationGolden(15);
    }

    function testDelegationRetryCrossParentRejected() public {
        runDelegationGolden(16);
    }
}

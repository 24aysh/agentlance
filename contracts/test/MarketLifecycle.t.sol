// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {MarketTestBase} from "./support/MarketTestBase.sol";
import {ProtocolTypes as P} from "../src/ProtocolTypes.sol";
import {IAgentLanceMarket} from "../src/IAgentLanceMarket.sol";
import {Vm} from "forge-std/Vm.sol";

abstract contract MarketFixtureBase is MarketTestBase {
    function textAt(string memory json, string memory path) internal pure returns (string memory) {
        bytes memory decoded = vm.parseJson(json, path);
        return decoded.length <= 32 ? "" : abi.decode(decoded, (string));
    }

    function numberAt(string memory json, string memory path) internal pure returns (uint256) {
        return vm.parseUint(vm.parseJsonString(json, path));
    }

    function stateCode(string memory name) internal pure returns (uint8) {
        if (keccak256(bytes(name)) == keccak256("OPEN")) return P.STATE_OPEN;
        if (keccak256(bytes(name)) == keccak256("AWARDED")) return P.STATE_AWARDED;
        if (keccak256(bytes(name)) == keccak256("RUNNING")) return P.STATE_RUNNING;
        if (keccak256(bytes(name)) == keccak256("SUBMITTED")) return P.STATE_SUBMITTED;
        return P.STATE_SETTLED;
    }

    function actor(string memory name) internal view returns (address) {
        bytes32 key = keccak256(bytes(name));
        if (key == keccak256("REQUESTER")) return REQUESTER;
        if (key == keccak256("OWNER")) return owner;
        if (key == keccak256("SIGNER")) return SIGNER;
        if (key == keccak256("PAYOUT")) return PAYOUT;
        return OTHER;
    }

    function seedLifecycle(string memory json, string memory path) internal {
        if (keccak256(bytes(textAt(json, string.concat(path, ".state")))) == keccak256("ABSENT")) {
            return;
        }
        P.TaskView memory value;
        value.task = spec(1);
        value.status = stateCode(textAt(json, string.concat(path, ".state")));
        value.task.terms.biddingClose = uint64(numberAt(json, string.concat(path, ".biddingClose")));
        value.task.terms.allocationBy = uint64(numberAt(json, string.concat(path, ".allocationBy")));
        value.task.terms.acceptBy = uint64(numberAt(json, string.concat(path, ".acceptBy")));
        value.task.terms.resultBy = uint64(numberAt(json, string.concat(path, ".resultBy")));
        value.task.terms.validationBy = uint64(numberAt(json, string.concat(path, ".validationBy")));
        value.task.terms.budgetAtoms = uint96(numberAt(json, string.concat(path, ".budgetAtoms")));
        value.ownWorkReserveAtoms =
            uint96(numberAt(json, string.concat(path, ".ownWorkReserveAtoms")));
        value.reservedChildBudgets =
            uint96(numberAt(json, string.concat(path, ".reservedChildBudgets")));
        value.committedChildPayouts =
            uint96(numberAt(json, string.concat(path, ".committedChildPayouts")));
        value.childrenCreated =
            uint32(vm.parseJsonUint(json, string.concat(path, ".childrenCreated")));
        value.activeChildren =
            uint32(vm.parseJsonUint(json, string.concat(path, ".activeChildren")));
        if (vm.parseJsonBool(json, string.concat(path, ".hasWinner"))) {
            value.allocation = P.OptionalAllocation(
                true,
                allocation(1, 7, uint96(numberAt(json, string.concat(path, ".reservedAtoms"))))
            );
        }
        if (vm.parseJsonBool(json, string.concat(path, ".hasResult"))) {
            value.result = P.OptionalResultCommitment(true, result(1));
        }
        string memory terminal = textAt(json, string.concat(path, ".terminalReason"));
        if (bytes(terminal).length != 0) {
            value.receipt.present = true;
            value.receipt.value.reason = uint8(
                vm.parseJsonUint(
                    vm.readFile("specs/catalog.json"), string.concat(".terminalReasons.", terminal)
                )
            );
        }
        market.seedTask(value);
        if (vm.parseJsonUint(json, string.concat(path, ".bidCount")) != 0) {
            market.seedBid(1, bid(1, 7));
        }
        uint256 payout = numberAt(json, string.concat(path, ".payoutCredit"));
        uint256 refund = numberAt(json, string.concat(path, ".refundCredit"));
        market.seedCredit(PAYOUT, payout);
        market.seedCredit(REQUESTER, refund);
        market.seedCheckpoint(
            7,
            "structured-output-v1",
            9,
            uint64(numberAt(json, string.concat(path, ".successes"))),
            uint64(numberAt(json, string.concat(path, ".failures")))
        );
        market.seedAccounting(
            value.task.terms.budgetAtoms,
            numberAt(json, string.concat(path, ".escrowAtoms")),
            payout + refund,
            0
        );
        vm.deal(address(market), value.task.terms.budgetAtoms);
    }

    function lifecyclePayload(string memory json, string memory action)
        internal
        view
        returns (bytes memory payload)
    {
        string memory command = textAt(json, string.concat(action, ".command"));
        bytes32 name = keccak256(bytes(command));
        string memory args = string.concat(action, ".arguments");
        if (name == keccak256("createTask")) {
            return abi.encodeCall(IAgentLanceMarket.createTask, (terms()));
        }
        if (name == keccak256("submitBid")) {
            P.OptionalBidPermit memory permit;
            P.OptionalSignature memory signature;
            return abi.encodeCall(IAgentLanceMarket.submitBid, (offer(1, 7), permit, signature));
        }
        if (name == keccak256("allocateTask")) {
            return abi.encodeCall(IAgentLanceMarket.allocateTask, (taskRef(1)));
        }
        if (name == keccak256("acceptAward")) {
            return abi.encodeCall(
                IAgentLanceMarket.acceptAward,
                (
                    P.ExecutionRef(taskRef(1), 1),
                    uint96(numberAt(json, string.concat(args, ".ownWorkReserveAtoms")))
                )
            );
        }
        if (name == keccak256("submitResult")) {
            return abi.encodeCall(
                IAgentLanceMarket.submitResult, (P.ExecutionRef(taskRef(1), 1), result(1).artifact)
            );
        }
        if (name == keccak256("settleVerdict")) {
            P.ValidationRecord memory record = verdict(1, 7, true);
            if (vm.keyExistsJson(json, string.concat(args, ".verdict"))) {
                record.verdict = keccak256(bytes(textAt(json, string.concat(args, ".verdict"))))
                    == keccak256("PASS")
                    ? 1
                    : 0;
            }
            if (vm.keyExistsJson(json, string.concat(args, ".resultMatches"))) {
                record.resultDigest = bytes32(uint256(900));
            }
            if (vm.keyExistsJson(json, string.concat(args, ".policyMatches"))) {
                record.validationPolicyDigest = bytes32(uint256(900));
            }
            return abi.encodeCall(IAgentLanceMarket.settleVerdict, (signVerdict(record)));
        }
        if (name == keccak256("expireTask")) {
            return abi.encodeCall(IAgentLanceMarket.expireTask, (taskRef(1)));
        }
        if (name == keccak256("cancelTask")) {
            return abi.encodeCall(IAgentLanceMarket.cancelTask, (taskRef(1)));
        }
        return abi.encodeCall(
            IAgentLanceMarket.withdrawCredit,
            (address(0x999), numberAt(json, string.concat(args, ".amountAtoms")))
        );
    }

    function checkLifecycleProjection(string memory json, string memory path) internal view {
        (uint64 count,,,,) = market.readAccounting();
        if (count == 0) {
            assertEq(textAt(json, string.concat(path, ".state")), "ABSENT");
            return;
        }
        P.TaskView memory value = market.readTask(taskRef(1));
        assertEq(value.status, stateCode(textAt(json, string.concat(path, ".state"))));
        assertEq(value.task.createdAt, numberAt(json, string.concat(path, ".createdAt")));
        assertEq(
            value.allocation.value.reservedAtoms,
            numberAt(json, string.concat(path, ".reservedAtoms"))
        );
        assertEq(
            value.status == P.STATE_SETTLED ? 0 : value.task.terms.budgetAtoms,
            numberAt(json, string.concat(path, ".escrowAtoms"))
        );
        assertEq(market.readCredit(PAYOUT), numberAt(json, string.concat(path, ".payoutCredit")));
        assertEq(market.readCredit(REQUESTER), numberAt(json, string.concat(path, ".refundCredit")));
        assertEq(
            market.readBid(taskRef(1), agentRef(7)).present ? 1 : 0,
            vm.parseJsonUint(json, string.concat(path, ".bidCount"))
        );
        assertEq(
            value.allocation.value.winner.present,
            vm.parseJsonBool(json, string.concat(path, ".hasWinner"))
        );
        assertEq(value.result.present, vm.parseJsonBool(json, string.concat(path, ".hasResult")));
        assertEq(
            value.ownWorkReserveAtoms, numberAt(json, string.concat(path, ".ownWorkReserveAtoms"))
        );
        assertEq(
            value.reservedChildBudgets, numberAt(json, string.concat(path, ".reservedChildBudgets"))
        );
        assertEq(
            value.committedChildPayouts,
            numberAt(json, string.concat(path, ".committedChildPayouts"))
        );
        assertEq(
            value.childrenCreated, vm.parseJsonUint(json, string.concat(path, ".childrenCreated"))
        );
        assertEq(
            value.activeChildren, vm.parseJsonUint(json, string.concat(path, ".activeChildren"))
        );
        (uint64 successes, uint64 failures) =
            market.readCounters(agentRef(7), "structured-output-v1", 100);
        assertEq(successes, numberAt(json, string.concat(path, ".successes")));
        assertEq(failures, numberAt(json, string.concat(path, ".failures")));
        string memory reason = textAt(json, string.concat(path, ".terminalReason"));
        assertEq(value.receipt.present, bytes(reason).length != 0);
        if (bytes(reason).length != 0) {
            assertEq(
                value.receipt.value.reason,
                vm.parseJsonUint(
                    vm.readFile("specs/catalog.json"), string.concat(".terminalReasons.", reason)
                )
            );
        }
    }

    function runLifecycleGolden(uint256 index) internal {
        string memory json = vm.readFile("specs/fixtures/lifecycle.json");
        string memory path = string.concat(".cases[", vm.toString(index), "]");
        seedLifecycle(json, string.concat(path, ".initial"));
        vm.warp(numberAt(json, string.concat(path, ".initial.now")));
        vm.roll(26);
        string memory action = string.concat(path, ".action");
        string memory args = string.concat(action, ".arguments");
        uint256 value = vm.keyExistsJson(json, string.concat(args, ".valueAtoms"))
            ? numberAt(json, string.concat(args, ".valueAtoms"))
            : 0;
        if (
            vm.keyExistsJson(json, string.concat(args, ".receiverAccepts"))
                && !vm.parseJsonBool(json, string.concat(args, ".receiverAccepts"))
        ) vm.etch(address(0x999), hex"60006000fd");
        bytes memory payload = lifecyclePayload(json, action);
        vm.recordLogs();
        vm.prank(actor(textAt(json, string.concat(action, ".actor"))));
        (bool success, bytes memory returned) = address(market).call{value: value}(payload);
        string memory error = textAt(json, string.concat(path, ".error"));
        assertEq(success, bytes(error).length == 0, textAt(json, string.concat(path, ".id")));
        if (!success) {
            if (index == 33) {
                assertEq(returned.length, 0); // Frozen nonpayable ABI entry rejection is not ProtocolError(WRONG_VALUE).
            } else {
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
        }
        Vm.Log[] memory logs = vm.getRecordedLogs();
        string[] memory expectedEvents =
            vm.parseJsonStringArray(json, string.concat(path, ".events"));
        assertEq(logs.length, expectedEvents.length);
        for (uint256 i; i < expectedEvents.length; i++) {
            assertEq(logs[i].topics[0], eventTopic(expectedEvents[i]));
        }
        checkLifecycleProjection(json, string.concat(path, ".expected"));
    }

    function eventTopic(string memory name) internal pure returns (bytes32) {
        bytes32 key = keccak256(bytes(name));
        if (key == keccak256("TaskCreated")) return IAgentLanceMarket.TaskCreated.selector;
        if (key == keccak256("BidAccepted")) return IAgentLanceMarket.BidAccepted.selector;
        if (key == keccak256("TaskAwarded")) return IAgentLanceMarket.TaskAwarded.selector;
        if (key == keccak256("AwardAccepted")) return IAgentLanceMarket.AwardAccepted.selector;
        if (key == keccak256("ResultSubmitted")) return IAgentLanceMarket.ResultSubmitted.selector;
        if (key == keccak256("TaskSettled")) return IAgentLanceMarket.TaskSettled.selector;
        return IAgentLanceMarket.CreditWithdrawn.selector;
    }
}

contract MarketLifecycleTest is MarketFixtureBase {
    function testLifecycleCreateRoot() public {
        runLifecycleGolden(0);
    }

    function testLifecycleAcceptBid() public {
        runLifecycleGolden(1);
    }

    function testLifecycleAllocatePositive() public {
        runLifecycleGolden(2);
    }

    function testLifecycleAcceptAward() public {
        runLifecycleGolden(3);
    }

    function testLifecycleSubmitResult() public {
        runLifecycleGolden(4);
    }

    function testLifecycleSuccess() public {
        runLifecycleGolden(5);
    }

    function testLifecycleFailedValidation() public {
        runLifecycleGolden(6);
    }

    function testLifecycleUnallocated() public {
        runLifecycleGolden(7);
    }

    function testLifecycleAllocationExpired() public {
        runLifecycleGolden(8);
    }

    function testLifecycleNoShow() public {
        runLifecycleGolden(9);
    }

    function testLifecycleExecutionTimeout() public {
        runLifecycleGolden(10);
    }

    function testLifecycleValidatorTimeout() public {
        runLifecycleGolden(11);
    }

    function testLifecycleCancelled() public {
        runLifecycleGolden(12);
    }

    function testLifecycleDuplicateSettlement() public {
        runLifecycleGolden(13);
    }

    function testLifecycleWrongSigner() public {
        runLifecycleGolden(14);
    }

    function testLifecycleEarlyAllocation() public {
        runLifecycleGolden(15);
    }

    function testLifecycleAllocationAtCutoff() public {
        runLifecycleGolden(16);
    }

    function testLifecycleAcceptAtCutoff() public {
        runLifecycleGolden(17);
    }

    function testLifecycleSubmitAtCutoff() public {
        runLifecycleGolden(18);
    }

    function testLifecycleVerdictAtCutoff() public {
        runLifecycleGolden(19);
    }

    function testLifecycleLateBid() public {
        runLifecycleGolden(20);
    }

    function testLifecycleCancelWithBid() public {
        runLifecycleGolden(21);
    }

    function testLifecycleSubmitBeforeAccept() public {
        runLifecycleGolden(22);
    }

    function testLifecycleActiveChildBlocksResult() public {
        runLifecycleGolden(23);
    }

    function testLifecycleEarlyTimeout() public {
        runLifecycleGolden(24);
    }

    function testLifecycleWrongCreationValue() public {
        runLifecycleGolden(25);
    }

    function testLifecycleWithdrawBlocked() public {
        runLifecycleGolden(26);
    }

    function testLifecycleWithdrawSuccess() public {
        runLifecycleGolden(27);
    }

    function testLifecycleAcceptBeforeCutoff() public {
        runLifecycleGolden(28);
    }

    function testLifecycleSubmitBeforeCutoff() public {
        runLifecycleGolden(29);
    }

    function testLifecycleVerdictBeforeCutoff() public {
        runLifecycleGolden(30);
    }

    function testLifecycleWrongResultBinding() public {
        runLifecycleGolden(31);
    }

    function testLifecycleWrongPolicyBinding() public {
        runLifecycleGolden(32);
    }

    function testLifecycleNonzeroCallValue() public {
        runLifecycleGolden(33);
    }
}

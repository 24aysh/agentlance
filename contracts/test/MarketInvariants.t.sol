// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {Test} from "forge-std/Test.sol";
import {AgentLanceMarket} from "../src/AgentLanceMarket.sol";
import {ProtocolTypes as P} from "../src/ProtocolTypes.sol";
import {ProtocolSignatures as Signatures} from "../src/ProtocolSignatures.sol";
import {TestIdentityRegistry} from "./support/TestIdentityRegistry.sol";

contract MarketHandler is Test {
    AgentLanceMarket public market;
    TestIdentityRegistry public registry;
    uint256 public successfulTransitions;
    uint256 public actions;
    uint256 public unexpectedRejections;
    uint256[9] public reachedReasons;
    mapping(uint64 => bytes32) public specHashes;
    mapping(uint64 => bytes32) public bidHashes;
    uint256 private constant VALIDATOR_KEY = 0xB0B;

    constructor() {
        registry = new TestIdentityRegistry();
        market = new AgentLanceMarket(address(registry), vm.addr(VALIDATOR_KEY));
        registry.setIdentity(7, address(this), address(this));
        vm.deal(address(this), 100000 ether);
    }
    receive() external payable {}

    function ref(uint64 id) public view returns (P.TaskRef memory) {
        return P.TaskRef(block.chainid, address(market), id);
    }

    function agent() public view returns (P.AgentRef memory) {
        return P.AgentRef(block.chainid, address(registry), 7);
    }

    function create(uint256 seed) public {
        actions++;
        P.TaskTerms memory terms;
        terms.policyVersion = 1;
        terms.refundAddress = address(this);
        terms.asset = P.Asset("NATIVE", "MON", 18);
        terms.budgetAtoms = uint96(50 + seed % 100);
        terms.input = P.ContentRef("ipfs://input", bytes32(0));
        terms.outputSchema = P.ContentRef("ipfs://schema", bytes32(0));
        terms.taskFamily = "structured-output-v1";
        terms.alphaNum = 1;
        terms.alphaDen = 100;
        terms.biddingClose = uint64(block.timestamp + 10);
        terms.allocationBy = uint64(block.timestamp + 20);
        terms.acceptBy = uint64(block.timestamp + 140);
        terms.resultBy = uint64(block.timestamp + 160);
        terms.validationBy = uint64(block.timestamp + 180);
        terms.validator = vm.addr(VALIDATOR_KEY);
        terms.validationPolicy = P.ContentRef("ipfs://policy", bytes32(0));
        try market.createTask{value: terms.budgetAtoms}(terms) returns (P.TaskRef memory task) {
            successfulTransitions++;
            specHashes[task.taskId] = keccak256(abi.encode(market.readTask(task).task));
        } catch {
            unexpectedRejections++;
        }
    }

    function choose(uint256 seed) internal view returns (P.TaskView memory task) {
        (uint64 count,,,,) = market.readAccounting();
        return market.readTask(ref(uint64(1 + seed % count)));
    }

    function bid(uint256 seed) public {
        actions++;
        P.TaskView memory task = choose(seed);
        if (
            task.status != P.STATE_OPEN || block.timestamp >= task.task.terms.biddingClose
                || market.readBid(task.task.taskRef, agent()).present
        ) {
            create(seed);
            (uint64 count,,,,) = market.readAccounting();
            task = market.readTask(ref(count));
        }
        P.BidOffer memory offer = P.BidOffer(
            task.task.taskRef,
            agent(),
            uint96(seed % (task.task.terms.budgetAtoms + 1)),
            address(this),
            address(this),
            bytes32(seed)
        );
        P.OptionalBidPermit memory permit;
        P.OptionalSignature memory signature;
        try market.submitBid(offer, permit, signature) {
            successfulTransitions++;
            bidHashes[task.task.taskRef.taskId] =
                keccak256(abi.encode(market.readBid(task.task.taskRef, agent()).value));
        } catch {
            unexpectedRejections++;
        }
    }

    function progress(uint256 seed) public {
        actions++;
        P.TaskView memory task = choose(seed);
        if (task.status == P.STATE_SETTLED) {
            actions--;
            create(seed);
            return;
        }
        P.TaskRef memory taskRef = task.task.taskRef;
        if (task.status == P.STATE_OPEN) {
            if (block.timestamp >= task.task.terms.allocationBy) {
                expire(taskRef);
                return;
            }
            if (
                seed % 3 == 0 && block.timestamp < task.task.terms.biddingClose
                    && !market.readBid(taskRef, agent()).present
            ) {
                try market.cancelTask(taskRef) {
                    successfulTransitions++;
                    recordReason(taskRef);
                } catch {
                    unexpectedRejections++;
                }
            } else {
                if (block.timestamp < task.task.terms.biddingClose) {
                    vm.warp(task.task.terms.biddingClose);
                }
                try market.allocateTask(taskRef) {
                    successfulTransitions++;
                    recordReason(taskRef);
                } catch {
                    unexpectedRejections++;
                }
            }
        } else if (task.status == P.STATE_AWARDED) {
            if (block.timestamp >= task.task.terms.acceptBy || seed % 3 == 0) {
                if (block.timestamp < task.task.terms.acceptBy) vm.warp(task.task.terms.acceptBy);
                expire(taskRef);
            } else {
                try market.acceptAward(P.ExecutionRef(taskRef, 1), 0) {
                    successfulTransitions++;
                } catch {
                    unexpectedRejections++;
                }
            }
        } else if (task.status == P.STATE_RUNNING) {
            if (block.timestamp >= task.task.terms.resultBy || seed % 3 == 0) {
                if (block.timestamp < task.task.terms.resultBy) vm.warp(task.task.terms.resultBy);
                expire(taskRef);
            } else {
                try market.submitResult(
                    P.ExecutionRef(taskRef, 1), P.ContentRef("ipfs://result", bytes32(0))
                ) {
                    successfulTransitions++;
                } catch {
                    unexpectedRejections++;
                }
            }
        } else {
            if (block.timestamp >= task.task.terms.validationBy || seed % 3 == 0) {
                if (block.timestamp < task.task.terms.validationBy) {
                    vm.warp(task.task.terms.validationBy);
                }
                expire(taskRef);
            } else {
                P.ValidationRecord memory record;
                record.schemaVersion = 1;
                record.executionRef = P.ExecutionRef(taskRef, 1);
                record.agentRef = agent();
                record.verdict = uint8(seed % 2);
                record.evidence = P.ContentRef("ipfs://evidence", bytes32(0));
                record.validator = vm.addr(VALIDATOR_KEY);
                record.nonce = taskRef.taskId;
                record.expiry = task.task.terms.validationBy;
                bytes32 digest = Signatures.typedDigest(
                    Signatures.domainSeparator(block.chainid, address(market)),
                    Signatures.verdictStructHash(record)
                );
                (uint8 v, bytes32 r, bytes32 s) = vm.sign(VALIDATOR_KEY, digest);
                record.signature = abi.encodePacked(r, s, v);
                try market.settleVerdict(record) {
                    successfulTransitions++;
                    recordReason(taskRef);
                } catch {
                    unexpectedRejections++;
                }
            }
        }
        vm.roll(block.number + 1);
    }

    function expire(P.TaskRef memory taskRef) internal {
        try market.expireTask(taskRef) {
            successfulTransitions++;
            recordReason(taskRef);
        } catch {
            unexpectedRejections++;
        }
    }

    function recordReason(P.TaskRef memory taskRef) internal {
        P.OptionalSettlementReceipt memory receipt = market.readSettlement(taskRef);
        if (receipt.present) reachedReasons[receipt.value.reason]++;
    }

    function withdraw(uint256 seed) public {
        actions++;
        uint256 credit = market.readCredit(address(this));
        if (credit == 0) {
            actions--;
            create(seed);
            return;
        }
        try market.withdrawCredit(address(this), 1 + seed % credit) {
            successfulTransitions++;
        } catch {
            unexpectedRejections++;
        }
    }
}

contract MarketInvariantsTest is Test {
    MarketHandler private handler;
    AgentLanceMarket private market;

    function setUp() public {
        vm.warp(1000);
        vm.roll(10);
        handler = new MarketHandler();
        market = handler.market();
        handler.create(0);
        bytes4[] memory selectors = new bytes4[](4);
        selectors[0] = MarketHandler.create.selector;
        selectors[1] = MarketHandler.bid.selector;
        selectors[2] = MarketHandler.progress.selector;
        selectors[3] = MarketHandler.withdraw.selector;
        targetSelector(FuzzSelector(address(handler), selectors));
        targetContract(address(handler));
    }

    function invariantConservationImmutabilityAndOnceOnlyCounters() public view {
        (uint64 count, uint256 deposits, uint256 escrow, uint256 credit, uint256 withdrawn) =
            market.readAccounting();
        uint256 sumEscrow;
        uint256 sumPaidAndRefunds;
        uint256 successes;
        uint256 failures;
        for (uint64 id = 1; id <= count; id++) {
            P.TaskView memory task = market.readTask(handler.ref(id));
            assertEq(keccak256(abi.encode(task.task)), handler.specHashes(id));
            P.OptionalBid memory bid = market.readBid(handler.ref(id), handler.agent());
            if (bid.present) assertEq(keccak256(abi.encode(bid.value)), handler.bidHashes(id));
            if (task.status == P.STATE_SETTLED) {
                assertTrue(task.receipt.present);
                P.SettlementReceipt memory receipt = task.receipt.value;
                assertEq(
                    uint256(receipt.paidAtoms) + receipt.refundAtoms, task.task.terms.budgetAtoms
                );
                assertLe(receipt.reservedAtoms, task.task.terms.budgetAtoms);
                assertEq(
                    receipt.paidAtoms,
                    receipt.reason == P.REASON_SUCCESS ? receipt.reservedAtoms : 0
                );
                sumPaidAndRefunds += uint256(receipt.paidAtoms) + receipt.refundAtoms;
                if (receipt.counterEffect == P.EFFECT_SUCCESS) successes++;
                if (receipt.counterEffect == P.EFFECT_FAILURE) failures++;
            } else {
                assertFalse(task.receipt.present);
                sumEscrow += task.task.terms.budgetAtoms;
            }
            if (task.allocation.present && task.allocation.value.winner.present) {
                assertGt(task.allocation.value.winningScore.value, 0);
                assertLe(task.allocation.value.reservedAtoms, task.task.terms.budgetAtoms);
            }
        }
        assertEq(deposits, escrow + credit + withdrawn);
        assertEq(escrow, sumEscrow);
        assertEq(deposits, sumEscrow + sumPaidAndRefunds);
        assertEq(credit, market.readCredit(address(handler)));
        assertGe(address(market).balance, escrow + credit);
        (uint64 recordedSuccesses, uint64 recordedFailures) =
            market.readCounters(handler.agent(), "structured-output-v1", type(uint64).max);
        assertEq(recordedSuccesses, successes);
        assertEq(recordedFailures, failures);
        assertEq(handler.unexpectedRejections(), 0);
        assertGe(handler.successfulTransitions(), handler.actions());
    }

    function afterInvariant() public {
        assertGt(handler.actions(), 1, "campaign must execute beyond setup");
        assertGt(
            handler.successfulTransitions(), 1, "campaign cannot pass by reverting every action"
        );
        emit log_named_uint("successfulTransitions", handler.successfulTransitions());
        emit log_named_uint("attemptedActions", handler.actions());
        for (uint256 reason = 1; reason <= 8; reason++) {
            emit log_named_uint(
                string.concat("terminalReason", vm.toString(reason)), handler.reachedReasons(reason)
            );
        }
    }
}

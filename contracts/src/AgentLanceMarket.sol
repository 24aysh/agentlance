// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {ProtocolTypes as P} from "./ProtocolTypes.sol";
import {IAgentLanceMarket} from "./IAgentLanceMarket.sol";
import {IAgentLanceViews} from "./IAgentLanceViews.sol";
import {ProtocolMath} from "./ProtocolMath.sol";
import {ProtocolSignatures as Signatures} from "./ProtocolSignatures.sol";
import {ContentUri} from "./ContentUri.sol";

/// @notice Immutable native-MON task market. Execution and correctness evaluation remain off-chain.
contract AgentLanceMarket is IAgentLanceMarket, IAgentLanceViews {
    struct StoredBid {
        bool exists;
        P.Bid value;
    }

    struct Checkpoint {
        uint64 blockNumber;
        uint64 successes;
        uint64 failures;
    }

    struct TaskData {
        P.TaskSpec spec;
        uint8 status;
        uint8 topCount;
        bool hasAllocation;
        bool hasResult;
        bool hasReceipt;
        uint32 childrenCreated;
        uint32 activeChildren;
        uint96 ownWorkReserveAtoms;
        uint96 reservedChildBudgets;
        uint96 committedChildPayouts;
        uint256[2] topAgentIds;
        mapping(uint256 => StoredBid) bids;
        P.Allocation allocation;
        P.ResultCommitment result;
        P.SettlementReceipt receipt;
    }

    uint64 internal taskCount;
    bool internal writeEntered;
    mapping(uint64 => TaskData) internal tasks;
    mapping(address => uint256) internal credits;
    mapping(address => mapping(bytes32 => mapping(uint256 => bool))) internal usedNonces;
    mapping(uint256 => mapping(bytes32 => Checkpoint[])) internal histories;
    uint256 internal depositedAtoms;
    uint256 internal escrowTotalAtoms;
    uint256 internal creditTotalAtoms;
    uint256 internal withdrawnAtoms;

    address private immutable identityRegistry;
    address private immutable validator;
    bytes32 private constant FAMILY = keccak256("structured-output-v1");

    constructor(address identityRegistry_, address validator_) {
        requireProtocol(
            identityRegistry_ != address(0) && validator_ != address(0), P.INVALID_RANGE
        );
        requireProtocol(identityRegistry_.code.length != 0, P.IDENTITY_UNAVAILABLE);
        identityRegistry = identityRegistry_;
        validator = validator_;
    }

    modifier writeGuard() {
        requireProtocol(
            block.number <= type(uint64).max && block.timestamp <= type(uint64).max, P.INVALID_RANGE
        );
        requireProtocol(!writeEntered, P.REENTRANCY);
        writeEntered = true;
        _;
        writeEntered = false;
    }

    function requireProtocol(bool condition, uint16 code) internal pure {
        if (!condition) revert ProtocolError(code);
    }

    function requireTaskRef(P.TaskRef memory ref) internal pure {
        requireProtocol(
            ref.chainId != 0 && ref.market != address(0) && ref.taskId != 0, P.INVALID_RANGE
        );
    }

    function requireAgentRef(P.AgentRef memory ref) internal pure {
        requireProtocol(ref.chainId != 0 && ref.identityRegistry != address(0), P.INVALID_RANGE);
    }

    function equalTaskRef(P.TaskRef memory a, P.TaskRef memory b) internal pure returns (bool) {
        return a.chainId == b.chainId && a.market == b.market && a.taskId == b.taskId;
    }

    function equalAgentRef(P.AgentRef memory a, P.AgentRef memory b) internal pure returns (bool) {
        return a.chainId == b.chainId && a.identityRegistry == b.identityRegistry
            && a.agentId == b.agentId;
    }

    function resolveTask(P.TaskRef memory ref) internal view returns (TaskData storage task) {
        requireTaskRef(ref);
        requireProtocol(
            ref.chainId == block.chainid && ref.market == address(this) && ref.taskId <= taskCount,
            P.UNKNOWN_TASK
        );
        return tasks[ref.taskId];
    }

    function requireIdentityNamespace(P.AgentRef memory ref) internal view {
        requireAgentRef(ref);
        requireProtocol(
            ref.chainId == block.chainid && ref.identityRegistry == identityRegistry,
            P.IDENTITY_UNAVAILABLE
        );
    }

    function checkState(TaskData storage task, uint8 expected) internal view {
        requireProtocol(task.status != P.STATE_SETTLED, P.ALREADY_SETTLED);
        requireProtocol(task.status == expected, P.WRONG_STATE);
    }

    function winnerBid(TaskData storage task) internal view returns (P.Bid storage bid) {
        return task.bids[task.allocation.winner.value.agentId].value;
    }

    function checkExecution(
        TaskData storage task,
        P.ExecutionRef memory executionRef,
        uint8 expected
    ) internal view {
        checkState(task, expected);
        requireProtocol(executionRef.awardId == 1, P.WRONG_STATE);
        requireProtocol(msg.sender == winnerBid(task).offer.executionSigner, P.UNAUTHORIZED);
    }

    function requireContent(P.ContentRef memory content) internal pure {
        requireProtocol(ContentUri.isContentUri(content.uri), P.INVALID_ENCODING);
    }

    function requireOptionalTaskRef(P.OptionalTaskRef memory optional) internal pure {
        if (optional.present) {
            requireTaskRef(optional.value);
        } else {
            requireProtocol(
                optional.value.chainId == 0 && optional.value.market == address(0)
                    && optional.value.taskId == 0,
                P.INVALID_ENCODING
            );
        }
    }

    function validateTerms(P.TaskTerms memory terms, uint64 parentId) internal view {
        requireOptionalTaskRef(terms.retryOf);
        requireProtocol(
            terms.refundAddress != address(0) && terms.validator != address(0), P.INVALID_RANGE
        );
        requireProtocol(
            terms.delegation.maxDepth <= 2 && terms.delegation.maxChildren <= 4, P.INVALID_RANGE
        );
        requireContent(terms.input);
        requireContent(terms.outputSchema);
        requireContent(terms.validationPolicy);
        requireProtocol(
            terms.policyVersion == 1 && keccak256(bytes(terms.taskFamily)) == FAMILY
                && terms.validator == validator,
            P.UNSUPPORTED_POLICY
        );
        requireProtocol(
            keccak256(bytes(terms.asset.kind)) == keccak256("NATIVE")
                && keccak256(bytes(terms.asset.symbol)) == keccak256("MON")
                && terms.asset.decimals == 18,
            P.INVALID_ASSET
        );
        requireProtocol(terms.budgetAtoms > 0, P.INVALID_BUDGET);
        requireProtocol(
            terms.alphaNum > 0 && terms.alphaDen > 0
                && ProtocolMath.gcd(terms.alphaNum, terms.alphaDen) == 1,
            P.INVALID_ALPHA
        );
        requireProtocol(
            block.timestamp < terms.biddingClose && terms.biddingClose < terms.allocationBy
                && terms.allocationBy < terms.acceptBy && terms.acceptBy < terms.resultBy
                && terms.resultBy < terms.validationBy
                && terms.acceptBy - terms.allocationBy >= 120,
            P.INVALID_DEADLINES
        );
        requireProtocol(block.number > 0, P.INVALID_RANGE);
        uint32 depth = parentId == 0 ? 0 : tasks[parentId].spec.depth + 1;
        requireProtocol(
            depth != terms.delegation.maxDepth || terms.delegation.maxChildren == 0, P.INVALID_TERMS
        );
        requireProtocol(
            msg.sender != validator && terms.refundAddress != validator, P.VALIDATOR_CONFLICT
        );
        if (parentId != 0) {
            P.Bid storage parentBid = winnerBid(tasks[parentId]);
            requireProtocol(
                parentBid.offer.executionSigner != validator && parentBid.offer.payout != validator,
                P.VALIDATOR_CONFLICT
            );
        }
    }

    function validateChildEnvelope(TaskData storage parent, P.TaskTerms memory terms)
        internal
        view
    {
        P.DelegationLimits storage limits = parent.spec.terms.delegation;
        requireProtocol(
            parent.spec.depth + 1 <= terms.delegation.maxDepth
                && terms.delegation.maxDepth <= limits.maxDepth,
            P.DEPTH_LIMIT
        );
        requireProtocol(
            terms.delegation.maxChildren <= limits.maxChildren
                && parent.childrenCreated < limits.maxChildren,
            P.CHILD_LIMIT
        );
        uint256 available = uint256(parent.allocation.reservedAtoms) - parent.ownWorkReserveAtoms
            - parent.reservedChildBudgets - parent.committedChildPayouts;
        requireProtocol(terms.budgetAtoms <= available, P.ENVELOPE_EXCEEDED);
        requireProtocol(
            uint256(terms.validationBy) + 120 <= parent.spec.terms.resultBy, P.CHILD_DEADLINE
        );
    }

    function validateRetry(P.OptionalTaskRef memory retryOf, uint64 parentId) internal view {
        if (!retryOf.present) return;
        P.TaskRef memory ref = retryOf.value;
        requireProtocol(
            ref.chainId == block.chainid && ref.market == address(this) && ref.taskId <= taskCount,
            P.INVALID_RETRY
        );
        TaskData storage prior = tasks[ref.taskId];
        requireProtocol(
            prior.status == P.STATE_SETTLED && prior.receipt.reason != P.REASON_SUCCESS
                && prior.spec.requester == msg.sender,
            P.INVALID_RETRY
        );
        requireProtocol(prior.spec.parentRef.present == (parentId != 0), P.INVALID_RETRY);
        if (parentId != 0) {
            requireProtocol(
                equalTaskRef(prior.spec.parentRef.value, tasks[parentId].spec.taskRef)
                    && equalTaskRef(prior.spec.rootRef, tasks[parentId].spec.rootRef),
                P.INVALID_RETRY
            );
        }
    }

    function createFundedTask(P.TaskTerms memory terms, uint64 parentId)
        internal
        returns (P.TaskRef memory ref)
    {
        validateTerms(terms, parentId);
        if (parentId != 0) validateChildEnvelope(tasks[parentId], terms);
        validateRetry(terms.retryOf, parentId);
        requireProtocol(msg.value == terms.budgetAtoms, P.WRONG_VALUE);
        requireProtocol(taskCount < type(uint64).max, P.TASK_LIMIT);
        ref = P.TaskRef(block.chainid, address(this), ++taskCount);
        TaskData storage task = tasks[taskCount];
        task.spec.schemaVersion = 1;
        task.spec.taskRef = ref;
        task.spec.requester = msg.sender;
        task.spec.terms = terms;
        task.spec.rootRef = ref;
        task.spec.createdAt = uint64(block.timestamp);
        task.spec.createdBlock = uint64(block.number);
        task.spec.reputationSnapshotBlock = uint64(block.number - 1);
        depositedAtoms += terms.budgetAtoms;
        escrowTotalAtoms += terms.budgetAtoms;
        if (parentId != 0) {
            TaskData storage parent = tasks[parentId];
            task.spec.parentRef = P.OptionalTaskRef(true, parent.spec.taskRef);
            task.spec.rootRef = parent.spec.rootRef;
            task.spec.depth = parent.spec.depth + 1;
            parent.childrenCreated++;
            parent.activeChildren++;
            parent.reservedChildBudgets += terms.budgetAtoms;
        }
        emit TaskCreated(P.TaskCreatedData(1, 1, task.spec));
    }

    function createTask(P.TaskTerms calldata terms)
        external
        payable
        override
        writeGuard
        returns (P.TaskRef memory taskRef)
    {
        return createFundedTask(terms, 0);
    }

    function createChildTask(P.TaskRef calldata parentRef, P.TaskTerms calldata terms)
        external
        payable
        override
        writeGuard
        returns (P.TaskRef memory taskRef)
    {
        TaskData storage parent = resolveTask(parentRef);
        checkState(parent, P.STATE_RUNNING);
        requireProtocol(msg.sender == winnerBid(parent).offer.executionSigner, P.UNAUTHORIZED);
        requireProtocol(block.timestamp < parent.spec.terms.resultBy, P.DEADLINE_PASSED);
        return createFundedTask(terms, parentRef.taskId);
    }

    function readRegistryAddress(bytes4 selector, uint256 agentId)
        internal
        view
        returns (address result)
    {
        address registry = identityRegistry;
        requireProtocol(registry.code.length != 0, P.IDENTITY_UNAVAILABLE);
        bytes memory input = abi.encodeWithSelector(selector, agentId);
        bool success;
        uint256 size;
        uint256 word;
        assembly ("memory-safe") {
            let output := mload(0x40)
            success := staticcall(gas(), registry, add(input, 32), mload(input), output, 32)
            size := returndatasize()
            word := mload(output)
        }
        requireProtocol(success && size == 32 && word <= type(uint160).max, P.IDENTITY_UNAVAILABLE);
        return address(uint160(word));
    }

    function validateBidBoundary(
        P.BidOffer memory offer,
        P.OptionalBidPermit memory permit,
        P.OptionalSignature memory signature
    ) internal pure {
        requireTaskRef(offer.taskRef);
        requireAgentRef(offer.agentRef);
        requireProtocol(
            offer.executionSigner != address(0) && offer.payout != address(0), P.INVALID_RANGE
        );
        if (permit.present) {
            requireTaskRef(permit.value.taskRef);
            requireAgentRef(permit.value.agentRef);
            requireProtocol(
                permit.value.owner != address(0) && permit.value.executionSigner != address(0)
                    && permit.value.payout != address(0),
                P.INVALID_RANGE
            );
        } else {
            P.BidPermit memory empty;
            requireProtocol(
                keccak256(abi.encode(permit.value)) == keccak256(abi.encode(empty)),
                P.INVALID_ENCODING
            );
        }
        requireProtocol(
            signature.present
                ? signature.value.length > 0 && signature.value.length <= 4096
                : signature.value.length == 0,
            P.INVALID_ENCODING
        );
    }

    function verifyBidPermit(
        P.BidOffer memory offer,
        P.OptionalBidPermit memory permit,
        P.OptionalSignature memory signature
    ) internal view returns (address owner) {
        requireIdentityNamespace(offer.agentRef);
        owner = readRegistryAddress(bytes4(keccak256("ownerOf(uint256)")), offer.agentRef.agentId);
        requireProtocol(owner != address(0), P.IDENTITY_UNAVAILABLE);
        address wallet = readRegistryAddress(
            bytes4(keccak256("getAgentWallet(uint256)")), offer.agentRef.agentId
        );
        requireProtocol(permit.present == signature.present, P.INVALID_SIGNATURE);
        if (permit.present) requireProtocol(permit.value.owner == owner, P.OWNER_CHANGED);
        else requireProtocol(msg.sender == owner, P.UNAUTHORIZED);
        requireProtocol(wallet != address(0), P.WALLET_UNSET);
        requireProtocol(wallet == offer.payout, P.WALLET_MISMATCH);
        if (permit.present) {
            P.BidPermit memory value = permit.value;
            requireProtocol(
                equalAgentRef(value.agentRef, offer.agentRef)
                    && value.executionSigner == offer.executionSigner
                    && value.payout == offer.payout && value.bidAtoms == offer.bidAtoms
                    && value.profileDigest == offer.profileDigest,
                P.INVALID_SIGNATURE
            );
            bytes32 digest = Signatures.typedDigest(
                Signatures.domainSeparator(block.chainid, address(this)),
                Signatures.bidStructHash(value)
            );
            requireProtocol(
                Signatures.verifyBidPermit(digest, signature.value, owner), P.INVALID_SIGNATURE
            );
            checkNonce(owner, Signatures.BID_TYPEHASH, value.nonce, value.expiry);
        }
    }

    function checkNonce(address signer, bytes32 primaryTypeHash, uint256 nonce, uint64 expiry)
        internal
        view
    {
        requireProtocol(!usedNonces[signer][primaryTypeHash][nonce], P.NONCE_USED);
        requireProtocol(block.timestamp < expiry, P.SIGNATURE_EXPIRED);
    }

    function updateTopTwo(TaskData storage task, P.Bid memory bid) internal {
        uint256 id = bid.offer.agentRef.agentId;
        if (task.topCount == 0) {
            task.topAgentIds[0] = id;
            task.topCount = 1;
        } else if (ProtocolMath.precedes(bid, task.bids[task.topAgentIds[0]].value)) {
            task.topAgentIds[1] = task.topAgentIds[0];
            task.topAgentIds[0] = id;
            task.topCount = 2;
        } else if (
            task.topCount == 1 || ProtocolMath.precedes(bid, task.bids[task.topAgentIds[1]].value)
        ) {
            task.topAgentIds[1] = id;
            task.topCount = 2;
        }
    }

    function submitBid(
        P.BidOffer calldata offer,
        P.OptionalBidPermit calldata permit,
        P.OptionalSignature calldata signature
    ) external override writeGuard {
        validateBidBoundary(offer, permit, signature);
        TaskData storage task = resolveTask(offer.taskRef);
        if (permit.present) {
            requireProtocol(equalTaskRef(permit.value.taskRef, task.spec.taskRef), P.UNKNOWN_TASK);
        }
        checkState(task, P.STATE_OPEN);
        requireProtocol(block.timestamp < task.spec.terms.biddingClose, P.DEADLINE_PASSED);
        address owner = verifyBidPermit(offer, permit, signature);
        requireProtocol(
            owner != validator && offer.executionSigner != validator && offer.payout != validator,
            P.VALIDATOR_CONFLICT
        );
        requireProtocol(offer.bidAtoms <= task.spec.terms.budgetAtoms, P.BID_OVER_BUDGET);
        requireProtocol(!task.bids[offer.agentRef.agentId].exists, P.DUPLICATE_AGENT);
        (uint64 successes, uint64 failures) =
            snapshotCounters(offer.agentRef.agentId, task.spec.reputationSnapshotBlock);
        uint32 p = ProtocolMath.calculateProbability(successes, failures);
        P.Bid memory bid = P.Bid(
            1,
            offer,
            owner,
            task.spec.reputationSnapshotBlock,
            P.Counters(successes, failures),
            p,
            ProtocolMath.calculateScore(
                p, task.spec.terms.alphaNum, task.spec.terms.alphaDen, offer.bidAtoms
            )
        );
        task.bids[offer.agentRef.agentId] = StoredBid(true, bid);
        updateTopTwo(task, bid);
        P.OptionalUint256 memory permitNonce;
        if (permit.present) {
            usedNonces[owner][Signatures.BID_TYPEHASH][permit.value.nonce] = true;
            permitNonce = P.OptionalUint256(true, permit.value.nonce);
        }
        emit BidAccepted(P.BidAcceptedData(1, 1, bid, permitNonce));
    }

    function calculateAllocation(TaskData storage task)
        internal
        view
        returns (P.Allocation memory allocation)
    {
        allocation.schemaVersion = 1;
        allocation.taskRef = task.spec.taskRef;
        allocation.mechanismVersion = 1;
        allocation.outcome = P.OUTCOME_UNALLOCATED;
        allocation.allocatedAt = uint64(block.timestamp);
        if (task.topCount == 0) return allocation;
        P.Bid storage best = task.bids[task.topAgentIds[0]].value;
        if (best.score <= 0) return allocation;
        int256 second = task.topCount == 2 ? task.bids[task.topAgentIds[1]].value.score : int256(0);
        if (second < 0) second = 0;
        uint96 critical = ProtocolMath.calculatePrice(
            best.p, second, task.spec.terms.alphaNum, task.spec.terms.alphaDen
        );
        allocation.outcome = P.OUTCOME_AWARDED;
        allocation.winner = P.OptionalAgentRef(true, best.offer.agentRef);
        allocation.awardId = 1;
        allocation.winningScore = P.OptionalInt256(true, best.score);
        allocation.secondScore = P.OptionalInt256(true, second);
        allocation.criticalAtoms = P.OptionalUint96(true, critical);
        allocation.reservedAtoms =
            critical < task.spec.terms.budgetAtoms ? critical : task.spec.terms.budgetAtoms;
    }

    function allocateTask(P.TaskRef calldata taskRef) external override writeGuard {
        TaskData storage task = resolveTask(taskRef);
        checkState(task, P.STATE_OPEN);
        requireProtocol(block.timestamp >= task.spec.terms.biddingClose, P.TOO_EARLY);
        requireProtocol(block.timestamp < task.spec.terms.allocationBy, P.DEADLINE_PASSED);
        P.Allocation memory allocation = calculateAllocation(task);
        task.allocation = allocation;
        task.hasAllocation = true;
        if (allocation.outcome == P.OUTCOME_UNALLOCATED) {
            P.OptionalValidationRecord memory noValidation;
            settleTask(task, P.REASON_UNALLOCATED, noValidation);
        } else {
            task.status = P.STATE_AWARDED;
            emit TaskAwarded(P.TaskAwardedData(1, 1, allocation));
        }
    }

    function acceptAward(P.ExecutionRef calldata executionRef, uint96 ownWorkReserveAtoms)
        external
        override
        writeGuard
    {
        TaskData storage task = resolveTask(executionRef.taskRef);
        checkExecution(task, executionRef, P.STATE_AWARDED);
        requireProtocol(block.timestamp < task.spec.terms.acceptBy, P.DEADLINE_PASSED);
        requireProtocol(ownWorkReserveAtoms <= task.allocation.reservedAtoms, P.INVALID_RESERVE);
        task.status = P.STATE_RUNNING;
        task.ownWorkReserveAtoms = ownWorkReserveAtoms;
        emit AwardAccepted(P.AwardAcceptedData(
                1, 1, executionRef, ownWorkReserveAtoms, uint64(block.timestamp)
            ));
    }

    function submitResult(P.ExecutionRef calldata executionRef, P.ContentRef calldata artifact)
        external
        override
        writeGuard
    {
        TaskData storage task = resolveTask(executionRef.taskRef);
        checkExecution(task, executionRef, P.STATE_RUNNING);
        requireProtocol(block.timestamp < task.spec.terms.resultBy, P.DEADLINE_PASSED);
        requireProtocol(task.activeChildren == 0, P.CHILDREN_ACTIVE);
        requireContent(artifact);
        task.status = P.STATE_SUBMITTED;
        task.hasResult = true;
        task.result = P.ResultCommitment(1, executionRef, artifact, uint64(block.timestamp));
        emit ResultSubmitted(P.ResultSubmittedData(1, 1, task.result));
    }

    function verifyValidationVerdict(TaskData storage task, P.ValidationRecord memory record)
        internal
        view
    {
        requireProtocol(
            equalAgentRef(record.agentRef, task.allocation.winner.value)
                && record.resultDigest == task.result.artifact.digest,
            P.RESULT_MISMATCH
        );
        requireProtocol(
            record.validationPolicyDigest == task.spec.terms.validationPolicy.digest,
            P.POLICY_MISMATCH
        );
        requireProtocol(record.validator == validator, P.INVALID_SIGNATURE);
        bytes32 digest = Signatures.typedDigest(
            Signatures.domainSeparator(block.chainid, address(this)),
            Signatures.verdictStructHash(record)
        );
        // The pinned validator always uses ECDSA, even if its address has delegation code.
        requireProtocol(
            Signatures.verifyEoa(digest, record.signature, validator), P.INVALID_SIGNATURE
        );
        checkNonce(validator, Signatures.VERDICT_TYPEHASH, record.nonce, record.expiry);
    }

    function settleVerdict(P.ValidationRecord calldata record) external override writeGuard {
        requireProtocol(record.schemaVersion == 1, P.INVALID_ENCODING);
        requireProtocol(record.verdict <= 1, P.INVALID_RANGE);
        requireAgentRef(record.agentRef);
        requireProtocol(record.validator != address(0), P.INVALID_RANGE);
        requireProtocol(
            record.signature.length > 0 && record.signature.length <= 4096, P.INVALID_ENCODING
        );
        requireContent(record.evidence);
        TaskData storage task = resolveTask(record.executionRef.taskRef);
        checkState(task, P.STATE_SUBMITTED);
        requireProtocol(record.executionRef.awardId == 1, P.WRONG_STATE);
        requireProtocol(block.timestamp < task.spec.terms.validationBy, P.DEADLINE_PASSED);
        verifyValidationVerdict(task, record);
        usedNonces[validator][Signatures.VERDICT_TYPEHASH][record.nonce] = true;
        settleTask(
            task,
            record.verdict == P.VERDICT_PASS ? P.REASON_SUCCESS : P.REASON_VALIDATION_FAILED,
            P.OptionalValidationRecord(true, record)
        );
    }

    function settleTask(
        TaskData storage task,
        uint8 reason,
        P.OptionalValidationRecord memory validation
    ) internal {
        P.SettlementReceipt memory receipt;
        receipt.schemaVersion = 1;
        receipt.taskRef = task.spec.taskRef;
        receipt.reason = reason;
        receipt.refundAddress = task.spec.terms.refundAddress;
        receipt.budgetAtoms = task.spec.terms.budgetAtoms;
        if (task.hasAllocation && task.allocation.winner.present) {
            receipt.winner = task.allocation.winner;
            receipt.payout = P.OptionalAddress(true, winnerBid(task).offer.payout);
            receipt.reservedAtoms = task.allocation.reservedAtoms;
        }
        receipt.paidAtoms = reason == P.REASON_SUCCESS ? receipt.reservedAtoms : 0;
        receipt.refundAtoms = receipt.budgetAtoms - receipt.paidAtoms;
        if (task.hasResult) {
            receipt.resultDigest = P.OptionalDigest(true, task.result.artifact.digest);
        }
        receipt.validation = validation;
        if (reason == P.REASON_SUCCESS) {
            receipt.counterEffect = P.EFFECT_SUCCESS;
        } else if (
            reason == P.REASON_VALIDATION_FAILED || reason == P.REASON_NO_SHOW
                || reason == P.REASON_EXECUTION_TIMEOUT
        ) {
            receipt.counterEffect = P.EFFECT_FAILURE;
        } else {
            receipt.counterEffect = P.EFFECT_NONE;
        }
        receipt.settledAt = uint64(block.timestamp);
        task.status = P.STATE_SETTLED;
        task.hasReceipt = true;
        task.receipt = receipt;
        escrowTotalAtoms -= receipt.budgetAtoms;
        creditTotalAtoms += receipt.budgetAtoms;
        if (receipt.paidAtoms != 0) credits[receipt.payout.value] += receipt.paidAtoms;
        if (receipt.refundAtoms != 0) credits[receipt.refundAddress] += receipt.refundAtoms;
        if (receipt.counterEffect != P.EFFECT_NONE) {
            recordOutcome(receipt.winner.value.agentId, receipt.counterEffect);
        }
        if (task.spec.parentRef.present) {
            TaskData storage parent = tasks[task.spec.parentRef.value.taskId];
            parent.reservedChildBudgets -= receipt.budgetAtoms;
            parent.committedChildPayouts += receipt.paidAtoms;
            parent.activeChildren--;
        }
        emit TaskSettled(P.TaskSettledData(1, 1, receipt));
    }

    function recordOutcome(uint256 agentId, uint8 effect) internal {
        Checkpoint[] storage history = histories[agentId][FAMILY];
        uint256 count = history.length;
        Checkpoint memory point =
            count == 0 ? Checkpoint(uint64(block.number), 0, 0) : history[count - 1];
        // One terminal observation per task bounds both counters by the uint64 namespace.
        requireProtocol(uint256(point.successes) + point.failures < taskCount, P.INVALID_RANGE);
        if (effect == P.EFFECT_SUCCESS) point.successes++;
        else point.failures++;
        bool overwrite = count != 0 && point.blockNumber == block.number;
        point.blockNumber = uint64(block.number);
        if (overwrite) history[count - 1] = point;
        else history.push(point);
    }

    function snapshotCounters(uint256 agentId, uint64 snapshotBlock)
        internal
        view
        returns (uint64 successes, uint64 failures)
    {
        Checkpoint[] storage history = histories[agentId][FAMILY];
        uint256 low;
        uint256 high = history.length;
        while (low < high) {
            uint256 middle = low + (high - low) / 2;
            if (history[middle].blockNumber <= snapshotBlock) low = middle + 1;
            else high = middle;
        }
        if (low != 0) return (history[low - 1].successes, history[low - 1].failures);
    }

    function expireTask(P.TaskRef calldata taskRef) external override writeGuard {
        TaskData storage task = resolveTask(taskRef);
        requireProtocol(task.status != P.STATE_SETTLED, P.ALREADY_SETTLED);
        uint64 cutoff;
        uint8 reason;
        if (task.status == P.STATE_OPEN) {
            cutoff = task.spec.terms.allocationBy;
            reason = P.REASON_ALLOCATION_EXPIRED;
        } else if (task.status == P.STATE_AWARDED) {
            cutoff = task.spec.terms.acceptBy;
            reason = P.REASON_NO_SHOW;
        } else if (task.status == P.STATE_RUNNING) {
            cutoff = task.spec.terms.resultBy;
            reason = P.REASON_EXECUTION_TIMEOUT;
        } else {
            cutoff = task.spec.terms.validationBy;
            reason = P.REASON_VALIDATOR_TIMEOUT;
        }
        requireProtocol(block.timestamp >= cutoff, P.TOO_EARLY);
        P.OptionalValidationRecord memory noValidation;
        settleTask(task, reason, noValidation);
    }

    function cancelTask(P.TaskRef calldata taskRef) external override writeGuard {
        TaskData storage task = resolveTask(taskRef);
        checkState(task, P.STATE_OPEN);
        requireProtocol(msg.sender == task.spec.requester, P.UNAUTHORIZED);
        requireProtocol(block.timestamp < task.spec.terms.biddingClose, P.DEADLINE_PASSED);
        requireProtocol(task.topCount == 0, P.INVALID_TERMS);
        P.OptionalValidationRecord memory noValidation;
        settleTask(task, P.REASON_CANCELLED, noValidation);
    }

    function withdrawCredit(address receiver, uint256 amountAtoms) external override writeGuard {
        requireProtocol(amountAtoms > 0 && receiver != address(0), P.INVALID_RANGE);
        requireProtocol(amountAtoms <= credits[msg.sender], P.INSUFFICIENT_CREDIT);
        credits[msg.sender] -= amountAtoms;
        creditTotalAtoms -= amountAtoms;
        withdrawnAtoms += amountAtoms;
        bool success;
        assembly ("memory-safe") { success := call(gas(), receiver, amountAtoms, 0, 0, 0, 0) }
        requireProtocol(success, P.TRANSFER_FAILED);
        emit CreditWithdrawn(P.CreditWithdrawnData(1, 1, msg.sender, receiver, amountAtoms));
    }

    function readTask(P.TaskRef calldata taskRef)
        external
        view
        override
        returns (P.TaskView memory view_)
    {
        TaskData storage task = resolveTask(taskRef);
        view_.task = task.spec;
        view_.status = task.status;
        view_.allocation = P.OptionalAllocation(task.hasAllocation, task.allocation);
        if (task.hasAllocation && task.allocation.winner.present) {
            view_.winningBid = P.OptionalBid(true, winnerBid(task));
        }
        view_.ownWorkReserveAtoms = task.ownWorkReserveAtoms;
        view_.childrenCreated = task.childrenCreated;
        view_.activeChildren = task.activeChildren;
        view_.reservedChildBudgets = task.reservedChildBudgets;
        view_.committedChildPayouts = task.committedChildPayouts;
        view_.result = P.OptionalResultCommitment(task.hasResult, task.result);
        view_.receipt = P.OptionalSettlementReceipt(task.hasReceipt, task.receipt);
    }

    function readBid(P.TaskRef calldata taskRef, P.AgentRef calldata agentRef)
        external
        view
        override
        returns (P.OptionalBid memory bid)
    {
        TaskData storage task = resolveTask(taskRef);
        requireIdentityNamespace(agentRef);
        StoredBid storage stored = task.bids[agentRef.agentId];
        return P.OptionalBid(stored.exists, stored.value);
    }

    function readSettlement(P.TaskRef calldata taskRef)
        external
        view
        override
        returns (P.OptionalSettlementReceipt memory receipt)
    {
        TaskData storage task = resolveTask(taskRef);
        return P.OptionalSettlementReceipt(task.hasReceipt, task.receipt);
    }

    function readCounters(
        P.AgentRef calldata agentRef,
        string calldata taskFamily,
        uint64 snapshotBlock
    ) external view override returns (uint64 successes, uint64 failures) {
        requireIdentityNamespace(agentRef);
        requireProtocol(keccak256(bytes(taskFamily)) == FAMILY, P.UNSUPPORTED_POLICY);
        return snapshotCounters(agentRef.agentId, snapshotBlock);
    }

    function readCredit(address owner) external view override returns (uint256 amountAtoms) {
        requireProtocol(owner != address(0), P.INVALID_RANGE);
        return credits[owner];
    }

    function isNonceUsed(address signer, bytes32 primaryTypeHash, uint256 nonce)
        external
        view
        override
        returns (bool used)
    {
        requireProtocol(signer != address(0), P.INVALID_RANGE);
        return usedNonces[signer][primaryTypeHash][nonce];
    }

    function readPolicy()
        external
        view
        override
        returns (
            uint256 chainId,
            address market,
            address identityRegistry,
            address validator,
            uint8 policyVersion
        )
    {
        return policyValues();
    }

    function policyValues() internal view returns (uint256, address, address, address, uint8) {
        return (block.chainid, address(this), identityRegistry, validator, 1);
    }

    function readAccounting()
        external
        view
        override
        returns (
            uint64 taskCount,
            uint256 depositedAtoms,
            uint256 escrowTotalAtoms,
            uint256 creditTotalAtoms,
            uint256 withdrawnAtoms
        )
    {
        return accountingValues();
    }

    function accountingValues() internal view returns (uint64, uint256, uint256, uint256, uint256) {
        return (taskCount, depositedAtoms, escrowTotalAtoms, creditTotalAtoms, withdrawnAtoms);
    }
}

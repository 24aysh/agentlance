// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {AgentLanceMarket} from "../../src/AgentLanceMarket.sol";
import {ProtocolTypes as P} from "../../src/ProtocolTypes.sol";

/// @dev Synthetic projections and arithmetic ceilings only; never used for deployed scenarios.
contract MarketHarness is AgentLanceMarket {
    constructor(address registry, address validator) AgentLanceMarket(registry, validator) {}

    function seedTask(P.TaskView calldata value) external {
        uint64 id = value.task.taskRef.taskId;
        if (id > taskCount) taskCount = id;
        TaskData storage task = tasks[id];
        task.spec = value.task;
        task.status = value.status;
        task.hasAllocation = value.allocation.present;
        task.allocation = value.allocation.value;
        task.ownWorkReserveAtoms = value.ownWorkReserveAtoms;
        task.childrenCreated = value.childrenCreated;
        task.activeChildren = value.activeChildren;
        task.reservedChildBudgets = value.reservedChildBudgets;
        task.committedChildPayouts = value.committedChildPayouts;
        task.hasResult = value.result.present;
        task.result = value.result.value;
        task.hasReceipt = value.receipt.present;
        task.receipt = value.receipt.value;
    }

    function seedBid(uint64 taskId, P.Bid calldata bid) external {
        TaskData storage task = tasks[taskId];
        task.bids[bid.offer.agentRef.agentId] = StoredBid(true, bid);
        updateTopTwo(task, bid);
    }

    function seedCheckpoint(
        uint256 agentId,
        string calldata family,
        uint64 height,
        uint64 successes,
        uint64 failures
    ) external {
        Checkpoint[] storage history = histories[agentId][keccak256(bytes(family))];
        Checkpoint memory point = Checkpoint(height, successes, failures);
        if (history.length != 0 && history[history.length - 1].blockNumber == height) {
            history[history.length - 1] = point;
        } else {
            history.push(point);
        }
    }

    function seedTaskCount(uint64 count) external {
        taskCount = count;
    }

    function clearBids(uint64 taskId) external {
        tasks[taskId].topCount = 0;
    }

    function seedCredit(address account, uint256 value) external {
        credits[account] = value;
    }

    function seedAccounting(uint256 deposits, uint256 escrow, uint256 credit, uint256 withdrawn)
        external
    {
        depositedAtoms = deposits;
        escrowTotalAtoms = escrow;
        creditTotalAtoms = credit;
        withdrawnAtoms = withdrawn;
    }

    function seedNonce(address signer, bytes32 kind, uint256 nonce) external {
        usedNonces[signer][kind][nonce] = true;
    }

    function projectChildEnvelope(uint64 parentId, P.TaskTerms calldata terms) external view {
        validateChildEnvelope(tasks[parentId], terms);
    }

    function observeOutcome(uint256 agentId, uint8 effect) external {
        recordOutcome(agentId, effect);
    }

    function checkpointCount(uint256 agentId, string calldata family)
        external
        view
        returns (uint256)
    {
        return histories[agentId][keccak256(bytes(family))].length;
    }
}

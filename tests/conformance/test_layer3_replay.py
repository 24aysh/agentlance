"""Bounded test-only event replay; no indexer, database, or production finality policy."""

from copy import deepcopy

import pytest

from modules.domain.records import agentKey, taskKey
from modules.market_core.state import Checkpoint, CoreState, TaskState
from tests.integration.test_layer3_transactions import runTree
from tests.layer3_support import Layer3Rig, localNode

pytestmark = pytest.mark.l3socket


class TreeEventProjection:
    """Rebuild the exercised tree from event facts, without transition/oracle calls.

    Raw ordered RPC logs are the only transition input. This bounded consumer covers
    awarded task trees, including terminal parents whose children settle later. It
    deliberately does not claim an L4 discovery service or UNALLOCATED reconstruction.
    """

    def __init__(self, chainId, market, codec):
        self.chainId, self.market, self.codec = chainId, market, codec
        self.state = CoreState()
        self.seen = {}

    def replay(self, logs):
        for log in logs:
            assert log["address"].lower() == self.market
            assert not log.get("removed", False)
            key = (
                self.chainId,
                self.market,
                log["blockHash"],
                int(log["logIndex"], 16),
            )
            event = self.codec.event(log)
            if key in self.seen:
                assert self.seen[key] == event, "Conflicting duplicate log"
                continue
            self.applyEvent(event, int(log["blockNumber"], 16))
            self.seen[key] = deepcopy(event)

    def resolveTask(self, ref):
        assert ref["chainId"] == str(self.chainId) and ref["market"] == self.market
        return self.state.tasks[taskKey(ref)]

    def applyEvent(self, event, height):
        state, name, payload = self.state, event["name"], deepcopy(event["payload"])
        if name == "TaskCreated":
            spec = payload["task"]
            assert spec["taskRef"]["chainId"] == str(self.chainId)
            assert spec["taskRef"]["market"] == self.market
            key = taskKey(spec["taskRef"])
            assert key not in state.tasks and key[2] == state.taskCount + 1
            budget = int(spec["terms"]["budgetAtoms"])
            state.tasks[key] = TaskState(spec, escrowAtoms=budget)
            state.taskCount += 1
            state.depositedAtoms += budget
            if spec["parentRef"] is not None:
                parent = self.resolveTask(spec["parentRef"])
                parent.childrenCreated += 1
                parent.activeChildren += 1
                parent.reservedChildBudgets += budget
        elif name == "BidAccepted":
            bid = payload["bid"]
            task = self.resolveTask(bid["offer"]["taskRef"])
            key = agentKey(bid["offer"]["agentRef"])
            assert task.status == "OPEN" and key not in task.bids
            task.bids[key] = bid
            task.topTwo = tuple(
                sorted(
                    task.bids.values(),
                    key=lambda value: (
                        -int(value["score"]),
                        agentKey(value["offer"]["agentRef"]),
                    ),
                )[:2]
            )
            if payload["permitNonce"] is not None:
                state.usedNonces.add(
                    (self.market, bid["ownerAtBid"], "BidPermit", int(payload["permitNonce"]))
                )
        elif name == "TaskAwarded":
            allocation = payload["allocation"]
            task = self.resolveTask(allocation["taskRef"])
            assert task.status == "OPEN" and task.allocation is None
            task.allocation, task.status = allocation, "AWARDED"
        elif name == "AwardAccepted":
            task = self.resolveTask(payload["executionRef"]["taskRef"])
            assert task.status == "AWARDED"
            task.status = "RUNNING"
            task.ownWorkReserveAtoms = int(payload["ownWorkReserveAtoms"])
        elif name == "ResultSubmitted":
            result = payload["result"]
            task = self.resolveTask(result["executionRef"]["taskRef"])
            assert task.status == "RUNNING" and task.result is None
            task.result, task.status = result, "SUBMITTED"
        elif name == "TaskSettled":
            receipt = payload["receipt"]
            task = self.resolveTask(receipt["taskRef"])
            assert task.receipt is None and receipt["reason"] != "UNALLOCATED"
            budget = int(receipt["budgetAtoms"])
            assert budget == task.escrowAtoms
            task.receipt, task.status, task.escrowAtoms = receipt, "SETTLED", 0
            for account, field in (
                (receipt["payout"], "paidAtoms"),
                (receipt["refundAddress"], "refundAtoms"),
            ):
                amount = int(receipt[field])
                if amount:
                    state.credits[account] = state.credits.get(account, 0) + amount
            state.reputation.seenReceipts[taskKey(receipt["taskRef"])] = receipt
            if receipt["counterEffect"] != "NONE":
                key = agentKey(receipt["winner"]), task.spec["terms"]["taskFamily"]
                history = state.reputation.checkpoints.get(key, ())
                previous = history[-1] if history else Checkpoint(0, 0, 0)
                point = Checkpoint(
                    height,
                    previous.successes + (receipt["counterEffect"] == "SUCCESS"),
                    previous.failures + (receipt["counterEffect"] == "FAILURE"),
                )
                state.reputation.checkpoints[key] = (
                    (*history[:-1], point)
                    if history and previous.blockNumber == height
                    else (*history, point)
                )
            validation = receipt["validation"]
            if validation is not None:
                state.usedNonces.add(
                    (
                        self.market,
                        validation["validator"],
                        "ValidationVerdict",
                        int(validation["nonce"]),
                    )
                )
            if task.spec["parentRef"] is not None:
                parent = self.resolveTask(task.spec["parentRef"])
                parent.reservedChildBudgets -= budget
                parent.committedChildPayouts += int(receipt["paidAtoms"])
                parent.activeChildren -= 1
        else:
            assert name == "CreditWithdrawn"
            amount = int(payload["amountAtoms"])
            assert state.credits[payload["owner"]] >= amount
            state.credits[payload["owner"]] -= amount
            state.withdrawnAtoms += amount


def testFullTreeEventReplayRebuildsIndependentState(tmp_path):
    with localNode(tmp_path / "node") as rpc:
        rig = Layer3Rig(rpc)
        runTree(rig)
        # Unlike rig.events, this input is obtained afresh from canonical chain logs.
        logs = rpc.call(
            "eth_getLogs",
            {"address": rig.market, "fromBlock": "0x0", "toBlock": "latest"},
        )
        assert {rig.codec.event(log)["name"] for log in logs} == set(rig.codec.catalog["events"])
        projection = TreeEventProjection(31337, rig.market, rig.codec)
        midpoint = len(logs) // 2
        projection.replay(logs[:midpoint])
        # A restarted consumer retains its own projection and cursor/deduplication state.
        resumed = TreeEventProjection(31337, rig.market, rig.codec)
        resumed.state, resumed.seen = deepcopy(projection.state), deepcopy(projection.seen)
        resumed.replay(logs[max(0, midpoint - 3) :])
        assert resumed.state == rig.state
        # The reference state was independently advanced by real inputs/context in runTree.
        # Its comprehensive comparison covers all task/bid/receipt/credit/counter/nonce getters.
        rig.compare()
        before = deepcopy(resumed.state)
        resumed.replay(logs)
        assert resumed.state == before and len(resumed.seen) == len(logs)
        assert resumed.state.withdrawnAtoms == 260
        assert sum(resumed.state.credits.values()) == 0
        assert len(resumed.state.usedNonces) == 3  # One signed child bid, two validator verdicts.
        assert len(resumed.state.reputation.seenReceipts) == 2
        parent = resumed.resolveTask(rig.taskRef(1))
        assert (parent.childrenCreated, parent.activeChildren) == (1, 0)
        assert (parent.reservedChildBudgets, parent.committedChildPayouts) == (0, 40)

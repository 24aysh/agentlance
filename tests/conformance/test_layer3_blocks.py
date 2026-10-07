"""Independent same-block transition replay against actual ordered Monad transactions."""

import json
from copy import deepcopy

import pytest

from modules.domain.records import agentKey, taskKey, validateRecord
from modules.market_core.state import Applied
from modules.market_core.transitions import applyCommand
from tests.integration.test_layer3_transactions import createRoot, stageTask
from tests.layer3_support import ROOT, Layer3Rig, localNode, readJson

pytestmark = pytest.mark.l3socket


def applyMinedBatch(rig, actions):
    """Mine shared commands once; neither implementation supplies the other's state."""
    caller = rig.actors["requester"]
    nonce = int(rig.rpc.call("eth_getTransactionCount", caller, "pending"), 16)
    transactions = []
    rig.rpc.call("evm_setAutomine", False)
    try:
        for offset, (name, data, value) in enumerate(actions):
            command = {"schemaVersion": 1, "command": name, "input": deepcopy(data)}
            validateRecord(command, "Command", rig.codec.schema)
            transaction = {
                "from": caller,
                "to": rig.market,
                "data": rig.codec.commandData(name, data),
                "value": hex(value),
                "nonce": hex(nonce + offset),
                # Four transactions fit the untouched node's 30M block limit.
                "gas": hex(4_000_000),
            }
            transactions.append(rig.rpc.call("eth_sendTransaction", transaction))
        rig.rpc.call("evm_mine")
        receipts = [rig.rpc.receipt(transaction) for transaction in transactions]
        blockHashes = {receipt["blockHash"] for receipt in receipts}
        assert len(blockHashes) == 1, "The four commands must actually share one mined block"
        block = rig.rpc.call("eth_getBlockByHash", receipts[0]["blockHash"], False)
        assert block["transactions"] == transactions
        assert [int(receipt["transactionIndex"], 16) for receipt in receipts] == [0, 1, 2, 3]
        for (name, data, value), receipt in zip(actions, receipts, strict=True):
            command = {"schemaVersion": 1, "command": name, "input": deepcopy(data)}
            before = deepcopy(rig.state)
            result = applyCommand(
                rig.state, command, rig.context(name, data, caller, value, block), rig.policy
            )
            assert rig.state == before
            assert isinstance(result, Applied), (name, result)
            assert int(receipt["status"], 16) == 1
            events = [rig.codec.event(log) for log in receipt["logs"]]
            assert events == list(result.events)
            rig.state = result.state
            rig.events.extend(events)
            rig.transactions.append(
                {
                    "hash": receipt["transactionHash"],
                    "command": name,
                    "block": receipt["blockNumber"],
                    "transactionIndex": receipt["transactionIndex"],
                    "gasUsed": receipt["gasUsed"],
                    "error": None,
                    "events": events,
                }
            )
    finally:
        rig.rpc.call("evm_setAutomine", True)
    rig.compare()
    return int(block["number"], 16)


@pytest.mark.parametrize(
    "case", readJson("specs/fixtures/layer-3-blocks.json")["cases"], ids=lambda case: case["id"]
)
def testSameBlockSnapshotDifferential(tmp_path, case):
    with localNode(tmp_path) as rpc:
        rig = Layer3Rig(rpc)
        for _ in range(2):
            taskId, terms = createRoot(rig)
            stageTask(rig, taskId, terms, signed=False)
        beforeTerms, afterTerms = rig.terms(), rig.terms()
        # These are common transaction inputs built before either system advances.
        actions = [
            ("createTask", {"terms": beforeTerms}, 100),
            ("settleVerdict", rig.verdict(1, verdict=case["action"]["order"][1], nonce=100), 0),
            ("settleVerdict", rig.verdict(2, verdict=case["action"]["order"][2], nonce=101), 0),
            ("createTask", {"terms": afterTerms}, 100),
        ]
        commonBlock = applyMinedBatch(rig, actions)
        for taskId in (3, 4):
            task = rig.state.tasks[taskKey(rig.taskRef(taskId))]
            assert int(task.spec["createdBlock"]) == commonBlock
            assert int(task.spec["reputationSnapshotBlock"]) == commonBlock - 1
        nextId, _ = createRoot(rig)
        nextTask = rig.state.tasks[taskKey(rig.taskRef(nextId))]
        assert int(nextTask.spec["createdBlock"]) == commonBlock + 1
        assert int(nextTask.spec["reputationSnapshotBlock"]) == commonBlock
        for taskId, expected in (
            (3, case["expected"]["sameBlockSnapshots"]),
            (4, case["expected"]["sameBlockSnapshots"]),
            (nextId, case["expected"]["nextBlockSnapshot"]),
        ):
            rig.command("submitBid", rig.bid(taskId), caller="owner")
            bid = rig.state.tasks[taskKey(rig.taskRef(taskId))].bids[agentKey(rig.agentRef(0))]
            assert {**bid["counters"], "p": bid["p"]} == expected
        histories = rig.state.reputation.checkpoints[
            (agentKey(rig.agentRef(0)), "structured-output-v1")
        ]
        assert len(histories) == 1 and histories[0].blockNumber == commonBlock
        reportPath = ROOT / ".scratch/layer3" / (case["id"] + ".json")
        reportPath.write_text(
            json.dumps(
                {"scenarioId": case["id"], "commonBlock": commonBlock, **rig.report()}, indent=2
            )
            + "\n"
        )

"""Crash/finality injection at external RPC and persistence boundaries, with a real market."""

import asyncio
from copy import deepcopy

import pytest

from modules.agent_client.participant import Participant, makeCommand
from modules.agent_client.ports import AdapterError
from tests.layer3_support import localNode
from tests.layer4_support import Layer4Rig

pytestmark = pytest.mark.l3socket


def participant(env):
    return Participant(
        env.rig.agentRef(0),
        env.rig.actors["signer"],
        env.market,
        env.registry,
        None,
        env.journal,
        env.chain.schema,
        None,
        None,
        (),
    )


@pytest.mark.parametrize(
    "stage", ["before-adapter-record", "signed-before-send", "lost-send", "receipt-before-result"]
)
def testSubmissionCrashReconcilesSameIntent(tmp_path, monkeypatch, stage):
    async def run(rpc):
        env = Layer4Rig(rpc, tmp_path)
        try:
            task = env.create()
            command = makeCommand(
                "submitBid", **env.rig.bid(int(task["taskRef"]["taskId"]), signed=True)
            )
            sends, crashed = [], []
            originalCall, originalSave = env.transport.call, env.journal.saveNative

            async def rpcCall(method, *params):
                result = await originalCall(method, *params)
                if method == "eth_sendRawTransaction":
                    sends.append(params[0])
                    if stage == "lost-send" and not crashed:
                        crashed.append(True)
                        raise AdapterError("UNAVAILABLE", "Injected lost send response")
                return result

            def save(operation):
                fail = not crashed and (
                    (stage == "before-adapter-record" and operation["transactionHash"] is None)
                    or (stage == "signed-before-send" and operation["transactionHash"] is not None)
                    or (stage == "receipt-before-result" and operation["result"] is not None)
                )
                if fail and stage == "before-adapter-record":
                    crashed.append(True)
                    raise RuntimeError("Injected power loss")
                if fail and stage == "receipt-before-result":
                    crashed.append(True)
                    raise RuntimeError("Injected power loss")
                originalSave(operation)
                if fail:
                    crashed.append(True)
                    raise RuntimeError("Injected power loss")

            monkeypatch.setattr(env.transport, "call", rpcCall)
            monkeypatch.setattr(env.journal, "saveNative", save)
            actor = participant(env)
            try:
                result = await actor.submitIntent("bid-task", command)
                if stage == "receipt-before-result":
                    await env.applied(result)
            except RuntimeError as error:
                assert "power loss" in str(error)
            intent = env.journal.findIntent("bid-task")
            assert intent["attempted"] and crashed
            originalId = intent["operationId"]
            env.finalize()
            await env.restart()
            result = await env.applied(await participant(env).submitIntent("bid-task", command))
            assert result["state"] == "APPLIED" and result["operationId"] == originalId
            assert len(env.journal.nativeOperations()) == 1
            logs = rpc.call(
                "eth_getLogs", {"address": env.rig.market, "fromBlock": "0x0", "toBlock": "latest"}
            )
            assert sum(env.rig.codec.event(log)["name"] == "BidAccepted" for log in logs) == 1
            changed = deepcopy(command)
            changed["input"]["offer"]["bidAtoms"] = "21"
            with pytest.raises(AdapterError, match="CONFLICT"):
                await env.market.submitSignedBid(changed, originalId)
        finally:
            await env.close()

    with localNode(tmp_path) as rpc:
        asyncio.run(run(rpc))


@pytest.mark.parametrize(
    "fault,kind",
    [
        ("unsupported", "UNAVAILABLE"),
        ("stale", "UNAVAILABLE"),
        ("regression", "UNAVAILABLE"),
        ("same-height", "FINALITY_CONFLICT"),
        ("historical-hash", "FINALITY_CONFLICT"),
        ("chain", "FINALITY_CONFLICT"),
        ("market-code", "FINALITY_CONFLICT"),
    ],
)
def testFinalityFaultsDoNotBecomeLatest(tmp_path, monkeypatch, fault, kind):
    async def run(rpc):
        env = Layer4Rig(rpc, tmp_path)
        try:
            observed = await env.chain.qualify()
            height = int(observed["blockNumber"])
            if fault == "historical-hash":
                env.finalize()
                env.finalize()
            original = env.transport.call
            methods = []

            async def broken(method, *params):
                methods.append((method, params))
                if method == "eth_getBlockByNumber" and params[0] == "finalized":
                    if fault == "unsupported":
                        raise AdapterError("UNAVAILABLE", "No finality tag")
                    block = deepcopy(await original(method, *params))
                    if fault == "stale":
                        block["timestamp"] = "0x1"
                    if fault == "regression":
                        block["number"] = hex(height - 1)
                    if fault == "same-height":
                        block["hash"] = "0x" + "ee" * 32
                    return block
                if (
                    fault == "historical-hash"
                    and method == "eth_getBlockByNumber"
                    and params[0] == hex(height)
                ):
                    return (await original(method, *params)) | {"hash": "0x" + "ef" * 32}
                if fault == "chain" and method == "eth_chainId":
                    return "0x1"
                if (
                    fault == "market-code"
                    and method == "eth_getCode"
                    and params[0] == env.rig.market
                ):
                    return "0x00"
                return await original(method, *params)

            monkeypatch.setattr(env.transport, "call", broken)
            with pytest.raises(AdapterError) as error:
                await env.chain.qualify()
            assert error.value.kind == kind
            assert not any(
                method == "eth_getBlockByNumber" and args[0] == "latest" for method, args in methods
            )
            await env.restart()
            assert env.journal.halted() == (kind == "FINALITY_CONFLICT")
        finally:
            await env.close()

    with localNode(tmp_path) as rpc:
        asyncio.run(run(rpc))


def testOrphanedUnfinalizedTransactionResendsIdenticalBytes(tmp_path):
    async def run(rpc):
        env = Layer4Rig(rpc, tmp_path)
        try:
            task = env.create()
            command = makeCommand(
                "submitBid", **env.rig.bid(int(task["taskRef"]["taskId"]), signed=True)
            )
            snapshot = rpc.call("evm_snapshot")
            result = await env.market.submitSignedBid(command, "reorg")
            rpc.call(
                "anvil_mine", "0x28"
            )  # Fork exceeds the 32-block overlap, but is not finalized.
            result = await env.market.readOperation("reorg")
            assert result["state"] == "PENDING"
            raw = env.journal.nativeOperation("reorg")["raw"]
            assert rpc.call("evm_revert", snapshot)
            env.rig.advance(env.rig.now() + 2)
            rpc.call("anvil_mine", "0x28")
            result = await env.market.readOperation("reorg")
            result = await env.applied(result)
            assert result["state"] == "APPLIED"
            assert env.journal.nativeOperation("reorg")["raw"] == raw
            assert env.journal.nativeOperation("reorg")["attempts"] == 2
        finally:
            await env.close()

    with localNode(tmp_path) as rpc:
        asyncio.run(run(rpc))


def testPreparedBytesSurviveDeadlineWithoutBroadcast(tmp_path, monkeypatch):
    async def run(rpc):
        env = Layer4Rig(rpc, tmp_path)
        try:
            task = env.create()
            command = makeCommand(
                "submitBid", **env.rig.bid(int(task["taskRef"]["taskId"]), signed=True)
            )
            original = env.journal.saveNative

            def crash(op):
                original(op)
                if op["transactionHash"]:
                    raise RuntimeError("power loss")

            monkeypatch.setattr(env.journal, "saveNative", crash)
            with pytest.raises(RuntimeError):
                await env.market.submitSignedBid(command, "expired")
            env.rig.advance(int(task["terms"]["biddingClose"]))
            env.finalize()
            await env.restart()
            assert (await env.market.readOperation("expired"))["state"] == "UNKNOWN"
            op = env.journal.nativeOperation("expired")
            assert op["attempts"] == 0 and op["raw"]
            assert (await env.market.readBid(task["taskRef"], env.rig.agentRef(0)))["bid"] is None
        finally:
            await env.close()

    with localNode(tmp_path) as rpc:
        asyncio.run(run(rpc))

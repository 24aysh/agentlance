"""Synthetic registry and L1-backed fixture bridge. Never a production fallback."""

import asyncio
import sys
from copy import deepcopy

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from modules.adapters.a2a.profile import jsonBytes, strictJson
from modules.agent_client.ports import (
    AdapterError,
    checkIdentity,
    checkStamp,
    checkTaskView,
    closed,
    ensure,
    recordCheck,
)
from modules.agent_client.signing import contentDigest, verifyBidPermit
from modules.domain.records import agentKey, taskKey

SESSION_HEADER = "X-AgentLance-Fixture-Session"
READ_ARGS = {
    "readReputation": "taskRef agentRef",
    "readIdentity": "agentRef",
    "readTask": "taskRef",
    "readBid": "taskRef agentRef",
    "observeAward": "executionRef",
    "readSettlement": "taskRef",
    "checkContractSignature": "owner digest signature stamp gasLimit",
}


async def controlLoop(control):
    """Local parent-process test control, deliberately absent from HTTP routes."""
    reader = asyncio.StreamReader(limit=65536)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
    )
    try:
        while raw := await reader.readline():
            try:
                result = await control(strictJson(raw))
            except Exception as error:
                result = {"controlError": f"{type(error).__name__}: {error}"}
            print(jsonBytes(result).decode(), flush=True)
    finally:
        transport.close()


def commandRecord(name, **data):
    return {"schemaVersion": 1, "command": name, "input": data}


def unknownOperation(operationId):
    return {
        "operationId": operationId,
        "state": "UNKNOWN",
        "error": None,
        "events": [],
        "stamp": None,
    }


class FixtureMarket:
    def __init__(self, policy, schema, types, sessionId, identities, contents):
        from modules.market_core.state import CoreState

        self.policy, self.schema, self.types, self.sessionId = policy, schema, types, sessionId
        self.identities, self.contents = deepcopy(identities), dict(contents)
        self.state, self.finalizedState = CoreState(), CoreState()
        self.now, self.block, self.finalizedBlock, self.finalizedTime = 1000, 10, 10, 1000
        self.pendingCommands, self.events, self.operations = set(), [], {}
        self.contractResults = {}
        self.lock = asyncio.Lock()

    def stamp(self, finalized=False):
        block = self.finalizedBlock if finalized else self.block
        return {
            "source": "FIXTURE",
            "chainId": str(self.policy.chainId),
            "blockNumber": str(block),
            "blockHash": contentDigest(f"synthetic:{self.sessionId}:{block}".encode()),
            "blockTimestamp": str(self.finalizedTime if finalized else self.now),
            "finality": "FINALIZED" if block <= self.finalizedBlock else "PENDING",
        }

    def finalize(self):
        self.finalizedState = deepcopy(self.state)
        self.finalizedBlock, self.finalizedTime = self.block, self.now

    def setClock(self, now):
        ensure(type(now) is int and self.now <= now < 2**64, "Fixture clock must advance")
        self.now, self.block = now, self.block + 1
        self.finalize()

    async def readIdentity(self, agentRef):
        recordCheck(agentRef, "AgentRef", self.schema)
        value = self.identities.get(agentKey(agentRef))
        ensure(value is not None, "Unregistered fixture identity", "NOT_FOUND")
        return deepcopy(value) | {"agentRef": deepcopy(agentRef), "stamp": self.stamp(True)}

    async def checkContractSignature(self, owner, digest, signature, stamp, gasLimit=50000):
        ensure(gasLimit == 50000, "Contract signature gas cap")
        key = (owner, digest, signature, stamp["blockHash"])
        return deepcopy(self.contractResults.get(key, {"outcome": "REVERTED", "returnData": None}))

    def taskState(self, taskRef, finalized=False):
        recordCheck(taskRef, "TaskRef", self.schema)
        state = self.finalizedState if finalized else self.state
        task = state.tasks.get(taskKey(taskRef))
        ensure(task is not None, "Unknown fixture task", "NOT_FOUND")
        return task

    def view(self, taskRef, finalized=False):
        task = self.taskState(taskRef, finalized)
        allocation = task.allocation
        bid = (
            task.bids[agentKey(allocation["winner"])]
            if allocation and allocation["winner"]
            else None
        )
        return deepcopy(
            {
                "task": task.spec,
                "status": task.status,
                "allocation": allocation,
                "winningBid": bid,
                "ownWorkReserveAtoms": str(task.ownWorkReserveAtoms),
                "childrenCreated": task.childrenCreated,
                "activeChildren": task.activeChildren,
                "reservedChildBudgets": str(task.reservedChildBudgets),
                "committedChildPayouts": str(task.committedChildPayouts),
                "result": task.result,
                "receipt": task.receipt,
                "stamp": self.stamp(finalized),
            }
        )

    async def readTask(self, taskRef):
        return self.view(taskRef, True)

    async def readReputation(self, taskRef, agentRef):
        from modules.market_core.reputation import calculateProbability, snapshotCounters

        recordCheck(agentRef, "AgentRef", self.schema)
        ensure(
            agentRef["chainId"] == str(self.policy.chainId)
            and agentRef["identityRegistry"] == self.policy.identityRegistry,
            "Reputation namespace",
        )
        task = self.taskState(taskRef, True).spec
        successes, failures = snapshotCounters(
            self.finalizedState.reputation,
            agentRef,
            task["terms"]["taskFamily"],
            int(task["reputationSnapshotBlock"]),
        )
        return {
            "taskRef": taskRef,
            "agentRef": agentRef,
            "taskFamily": task["terms"]["taskFamily"],
            "snapshotBlock": task["reputationSnapshotBlock"],
            "counters": {"successes": str(successes), "failures": str(failures)},
            "p": calculateProbability(successes, failures),
            "stamp": self.stamp(True),
        }

    async def observeAward(self, executionRef):
        recordCheck(executionRef, "ExecutionRef", self.schema)
        return self.view(executionRef["taskRef"])

    async def readBid(self, taskRef, agentRef):
        recordCheck(agentRef, "AgentRef", self.schema)
        task = self.taskState(taskRef, True)
        return {"bid": deepcopy(task.bids.get(agentKey(agentRef))), "stamp": self.stamp(True)}

    async def readSettlement(self, taskRef):
        return {
            "receipt": deepcopy(self.taskState(taskRef, True).receipt),
            "stamp": self.stamp(True),
        }

    async def submit(self, caller, operationId, command, valueAtoms="0"):
        from modules.market_core.state import (
            Applied,
            CommandContext,
            IdentityObservation,
            SignatureObservation,
        )
        from modules.market_core.transitions import applyCommand

        ensure(isinstance(operationId, str) and 0 < len(operationId) <= 128, "Operation ID")
        recordCheck(command, "Command", self.schema)
        recordCheck(valueAtoms, "Uint96", self.schema)
        async with self.lock:
            key, body = (caller, operationId), (command, valueAtoms)
            if key in self.operations:
                previous, result = self.operations[key]
                ensure(previous == body, "Operation reused with different input", "CONFLICT")
                return deepcopy(result)
            context = {
                "caller": caller,
                "blockNumber": self.block + 1,
                "blockTimestamp": self.now,
                "valueAtoms": int(valueAtoms),
            }
            if command["command"] == "submitBid":
                offer, permit, signature = (
                    command["input"][field] for field in ("offer", "permit", "signature")
                )
                snapshot = self.identities.get(agentKey(offer["agentRef"]))
                context["identityObservation"] = IdentityObservation(
                    offer["agentRef"],
                    snapshot is not None,
                    snapshot["owner"] if snapshot else None,
                    snapshot["verifiedWallet"] if snapshot else None,
                )
                if permit is not None and snapshot is not None:
                    registry = await self.readIdentity(offer["agentRef"])
                    domain = {
                        "name": "AgentLance",
                        "version": "1",
                        "chainId": self.policy.chainId,
                        "verifyingContract": self.policy.market,
                    }
                    valid = await verifyBidPermit(
                        offer, permit, signature, registry, domain, self.types, self
                    )
                    context["signatureObservation"] = SignatureObservation(
                        "BidPermit",
                        self.policy.chainId,
                        self.policy.market,
                        permit["owner"],
                        deepcopy(permit),
                        signature,
                        valid,
                    )
            ensure(
                command["command"] not in {"settleVerdict", "withdrawCredit"},
                "No validator or native transfers in fixture bridge",
                "UNSUPPORTED",
            )
            result = applyCommand(self.state, command, CommandContext(**context), self.policy)
            response = {
                "operationId": operationId,
                "state": "REJECTED",
                "error": None,
                "events": [],
                "stamp": None,
            }
            if isinstance(result, Applied):
                self.state, self.block = result.state, self.block + 1
                self.events.extend(deepcopy(result.events))
                if command["command"] not in self.pendingCommands:
                    self.finalize()
                response.update(state="APPLIED", events=list(result.events), stamp=self.stamp())
            else:
                response["error"] = result.code
            self.operations[key] = deepcopy(body), deepcopy(response)
            return response

    def readOperation(self, caller, operationId):
        item = self.operations.get((caller, operationId))
        result = deepcopy(item[1]) if item else unknownOperation(operationId)
        if result["stamp"] and int(result["stamp"]["blockNumber"]) <= self.finalizedBlock:
            result["stamp"]["finality"] = "FINALIZED"
        return result


def fixtureApp(market, readToken, actors):
    app = FastAPI()

    @app.middleware("http")
    async def sessionGuard(request, callNext):
        if request.url.path.startswith("/fixture/content/"):
            return await callNext(request)
        if request.headers.get(SESSION_HEADER) != market.sessionId:
            return JSONResponse(
                {"kind": "CONFLICT", "detail": "Fixture session mismatch"}, status_code=409
            )
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if token != readToken and token not in actors:
            return JSONResponse(
                {"kind": "UNAUTHENTICATED", "detail": "Fixture credential required"},
                status_code=401,
                headers={SESSION_HEADER: market.sessionId},
            )
        request.state.actor = actors.get(token)
        try:
            response = await callNext(request)
        except AdapterError as error:
            status = {
                "NOT_FOUND": 404,
                "CONFLICT": 409,
                "FINALITY_CONFLICT": 409,
                "UNAVAILABLE": 503,
                "FORBIDDEN": 403,
            }.get(error.kind, 400)
            response = JSONResponse(
                {"kind": error.kind, "detail": error.detail}, status_code=status
            )
        response.headers[SESSION_HEADER] = market.sessionId
        return response

    async def body(request):
        data = bytearray()
        async for chunk in request.stream():
            ensure(len(data) + len(chunk) <= 65536, "Fixture body limit")
            data.extend(chunk)
        return strictJson(bytes(data))

    @app.post("/fixture/read")
    async def read(request: Request):
        value = await body(request)
        closed(value, "method args")
        ensure(value["method"] in READ_ARGS, "Unknown read method")
        closed(value["args"], READ_ARGS[value["method"]])
        return await getattr(market, value["method"])(**value["args"])

    @app.post("/fixture/commands")
    async def submit(request: Request):
        actor = request.state.actor
        ensure(actor is not None, "Write credential required", "FORBIDDEN")
        value = await body(request)
        closed(value, "operationId command valueAtoms")
        recordCheck(value["command"], "Command", market.schema)
        ensure(value["command"]["command"] in actor["commands"], "Command not allowed", "FORBIDDEN")
        return await market.submit(actor["address"], **value)

    @app.get("/fixture/operations/{operationId}")
    async def operation(operationId: str, request: Request):
        ensure(request.state.actor is not None, "Actor required", "FORBIDDEN")
        return market.readOperation(request.state.actor["address"], operationId)

    @app.get("/fixture/content/{name}")
    async def content(name: str):
        if name not in market.contents:
            return Response(status_code=404)
        return Response(market.contents[name], media_type="application/json")

    return app


class FixtureClient:
    def __init__(self, http, origin, sessionId, readToken, actorToken, schema):
        self.http, self.origin, self.sessionId = http, origin, sessionId
        self.readToken, self.actorToken, self.schema = readToken, actorToken, schema

    async def request(self, method, path, data=None, write=False):
        headers = {
            SESSION_HEADER: self.sessionId,
            "Authorization": "Bearer " + (self.actorToken if write else self.readToken),
            "Content-Type": "application/json",
        }
        try:
            async with asyncio.timeout(10):
                async with self.http.stream(
                    method,
                    self.origin + path,
                    headers=headers,
                    content=jsonBytes(data) if data is not None else None,
                ) as response:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        ensure(len(raw) + len(chunk) <= 1048576, "Bridge response limit")
                        raw.extend(chunk)
                    value = strictJson(bytes(raw))
                    ensure(
                        response.headers.get(SESSION_HEADER) == self.sessionId,
                        "Fixture session mismatch",
                        "CONFLICT",
                    )
                    if response.status_code != 200:
                        closed(value, "kind detail")
                        raise AdapterError(value["kind"], value["detail"])
                    return value
        except (TimeoutError, httpx.HTTPError, OSError) as error:
            raise AdapterError("UNAVAILABLE", "Fixture bridge transport failed") from error

    async def read(self, method, **args):
        return await self.request("POST", "/fixture/read", {"method": method, "args": args})

    async def readIdentity(self, agentRef):
        value = await self.read("readIdentity", agentRef=agentRef)
        checkIdentity(value, self.schema)
        ensure(value["agentRef"] == agentRef, "Identity reference")
        return value

    async def checkContractSignature(self, owner, digest, signature, stamp, gasLimit=50000):
        return await self.read(
            "checkContractSignature",
            owner=owner,
            digest=digest,
            signature=signature,
            stamp=stamp,
            gasLimit=gasLimit,
        )

    async def readTask(self, taskRef):
        value = await self.read("readTask", taskRef=taskRef)
        checkTaskView(value, self.schema)
        ensure(value["task"]["taskRef"] == taskRef, "Task reference")
        return value

    async def readReputation(self, taskRef, agentRef):
        from modules.agent_client.ports import checkReputation

        value = await self.read("readReputation", taskRef=taskRef, agentRef=agentRef)
        checkReputation(value, self.schema)
        ensure(
            value["taskRef"] == taskRef and value["agentRef"] == agentRef, "Reputation reference"
        )
        return value

    async def observeAward(self, executionRef):
        value = await self.read("observeAward", executionRef=executionRef)
        checkTaskView(value, self.schema)
        ensure(value["task"]["taskRef"] == executionRef["taskRef"], "Award reference")
        return value

    async def readBid(self, taskRef, agentRef):
        value = await self.read("readBid", taskRef=taskRef, agentRef=agentRef)
        closed(value, "bid stamp")
        checkStamp(value["stamp"], self.schema)
        if value["bid"] is not None:
            recordCheck(value["bid"], "Bid", self.schema)
            ensure(
                value["bid"]["offer"]["taskRef"] == taskRef
                and value["bid"]["offer"]["agentRef"] == agentRef,
                "Bid reference",
            )
        return value

    async def readSettlement(self, taskRef):
        value = await self.read("readSettlement", taskRef=taskRef)
        closed(value, "receipt stamp")
        checkStamp(value["stamp"], self.schema)
        if value["receipt"] is not None:
            recordCheck(value["receipt"], "SettlementReceipt", self.schema)
            ensure(value["receipt"]["taskRef"] == taskRef, "Receipt reference")
        return value

    def checkOperation(self, value, operationId):
        closed(value, "operationId state error events stamp")
        ensure(
            value["operationId"] == operationId
            and value["state"] in {"PENDING", "APPLIED", "REJECTED", "UNKNOWN"},
            "Operation binding",
        )
        ensure(isinstance(value["events"], list), "Events array")
        for event in value["events"]:
            recordCheck(event, "Event", self.schema)
        if value["stamp"] is not None:
            checkStamp(value["stamp"], self.schema)
        if value["state"] == "APPLIED":
            ensure(value["stamp"] is not None and value["error"] is None, "Applied operation")
        elif value["state"] == "REJECTED":
            recordCheck(value["error"], "Error", self.schema)
            ensure(not value["events"], "Rejected events")
        else:
            ensure(value["error"] is None and not value["events"], "Pending operation")
        return value

    async def write(self, command, operationId, valueAtoms="0"):
        recordCheck(command, "Command", self.schema)
        value = await self.request(
            "POST",
            "/fixture/commands",
            {"command": command, "operationId": operationId, "valueAtoms": valueAtoms},
            write=True,
        )
        return self.checkOperation(value, operationId)

    async def submitSignedBid(self, command, operationId):
        ensure(command["command"] == "submitBid", "Command kind")
        return await self.write(command, operationId)

    async def acceptAward(self, command, operationId):
        ensure(command["command"] == "acceptAward", "Command kind")
        return await self.write(command, operationId)

    async def publishChild(self, command, valueAtoms, operationId):
        ensure(command["command"] == "createChildTask", "Command kind")
        return await self.write(command, operationId, valueAtoms)

    async def commitResult(self, command, operationId):
        ensure(command["command"] == "submitResult", "Command kind")
        return await self.write(command, operationId)

    async def readOperation(self, operationId):
        value = await self.request("GET", "/fixture/operations/" + operationId, write=True)
        return self.checkOperation(value, operationId)

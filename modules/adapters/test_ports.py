"""Exercise all market capabilities over the authenticated fixture bridge."""

import asyncio
from copy import deepcopy

import pytest

from modules.adapters.fixtures import SESSION_HEADER, commandRecord
from modules.agent_client.ports import AdapterError


def testChildFundingAndAmbiguousResponse(l2, monkeypatch):
    async def run():
        l2.terms["delegation"] = {"maxDepth": 2, "maxChildren": 2}
        await l2.running()
        terms = deepcopy(l2.terms)
        terms.update(
            budgetAtoms="20",
            biddingClose="1500",
            allocationBy="1600",
            acceptBy="1720",
            resultBy="2000",
            validationBy="2200",
        )
        terms["delegation"] = {"maxDepth": 2, "maxChildren": 0}
        original = l2.market.publishChild
        calls = []

        async def lose(*args):
            calls.append(args)
            await original(*args)
            raise AdapterError("UNAVAILABLE", "Response lost")

        monkeypatch.setattr(l2.market, "publishChild", lose)
        with pytest.raises(AdapterError):
            await l2.participant.publishChild(l2.config["taskRef"], terms, "child-1")
        l2.restart()
        result = await l2.participant.publishChild(l2.config["taskRef"], terms, "child-1")
        assert result["state"] == "APPLIED" and len(calls) == 1
        parent = await l2.market.readTask(l2.config["taskRef"])
        assert (
            parent["childrenCreated"],
            parent["activeChildren"],
            parent["reservedChildBudgets"],
        ) == (1, 1, "20")
        childRef = l2.config["taskRef"] | {"taskId": "2"}
        child = await l2.market.readTask(childRef)
        assert child["task"]["parentRef"] == l2.config["taskRef"]
        assert child["task"]["requester"] == l2.config["signer"]
        assert l2.host.state.depositedAtoms == 120
        monkeypatch.setattr(l2.market, "publishChild", original)
        tooBig = terms | {"budgetAtoms": "31"}
        rejected = await l2.participant.publishChild(l2.config["taskRef"], tooBig, "child-2")
        assert rejected["state"] == "REJECTED" and rejected["error"] == "ENVELOPE_EXCEEDED"
        badDeadline = terms | {"validationBy": "2500"}
        rejected = await l2.participant.publishChild(l2.config["taskRef"], badDeadline, "child-3")
        assert rejected["state"] == "REJECTED"
        badFunding = commandRecord("createChildTask", parentRef=l2.config["taskRef"], terms=terms)
        rejected = await l2.market.publishChild(badFunding, "19", "short-deposit")
        assert rejected["state"] == "REJECTED" and rejected["error"] == "WRONG_VALUE"
        with pytest.raises(AdapterError, match="CONFLICT"):
            await l2.participant.publishChild(l2.config["taskRef"], tooBig, "child-1")
        assert l2.host.state.depositedAtoms == 120

    asyncio.run(run())


@pytest.mark.parametrize(
    "case", ["caller", "clock", "signature-flag", "readonly", "credential", "session"]
)
def testBridgeCannotInventAuthority(l2, case):
    async def run():
        await l2.award()
        payload = {
            "operationId": "forged",
            "command": commandRecord("acceptAward", executionRef=l2.ref, ownWorkReserveAtoms="0"),
            "valueAtoms": "0",
        }
        headers = {
            SESSION_HEADER: l2.config["sessionId"],
            "Authorization": "Bearer " + l2.config["actorToken"],
        }
        if case == "caller":
            payload["caller"] = l2.config["signer"]
        if case == "clock":
            payload["blockTimestamp"] = "1400"
        if case == "signature-flag":
            payload["valid"] = True
        if case == "readonly":
            headers["Authorization"] = "Bearer " + l2.config["readToken"]
        if case == "credential":
            headers["Authorization"] = "Bearer wrong"
        if case == "session":
            headers[SESSION_HEADER] = "wrong"
        response = await l2.http.post(
            l2.config["marketOrigin"] + "/fixture/commands", json=payload, headers=headers
        )
        assert response.status_code in {400, 401, 403, 409}
        assert l2.host.view(l2.config["taskRef"])["status"] == "AWARDED"

    asyncio.run(run())


@pytest.mark.parametrize(
    "case",
    [
        "direct",
        "operator",
        "permit",
        "transfer",
        "transfer-back",
        "payout",
        "profile",
        "signer",
        "nonce",
        "expiry",
        "contract-valid",
        "contract-short",
        "contract-revert",
        "contract-gas",
    ],
)
def testAdmissionAuthority(l2, case):
    from modules.agent_client.signing import buildBidPermitTypedData, typedDigest
    from modules.domain.records import agentKey

    async def run():
        await l2.create()
        l2.host.setClock(1100)
        identity = l2.host.identities[agentKey(l2.config["agentRef"])]
        owner = identity["owner"]
        offer = {
            "taskRef": l2.config["taskRef"],
            "agentRef": l2.config["agentRef"],
            "bidAtoms": "20",
            "executionSigner": l2.config["signer"],
            "payout": l2.config["payout"],
            "profileDigest": l2.journal.storeContent(l2.cardBytes),
        }
        permit = offer | {"owner": owner, "nonce": "1", "expiry": "1200"}
        signature = l2.sign(permit)
        caller = l2.config["signer"]
        if case in {"direct", "operator"}:
            permit = signature = None
            caller = owner if case == "direct" else l2.config["requester"]
        if case == "transfer":
            identity["owner"] = l2.config["requester"]
        if case == "transfer-back":
            identity["owner"] = l2.config["requester"]
            identity["owner"] = owner
        if case in {"payout", "profile", "signer"}:
            field = {"payout": "payout", "profile": "profileDigest", "signer": "executionSigner"}[
                case
            ]
            offer[field] = "0x" + "42" * (32 if case == "profile" else 20)
        if case == "nonce":
            permit["nonce"] = "2"
        if case == "expiry":
            l2.host.setClock(1200)
        if case.startswith("contract"):
            identity["ownerHasCode"] = True
            domain = {
                "name": "AgentLance",
                "version": "1",
                "chainId": 31337,
                "verifyingContract": l2.config["taskRef"]["market"],
            }
            digest = typedDigest(buildBidPermitTypedData(permit, domain, l2.types))
            outcomes = {
                "contract-valid": {"outcome": "RETURNED", "returnData": "0x1626ba7e" + "00" * 28},
                "contract-short": {"outcome": "RETURNED", "returnData": "0x1626ba7e"},
                "contract-revert": {"outcome": "REVERTED", "returnData": None},
                "contract-gas": {"outcome": "OUT_OF_GAS", "returnData": None},
            }
            l2.host.contractResults[
                (owner, digest, signature, l2.host.stamp(True)["blockHash"])
            ] = outcomes[case]
        command = commandRecord("submitBid", offer=offer, permit=permit, signature=signature)
        result = await l2.host.submit(caller, "admission", command)
        valid = case in {"direct", "permit", "transfer-back", "contract-valid"}
        assert result["state"] == ("APPLIED" if valid else "REJECTED"), result
        assert await l2.host.submit(caller, "admission", command) == result
        if valid:
            assert len(l2.host.state.usedNonces) == (0 if case == "direct" else 1)
            duplicate = await l2.host.submit(caller, "another-operation", command)
            assert duplicate["state"] == "REJECTED"

    asyncio.run(run())


def testRestoredWalletNewOwner(l2):
    from eth_account import Account

    from modules.domain.records import agentKey

    async def run():
        await l2.create()
        l2.host.setClock(1100)
        identity = l2.host.identities[agentKey(l2.config["agentRef"])]
        l2.config["ownerKey"] = "0x" + "0" * 63 + "2"
        identity["owner"] = Account.from_key(l2.config["ownerKey"]).address.lower()
        identity["verifiedWallet"] = None
        with pytest.raises(AdapterError, match="WALLET_UNSET"):
            await l2.participant.prepareBid(l2.config["taskRef"], "20", l2.sign, "1", "1200")
        identity["verifiedWallet"] = l2.config["payout"]
        result = await l2.participant.prepareBid(l2.config["taskRef"], "20", l2.sign, "1", "1200")
        assert result["state"] == "APPLIED"
        admitted = await l2.market.readBid(l2.config["taskRef"], l2.config["agentRef"])
        assert admitted["bid"]["ownerAtBid"] == identity["owner"]

    asyncio.run(run())


def testContractSignatureReadBoundary(l2):
    async def run():
        snapshot = await l2.market.readIdentity(l2.config["agentRef"])
        digest = "0x" + "aa" * 32
        signature = "0x1234"
        expected = {"outcome": "RETURNED", "returnData": "0x1626ba7e" + "00" * 28}
        l2.host.contractResults[
            (snapshot["owner"], digest, signature, snapshot["stamp"]["blockHash"])
        ] = expected
        assert (
            await l2.market.checkContractSignature(
                snapshot["owner"], digest, signature, snapshot["stamp"]
            )
            == expected
        )
        with pytest.raises(AdapterError):
            await l2.market.checkContractSignature(
                snapshot["owner"], digest, signature, snapshot["stamp"], gasLimit=50001
            )

    asyncio.run(run())

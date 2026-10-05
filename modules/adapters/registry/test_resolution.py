"""Registration and card are untrusted data, not execution authority."""

import asyncio
from copy import deepcopy

import pytest

from modules.adapters.a2a.profile import jsonBytes, strictJson
from modules.adapters.registry.resolution import resolveProfile, selectInterface
from modules.agent_client.ports import AdapterError
from modules.domain.records import agentKey


def testResolvedExactBytes(l2):
    async def run():
        resolved = await resolveProfile(
            l2.config["agentRef"],
            l2.market,
            l2.content,
            l2.schema,
            31337,
            l2.config["agentRef"]["identityRegistry"],
        )
        assert resolved.cardBytes == l2.cardBytes
        assert resolved.profile["agentRef"] == l2.config["agentRef"]
        assert resolved.registry["verifiedWallet"] == l2.config["payout"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "field,value",
    [
        ("supportedInterfaces", []),
        (
            "supportedInterfaces",
            [
                {
                    "url": "https://example.com",
                    "protocolVersion": "0.3",
                    "protocolBinding": "HTTP+JSON",
                }
            ],
        ),
        ("capabilities", {}),
        ("skills", []),
        ("defaultInputModes", ["text/plain"]),
        ("name", ""),
        ("skills", [None]),
        ("capabilities", []),
        ("supportedInterfaces", [None]),
        ("unknown", 1),
    ],
)
def testMalformedCard(l2, field, value):
    card = strictJson(l2.cardBytes)
    card[field] = value
    with pytest.raises(AdapterError):
        selectInterface(card)


@pytest.mark.parametrize(
    "change",
    [
        "identity",
        "endpoints",
        "endpoint-type",
        "inactive",
        "bad-json",
        "wallet",
        "registry",
        "missing",
        "unknown-extension",
        "tenant",
    ],
)
def testResolutionFailure(l2, change):
    async def run():
        registry = l2.config["agentRef"]["identityRegistry"]
        identity = l2.host.identities[agentKey(l2.config["agentRef"])]
        registration = strictJson(l2.host.contents["registration.json"])
        if change == "identity":
            registration["registrations"][0]["agentId"] = 8
        if change == "endpoints":
            registration["services"].append(
                {"name": "A2A", "endpoint": "https://elsewhere.example/card"}
            )
        if change == "endpoint-type":
            registration["services"][0]["endpoint"] = {}
        if change == "inactive":
            registration["active"] = False
        l2.host.contents["registration.json"] = jsonBytes(registration)
        if change == "bad-json":
            l2.host.contents["registration.json"] = b'{"active":true,"active":true}'
        if change == "wallet":
            identity["verifiedWallet"] = None
        if change == "registry":
            registry = "0x" + "42" * 20
        if change == "missing":
            l2.host.identities.clear()
        if change in {"unknown-extension", "tenant"}:
            card = strictJson(l2.cardBytes)
            if change == "tenant":
                card["supportedInterfaces"][0]["tenant"] = "tenant"
            else:
                card["capabilities"]["extensions"].append({"uri": "urn:unknown", "required": True})
            l2.cardBytes = jsonBytes(card)
            l2.compose()
        if change == "wallet":
            await l2.create()
            with pytest.raises(AdapterError, match="WALLET_UNSET"):
                await l2.participant.prepareBid(l2.config["taskRef"], "20", l2.sign, "1", "1200")
        else:
            with pytest.raises(AdapterError):
                await resolveProfile(
                    l2.config["agentRef"], l2.market, l2.content, l2.schema, 31337, registry
                )
        assert not l2.calls

    asyncio.run(run())


@pytest.mark.parametrize("agentId", ["0", str(2**256 - 1)])
def testIdentityIntegerRange(l2, agentId):
    async def run():
        original = deepcopy(l2.config["agentRef"])
        ref = original | {"agentId": agentId}
        l2.host.identities[agentKey(ref)] = l2.host.identities[agentKey(original)]
        registration = strictJson(l2.host.contents["registration.json"])
        registration["registrations"][0]["agentId"] = int(agentId)
        l2.host.contents["registration.json"] = jsonBytes(registration)
        result = await resolveProfile(
            ref, l2.market, l2.content, l2.schema, 31337, ref["identityRegistry"]
        )
        assert result.profile["agentRef"]["agentId"] == agentId

    asyncio.run(run())

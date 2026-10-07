"""Eligibility never spends on models; observation shares existing execution claims."""

import asyncio

import pytest

from modules.agent_client.discovery import checkFilterPolicy, filterTask
from modules.agent_client.ports import AdapterError


def localPolicy(l2):
    return {
        "version": "test-1",
        "supportedDigests": list(l2.participant.supportedDigests),
        "requiredSkills": ["structured-output-v1"],
        "advertisedSkills": ["structured-output-v1"],
        "minBudgetAtoms": "0",
        "maxBudgetAtoms": str(2**96 - 1),
        "leadTimeSeconds": 0,
        "allowRequesters": [],
        "denyRequesters": [],
    }


@pytest.mark.parametrize(
    "case,expected",
    [
        ("supported", ("ELIGIBLE", "SUPPORTED")),
        ("capacity", ("WAIT", "CAPACITY")),
        ("unknown-capacity", ("WAIT", "CAPACITY")),
        ("capability", ("IGNORE", "CAPABILITY")),
        ("template", ("IGNORE", "TEMPLATE")),
        ("budget", ("IGNORE", "BUDGET")),
        ("deadline", ("IGNORE", "DEADLINE")),
        ("policy", ("IGNORE", "POLICY")),
        ("pending", ("WAIT", "FINALITY")),
        ("closed", ("IGNORE", "CLOSED")),
    ],
)
def testCheapEligibility(l2, case, expected):
    async def run():
        if case == "closed":
            await l2.award()
        else:
            await l2.create()
        policy, capacity = localPolicy(l2), 1
        view = await l2.market.readTask(l2.config["taskRef"])
        if case == "capacity":
            capacity = 0
        if case == "unknown-capacity":
            capacity = None
        if case == "capability":
            policy["advertisedSkills"] = []
        if case == "template":
            policy["supportedDigests"][0] = "0x" + "00" * 32
        if case == "budget":
            policy["minBudgetAtoms"] = "101"
        if case == "deadline":
            policy["leadTimeSeconds"] = 200
        if case == "policy":
            policy["denyRequesters"] = [view["task"]["requester"]]
        if case == "pending":
            view["stamp"]["finality"] = "PENDING"
        result = filterTask(view, policy, capacity, l2.schema)
        assert (result["state"], result["reason"]) == expected
        assert l2.calls == []

    asyncio.run(run())


@pytest.mark.parametrize(
    "change",
    ["negative-lead", "numeric-budget", "reversed-budget", "duplicate-skill", "foreign-field"],
)
def testInvalidLocalPolicy(l2, change):
    policy = localPolicy(l2)
    if change == "negative-lead":
        policy["leadTimeSeconds"] = -1
    if change == "numeric-budget":
        policy["minBudgetAtoms"] = 0
    if change == "reversed-budget":
        policy.update(minBudgetAtoms="2", maxBudgetAtoms="1")
    if change == "duplicate-skill":
        policy["requiredSkills"] *= 2
    if change == "foreign-field":
        policy["chooseWinner"] = True
    with pytest.raises(AdapterError):
        checkFilterPolicy(policy, l2.schema)


def testOwnWatcherAdmissionAndHintsShareClaim(l2):
    async def run():
        await l2.award()
        own = await l2.participant.observeOwnAward(l2.ref)
        hint = await l2.participant.receiveHint(l2.message)
        assert own["correlation"] == hint["correlation"]
        l2.host.setClock(1400)
        for _ in range(5):
            await l2.participant.tick()
        l2.restart()
        await l2.participant.observeOwnAward(l2.ref)
        await l2.participant.tick()
        assert len(l2.calls) == 1

    asyncio.run(run())


def testOwnWatcherCannotAdmitPendingOrWrongWinner(l2):
    async def run():
        await l2.award(pending=True)
        with pytest.raises(AdapterError, match="finalized"):
            await l2.participant.observeOwnAward(l2.ref)
        assert l2.journal.rows() == []
        l2.host.setClock(1400)
        l2.participant.signer = "0x" + "ef" * 20
        with pytest.raises(AdapterError):
            await l2.participant.observeOwnAward(l2.ref)
        assert not l2.calls and l2.journal.rows() == []

    asyncio.run(run())

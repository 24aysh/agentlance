import asyncio
from copy import deepcopy

import pytest
from hypothesis import given
from hypothesis import strategies as st

from modules.adapters.a2a.profile import jsonBytes
from modules.agent_client.ports import AdapterError
from modules.agent_client.signing import contentDigest
from modules.execution.rules import checkOutput, executionDigest, parentState, validateChildPlan
from modules.market_core.accounting import availableChildBudget
from tests.layer2_support import Layer2Rig
from tests.layer6_support import executionProfile


def testChildEnvelopeGoldenAndPlanBoundaries(tmp_path):
    async def scenario():
        rig = Layer2Rig(tmp_path)
        profile = executionProfile(rig.participant, manager=True)
        profile["delegation"].update(
            depositAtoms="100", childBudgetAtoms="30", templates=[list(rig.digests)]
        )
        profile["runtime"]["configDigest"] = executionDigest(profile)
        try:
            await rig.award()
            await rig.participant.receiveHint(rig.message)
            await rig.participant.tick()
            view = await rig.market.observeAward(rig.ref)
            view["allocation"]["reservedAtoms"] = "100"
            view["ownWorkReserveAtoms"] = "20"
            view["task"]["terms"]["delegation"] = {"maxDepth": 2, "maxChildren": 4}
            view["task"]["terms"]["resultBy"] = "2000"
            terms = deepcopy(view["task"]["terms"])
            raw = jsonBytes({"value": 7})
            terms.update(
                input={"uri": "https://fixture.example/input.json", "digest": contentDigest(raw)},
                budgetAtoms="30",
                biddingClose="1201",
                allocationBy="1202",
                acceptBy="1322",
                resultBy="1400",
                validationBy="1500",
                refundAddress=rig.participant.signer,
                delegation={"maxDepth": 2, "maxChildren": 0},
            )
            slots = [
                {
                    "id": "left",
                    "terms": terms,
                    "input": {"value": 7},
                    "fallbackInput": {"value": 7},
                },
                {
                    "id": "right",
                    "terms": terms | {"budgetAtoms": "40"},
                    "input": {"value": 7},
                    "fallbackInput": {"value": 7},
                },
            ]
            from modules.economics.records import recordDigest

            plan = {
                "version": 1,
                "executionRef": rig.ref,
                "profileDigest": recordDigest(profile),
                "mode": "DELEGATE",
                "slots": slots,
            }
            assert (
                validateChildPlan(plan, view, profile, rig.schema, rig.participant.signer) == plan
            )
            with pytest.raises(AdapterError, match="ENVELOPE"):
                validateChildPlan(
                    plan, view, profile, rig.schema, rig.participant.signer, [{"budgetAtoms": "11"}]
                )
            for field, value in (
                ("refundAddress", rig.config["requester"]),
                ("validationBy", "1881"),
                ("acceptBy", "1203"),
                ("budgetAtoms", "0"),
                ("validator", rig.participant.signer),
            ):
                invalid = deepcopy(plan)
                invalid["slots"][0]["terms"][field] = value
                with pytest.raises(AdapterError):
                    validateChildPlan(invalid, view, profile, rig.schema, rig.participant.signer)
            invalid = deepcopy(plan)
            invalid["slots"][1]["id"] = "left"
            with pytest.raises(AdapterError):
                validateChildPlan(invalid, view, profile, rig.schema, rig.participant.signer)
            view.update(reservedChildBudgets="0", committedChildPayouts="60", childrenCreated=2)
            assert availableChildBudget(parentState(view)) == 20
        finally:
            await rig.http.aclose()
            rig.journal.close()

    asyncio.run(scenario())


@given(st.integers(0, 100000), st.integers(0, 100000), st.integers(0, 100000))
def testEnvelopeConservation(reserve, pending, paid):
    from modules.market_core.state import TaskState

    parent = TaskState(
        spec={},
        allocation={"reservedAtoms": str(reserve + pending + paid + 20)},
        ownWorkReserveAtoms=reserve,
        reservedChildBudgets=pending,
        committedChildPayouts=paid,
    )
    assert availableChildBudget(parent) == 20


@pytest.mark.parametrize(
    "raw",
    [
        b'{"value":"/etc/passwd"}',
        b'{"value":true}',
        b'{"value":1001}',
        b'{"value":7,"extra":8}',
        b"NaN",
    ],
)
def testOutputImportHasNoFileAuthority(raw):
    from tests.layer3_support import readJson

    with pytest.raises((AdapterError, ValueError)):
        checkOutput(raw, readJson("specs/fixtures/layer-2/output-schema.json"))

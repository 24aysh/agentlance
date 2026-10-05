from copy import deepcopy

import pytest
from hypothesis import given
from hypothesis import strategies as st

from conftest import OBJECTS, cases
from modules.market_core.market import calculateScore, evaluateAuction, updateTopTwo


@pytest.mark.parametrize("case", cases("market"), ids=lambda case: case["id"])
def testMarketGoldens(case):
    before = deepcopy(case["input"])
    assert evaluateAuction(case["input"]) == case["expected"]
    assert case["input"] == before


@given(st.lists(st.tuples(st.integers(0, 1000000), st.integers(0, 100)), max_size=12))
def testOrderPriceAndMonotonicity(rows):
    bids, top = [], ()
    for index, (p, amount) in enumerate(rows):
        offer = {
            "agentRef": {**OBJECTS["AgentRef"], "agentId": str(index)},
            "bidAtoms": str(amount),
            "p": p,
        }
        bids.append(offer)
        top = updateTopTwo(top, {"offer": offer, "p": p, "score": str(p * 100 - 1000000 * amount)})
        assert calculateScore(p, 1, 100, amount + 1) <= calculateScore(p, 1, 100, amount)
    ordered = sorted(
        bids,
        key=lambda bid: (
            -(bid["p"] * 100 - 1000000 * int(bid["bidAtoms"])),
            int(bid["agentRef"]["agentId"]),
        ),
    )
    assert [row["offer"] for row in top] == ordered[:2]
    auction = {"budgetAtoms": "100", "alphaNum": "1", "alphaDen": "100", "bids": bids}
    result = evaluateAuction(auction)
    assert evaluateAuction({**auction, "bids": bids[::-1]}) == result
    if result["outcome"] == "AWARDED":
        winning = next(bid for bid in bids if bid["agentRef"] == result["winner"])
        assert int(winning["bidAtoms"]) <= int(result["reservedAtoms"]) <= 100
        critical, second = int(result["criticalAtoms"]), int(result["secondScore"])
        assert critical * 1000000 <= winning["p"] * 100 - second < (critical + 1) * 1000000
        assert int(result["winningScore"]) > 0

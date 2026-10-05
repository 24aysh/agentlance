"""Integer allocation and critical price over an immutable auction snapshot."""

from copy import deepcopy
from math import gcd

from modules.domain.records import (
    ProtocolViolation,
    agentKey,
    decodeUnsigned,
    require,
    unsigned,
)

Q = 1_000_000


def validateAlpha(alphaNum: int, alphaDen: int) -> None:
    unsigned(alphaNum, 96)
    unsigned(alphaDen, 96)
    require(alphaNum > 0 and alphaDen > 0 and gcd(alphaNum, alphaDen) == 1, "INVALID_ALPHA")


def calculateScore(p: int, alphaNum: int, alphaDen: int, bidAtoms: int) -> int:
    unsigned(p, 32)
    require(p <= Q, "INVALID_RANGE")
    unsigned(bidAtoms, 96)
    validateAlpha(alphaNum, alphaDen)
    return p * alphaDen - Q * alphaNum * bidAtoms


def updateTopTwo(topTwo: tuple, admittedBid: dict) -> tuple:
    ordered = sorted(
        (*topTwo, admittedBid),
        key=lambda bid: (-int(bid["score"]), agentKey(bid["offer"]["agentRef"])),
    )
    return deepcopy(tuple(ordered[:2]))


def priceAuction(budget: int, alphaNum: int, alphaDen: int, topTwo: tuple) -> dict:
    if not topTwo or int(topTwo[0]["score"]) <= 0:
        return {
            "outcome": "UNALLOCATED",
            "winner": None,
            "winningScore": None,
            "secondScore": None,
            "criticalAtoms": None,
            "reservedAtoms": "0",
        }
    winner = topTwo[0]
    second = max(0, int(topTwo[1]["score"])) if len(topTwo) == 2 else 0
    critical = (winner["p"] * alphaDen - second) // (Q * alphaNum)
    return {
        "outcome": "AWARDED",
        "winner": deepcopy(winner["offer"]["agentRef"]),
        "winningScore": winner["score"],
        "secondScore": str(second),
        "criticalAtoms": str(critical),
        "reservedAtoms": str(min(budget, critical)),
    }


def evaluateAuction(auction: dict) -> dict:
    try:
        budget = decodeUnsigned(auction["budgetAtoms"], 96)
        require(budget > 0, "INVALID_BUDGET")
        numerator = decodeUnsigned(auction["alphaNum"], 96)
        denominator = decodeUnsigned(auction["alphaDen"], 96)
        validateAlpha(numerator, denominator)
        seen, topTwo = set(), ()
        for bid in auction["bids"]:
            key = agentKey(bid["agentRef"])
            amount = decodeUnsigned(bid["bidAtoms"], 96)
            score = calculateScore(bid["p"], numerator, denominator, amount)
            require(key not in seen, "DUPLICATE_AGENT")
            require(amount <= budget, "BID_OVER_BUDGET")
            seen.add(key)
            topTwo = updateTopTwo(topTwo, {"offer": bid, "p": bid["p"], "score": str(score)})
        result = priceAuction(budget, numerator, denominator, topTwo)
        if result["outcome"] == "UNALLOCATED":
            return {"outcome": "UNALLOCATED", "winner": None, "reservedAtoms": "0"}
        return result
    except ProtocolViolation as error:
        return {"outcome": "REJECTED", "reason": error.code}


def calculateAllocation(taskSpec: dict, topTwo: tuple, allocatedAt: int) -> dict:
    unsigned(allocatedAt, 64)
    terms = taskSpec["terms"]
    result = priceAuction(
        int(terms["budgetAtoms"]), int(terms["alphaNum"]), int(terms["alphaDen"]), topTwo
    )
    return {
        "schemaVersion": 1,
        "taskRef": deepcopy(taskSpec["taskRef"]),
        "mechanismVersion": 1,
        **result,
        "awardId": int(result["outcome"] == "AWARDED"),
        "allocatedAt": str(allocatedAt),
    }

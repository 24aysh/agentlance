"""An operator-selected reservation-cost heuristic; never protocol authority."""

from modules.agent_client.ports import closed, ensure, recordCheck
from modules.economics.estimator import validateEstimate
from modules.economics.records import MON, Q, atoms, checkPolicy, recordDigest, textField
from modules.market_core.market import calculateScore


def decideBid(task, estimate, reputation, overhead, policy, now, schema):
    checkPolicy(policy)
    recordCheck(task, "TaskSpec", schema)
    reason, amount, inputs = None, None, {}
    expires = int(task["terms"]["biddingClose"]) - policy["leadTimeSeconds"]
    if now >= expires:
        reason = "EXPIRED"
    elif estimate is None:
        reason = "COST_UNKNOWN"
    else:
        validateEstimate(estimate, schema)
        ensure(
            estimate["taskRef"] == task["taskRef"]
            and estimate["inputDigest"] == task["terms"]["input"]["digest"],
            "Estimate task binding",
        )
        expires = min(expires, int(estimate["expiresAt"]))
        if not int(estimate["createdAt"]) <= now < expires:
            reason = "EXPIRED"
        elif estimate["tail"] != "BOUNDED" or estimate["unit"] != MON:
            reason = "UNBOUNDED_COST" if estimate["tail"] == "UNBOUNDED" else "COST_UNKNOWN"
    if reason is None:
        if reputation is None:
            reason = "REPUTATION_UNAVAILABLE"
        else:
            from modules.agent_client.ports import checkReputation

            checkReputation(reputation, schema)
            ensure(
                reputation["taskRef"] == task["taskRef"]
                and reputation["snapshotBlock"] == task["reputationSnapshotBlock"]
                and reputation["taskFamily"] == task["terms"]["taskFamily"],
                "Reputation task binding",
            )
            p, q = reputation["p"], min(reputation["p"], policy["successPpmCap"])
            closed(overhead, "gasAtoms validationAtoms forecastAtoms expiresAt source")
            textField(overhead["source"], 1024)
            expires = min(expires, atoms(overhead["expiresAt"], 64))
            values = [overhead[k] for k in ("gasAtoms", "validationAtoms", "forecastAtoms")]
            if now >= expires:
                reason = "EXPIRED"
            elif any(value is None for value in values):
                reason = "COST_UNKNOWN"
            elif q == 0:
                reason = "ZERO_SUCCESS_PROXY"
            else:
                c, h = atoms(estimate[f"p{policy['quantile']}Atoms"]), sum(atoms(v) for v in values)
                numerator = (c + h) * (Q + policy["marginPpm"])
                amount = (numerator + q - 1) // q
                inputs = {
                    "costAtoms": str(c),
                    "overheadAtoms": str(h),
                    "p": p,
                    "q": q,
                    "marginPpm": policy["marginPpm"],
                    "quantile": policy["quantile"],
                }
                if amount >= 2**96 or amount > int(task["terms"]["budgetAtoms"]):
                    reason = "OVER_BUDGET"
                elif (
                    calculateScore(
                        p, int(task["terms"]["alphaNum"]), int(task["terms"]["alphaDen"]), amount
                    )
                    <= 0
                ):
                    reason = "NONPOSITIVE_SCORE"
    decision = {
        "taskRef": task["taskRef"],
        "estimateId": estimate["estimateId"] if estimate else None,
        "policyDigest": recordDigest(policy),
        "action": "ABSTAIN" if reason else "BID",
        "bidAtoms": None if reason else str(amount),
        "reasons": [reason or "BID_READY"],
        "inputs": inputs,
        "reputation": reputation,
        "createdAt": str(now),
        "expiresAt": str(expires),
    }
    return decision

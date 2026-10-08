"""Agent-local receipt/usage join; observed scope never implies whole-agent profit."""

from modules.agent_client.ports import ensure
from modules.economics.records import recordDigest
from modules.validation.observer import OutcomeObserver


class Reconciler:
    def __init__(self, history, watcher, store):
        ensure(
            history.store.journal is store.journal, "Reconciliation must share the agent journal"
        )
        self.history, self.watcher, self.store = history, watcher, store
        self.market = history.market
        self.observer = OutcomeObserver(self.market, watcher, store)

    async def tick(self, limit=8):
        ensure(type(limit) is int and 1 <= limit <= 32, "Reconciliation tick limit")
        await self.observer.consume()
        reports = {
            recordDigest(row["report"]["executionRef"]): row
            for row in self.history.store.activeReports()
        }
        progress = self.watcher.progress()
        tasks = self.watcher.taskRows(progress["stamp"]["blockNumber"]) if progress else []
        own = [
            row
            for _, row in self.store.rows("receipt")
            if row["receipt"]["winner"] == self.history.store.agentRef
        ]
        cursor = self.store.get("cursor", "reconciliation")
        start = int(cursor["position"][0]) % len(own) if cursor and own else 0
        for offset in range(min(limit, len(own))):
            index = (start + offset) % len(own)
            projection = own[index]
            self.store.save(
                "cursor", "reconciliation", {"version": 1, "position": [str(index + 1), "0"]}
            )
            receipt, task = projection["receipt"], projection["task"]
            ref = {"taskRef": receipt["taskRef"], "awardId": 1}
            key = recordDigest(ref)
            if self.history.store.get("usage_receipts", key) is None:
                await self.history.attachSettlement(ref, receipt, projection["stamp"])
            selected = reports.get(key)
            view = await self.market.readTask(task["taskRef"])
            if view["receipt"] != receipt:
                self.store.journal.halt("Reconciliation receipt changed")
            children = [
                child["task"] for child in tasks if child["task"]["parentRef"] == task["taskRef"]
            ]
            childReceipts = []
            for child in children:
                childView = await self.market.readTaskAt(child["taskRef"], view["stamp"])
                childReceipts.append({"taskRef": child["taskRef"], "receipt": childView["receipt"]})
            completeChildren = len(children) == view["childrenCreated"] and all(
                c["receipt"] is not None for c in childReceipts
            )
            report = selected["report"] if selected else None
            bundle = (
                self.history.store.get("economic_estimates", report["estimateId"])
                if report and report["estimateId"]
                else None
            )
            inputs = {
                "operator": self.history.store.operator,
                "receiptDigest": recordDigest(receipt),
                "receiptStamp": projection["stamp"],
                "reportId": report["reportId"] if report else None,
                "reportDigest": recordDigest(selected) if selected else None,
                "estimateId": report["estimateId"] if report else None,
                "estimateDigest": recordDigest(bundle) if bundle else None,
                "childrenCreated": view["childrenCreated"],
                "children": childReceipts,
            }
            version = recordDigest([ref, inputs])
            if self.store.get("reconciliation", version):
                continue
            missing = [
                "INCOMPLETE_WHOLE_COST_SCOPE",
                "UNREPORTED_VALIDATOR_COST",
                "UNREPORTED_GAS_AND_PARTICIPATION_COST",
            ]
            if not completeChildren:
                missing.append("UNKNOWN_CHILD_PAYMENTS")
            ownCost = None
            if report is None:
                missing.append("MISSING_USAGE")
            elif report["completeness"] != "COMPLETE":
                missing.append("INCOMPLETE_USAGE")
            else:
                items = [item for item in report["items"] if item["category"] == "EXECUTION"]
                if all(
                    item["costAtoms"] is not None
                    and item["unit"] == {"currency": "MON", "decimals": 18}
                    for item in items
                ):
                    ownCost = str(sum(int(item["costAtoms"]) for item in items))
                else:
                    missing.append("UNKNOWN_EXECUTION_COST_OR_UNIT")
            observations = {
                "scope": "OWN_EXECUTION",
                "receipt": receipt,
                "creditedPayoutAtoms": receipt["paidAtoms"],
                "creditedRefundAtoms": receipt["refundAtoms"],
                "withdrawnAtoms": None,
                "directChildPaidAtoms": str(
                    sum(int(c["receipt"]["paidAtoms"]) for c in childReceipts)
                )
                if completeChildren
                else None,
                "ownReportedExecutionAtoms": ownCost,
                "report": report,
                "context": selected["context"] if selected else None,
                "comparison": self.history.evaluateForecasts([ref]),
                "wholeAgentProfitAtoms": None,
                "systemUtility": None,
            }
            body = {
                "version": 1,
                "executionRef": ref,
                "inputs": inputs,
                "observations": observations,
                "missing": missing,
            }
            with self.store.db:
                self.store.put("reconciliation", version, body, immutable=True)
                self.store.put(
                    "selection",
                    recordDigest([self.history.store.operator, ref]),
                    {"version": 1, "key": version},
                )

    def readReconciliation(self, executionRef):
        pointer = self.store.get(
            "selection", recordDigest([self.history.store.operator, executionRef])
        )
        return self.store.get("reconciliation", pointer["key"]) if pointer else None

from copy import deepcopy

import pytest

from conftest import example
from modules.adapters.storage.economics import EconomicsStore
from modules.adapters.storage.journal import Journal
from modules.agent_client.ports import AdapterError
from modules.economics.records import recordDigest
from tests.layer3_support import readJson


def testDurableBudgetCrashAndClock(tmp_path):
    path = tmp_path / "budget.sqlite"
    agent, operator = example("AgentRef"), example("TaskSpec")["requester"]
    forecast = readJson("specs/fixtures/layer-5/economics.json")["forecast"] | {
        "enabled": True,
        "dailyBudgetAtoms": "2",
    }
    journal = Journal(path, {"mode": "fixture"})
    store = EconomicsStore(journal, agent, operator)
    try:
        assert store.reserveForecast("first", forecast, recordDigest({}), 1000)
        assert not store.reserveForecast("first", forecast, recordDigest({}), 1000)
        assert not store.reserveForecast("second", forecast, recordDigest({}), 1000)
    finally:
        journal.close()
    journal = Journal(path, {"mode": "fixture"})
    store = EconomicsStore(journal, agent, operator)
    try:
        first = store.get("forecast_attempts", "first")
        assert (
            first["state"] == "UNKNOWN"
            and first["reservedAtoms"] == "1"
            and first["actualCostAtoms"] is None
        )
        assert not store.reserveForecast("first", forecast, recordDigest({"new": "version"}), 1000)
        assert store.reserveForecast("second", forecast, recordDigest({}), 1000)
        store.finishForecast("second", {"usage": "not billing evidence"})
        assert not store.reserveForecast("third", forecast, recordDigest({}), 1000)
        with pytest.raises(AdapterError, match="backwards"):
            store.reserveForecast("third", forecast, recordDigest({}), 999)
        assert store.reserveForecast("third", forecast, recordDigest({}), 86401)
        store.finishForecast("third", reason="TIMEOUT")
        other = deepcopy(forecast)
        other["feeUnit"]["currency"] = "USD"
        with pytest.raises(AdapterError, match="conflict"):
            store.reserveForecast("fourth", other, recordDigest({}), 86401)
    finally:
        journal.close()


def testEconomicsMigrationAndStorageBackpressure(tmp_path):
    journal = Journal(tmp_path / "state.sqlite", {"mode": "fixture"})
    try:
        journal.db.execute("PRAGMA user_version=2")
        journal.db.commit()
        digest = journal.storeContent(b"retained")
    finally:
        journal.close()
    journal = Journal(tmp_path / "state.sqlite", {"mode": "fixture"})
    try:
        assert journal.db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert journal.readContent(digest) == b"retained"
        store = EconomicsStore(
            journal, example("AgentRef"), example("TaskSpec")["requester"], maxRows=1
        )
        with pytest.raises(AdapterError, match="capacity"):
            store.enqueueCandidate(example("TaskSpec"), {}, recordDigest({}))
        assert store.items("economic_candidates") == []
    finally:
        journal.close()


def testExpenseCannotBeReattributedToAnotherExecution(tmp_path):
    journal = Journal(tmp_path / "reports.sqlite", {"mode": "fixture"})
    try:
        store = EconomicsStore(journal, example("AgentRef"), example("TaskSpec")["requester"])
        report = example("CostReport")
        context = {"operator": store.operator}
        assert store.saveReport(report, context, None) == "STORED"
        duplicate = deepcopy(report)
        duplicate["reportId"] = recordDigest(["different-report", report])
        duplicate["executionRef"]["taskRef"]["taskId"] = "999"
        with pytest.raises(AdapterError, match="another execution"):
            store.saveReport(duplicate, context, None)
        assert len(store.activeReports()) == 1
    finally:
        journal.close()

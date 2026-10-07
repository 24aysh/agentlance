"""Version-one journals retain economic intents, execution claims and exact bytes."""

import asyncio

import pytest

from modules.adapters.storage.journal import Journal
from modules.agent_client.ports import AdapterError


def testLayer2JournalMigrationKeepsClaims(l2):
    async def run():
        await l2.running()
        l2.journal.update(l2.ref, phase="READY")
        assert l2.journal.claim(l2.ref)
        digest = l2.journal.storeContent(b"migration-provenance")
        oldRows = l2.journal.db.execute("SELECT * FROM operations").fetchall()
        for table in (
            "chain_binding",
            "native_operations",
            "streams",
            "observed_logs",
            "task_versions",
            "deliveries",
            "discovered_agents",
            "notifications",
        ):
            l2.journal.db.execute(f"DROP TABLE {table}")
        l2.journal.db.execute("PRAGMA user_version=1")
        l2.journal.db.commit()
        l2.restart()
        assert l2.journal.db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert l2.journal.db.execute("SELECT * FROM operations").fetchall() == oldRows
        assert l2.journal.readContent(digest) == b"migration-provenance"
        assert l2.journal.get(l2.ref)["phase"] == "INTERRUPTED"
        with pytest.raises(AdapterError, match="not ready"):
            l2.journal.claim(l2.ref)
        assert not l2.calls
        with pytest.raises(AdapterError, match="Fixture journal"):
            l2.journal.bindChain({"chainId": "31337"})

    asyncio.run(run())


def testBoundedContentPreservesExistingBytes(tmp_path):
    journal = Journal(tmp_path / "bounded.sqlite", {"mode": "test"}, maxContentBytes=8)
    try:
        digest = journal.storeContent(b"12345678")
        assert journal.storeContent(b"12345678") == digest
        with pytest.raises(AdapterError, match="capacity"):
            journal.storeContent(b"new")
        assert journal.readContent(digest) == b"12345678"
    finally:
        journal.close()

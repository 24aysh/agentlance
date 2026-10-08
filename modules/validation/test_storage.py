import asyncio
import subprocess

import pytest

from modules.adapters.storage.ipfs import KuboPublisher
from modules.adapters.storage.journal import Journal
from modules.adapters.storage.validation import ValidationStore
from modules.agent_client.ports import AdapterError
from tests.layer7_support import localKubo


def testKuboImmutablePublicationRestartAndReadback(tmp_path):
    async def run(kubo):
        journal = Journal(tmp_path / "evidence.sqlite", {"role": "validator"})
        uploader = KuboPublisher(kubo["rpc"], ValidationStore(journal))
        try:
            raw = b'{"public":"evidence"}'
            ref = await uploader.publishEvidence(raw, "one")
            subprocess.run(
                ["docker", "restart", kubo["container"]], check=True, capture_output=True
            )
            for _ in range(100):
                try:
                    assert await uploader.publishEvidence(raw, "one") == ref
                    break
                except AdapterError as error:
                    assert error.kind == "UNAVAILABLE"
                    await asyncio.sleep(0.1)
            else:
                raise AssertionError("Kubo restart did not recover")
            assert await uploader.publishEvidence(raw, "two") == ref
            with pytest.raises(AdapterError, match="payload changed"):
                await uploader.publishEvidence(b"changed", "one")
        finally:
            await uploader.close()
            journal.close()

    with localKubo() as kubo:
        asyncio.run(run(kubo))


def testValidationMigrationClosedRecordsAndRetention(tmp_path):
    path = tmp_path / "journal.sqlite"
    journal = Journal(path, {"role": "validator"}, maxContentBytes=1048576)
    raw = b"preserve"
    digest = journal.storeContent(raw)
    journal.db.execute("DROP TABLE validation_records")
    journal.db.execute("PRAGMA user_version=4")
    journal.db.commit()
    journal.close()
    journal = Journal(path, {"role": "validator"}, maxContentBytes=1048576)
    try:
        store = ValidationStore(journal, maxRows=1)
        assert journal.db.execute("PRAGMA user_version").fetchone()[0] == 5
        assert journal.readContent(digest) == raw
        store.save("cursor", "one", {"version": 1, "position": ["0", "0"]})
        with pytest.raises(AdapterError, match="storage full"):
            store.save("cursor", "two", {"version": 1, "position": ["0", "0"]})
        with pytest.raises(AdapterError, match="record shape"):
            store.save("cursor", "one", {"version": 1, "position": [], "unknown": 1})
        with pytest.raises(AdapterError, match="retention capacity"):
            store.reserveContent()
        assert journal.readContent(digest) == raw
    finally:
        journal.close()

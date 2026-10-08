"""One durable execution claim per economic key; no economic authority."""

import fcntl
import sqlite3
from copy import deepcopy
from uuid import uuid4

from modules.adapters.a2a.profile import ProfileError, jsonBytes, strictJson
from modules.agent_client.ports import AdapterError, ensure
from modules.agent_client.signing import contentDigest

STATES = {
    "WAIT": "INPUT_REQUIRED",
    "ACCEPT_PENDING": "SUBMITTED",
    "READY": "SUBMITTED",
    "STARTED": "WORKING",
    "ARTIFACT_READY": "COMPLETED",
    "RESULT_PENDING": "COMPLETED",
    "RESULT_RECORDED": "COMPLETED",
    "STOPPED": "REJECTED",
    "INTERRUPTED": "FAILED",
}


def executionKey(ref):
    task = ref["taskRef"]
    return jsonBytes([task["chainId"], task["market"], task["taskId"], ref["awardId"]]).decode()


class Journal:
    def __init__(self, path, settings, *, maxContentBytes=64 * 1024 * 1024):
        self.settings = deepcopy(settings)
        ensure(type(maxContentBytes) is int and maxContentBytes > 0, "Content storage limit")
        self.maxContentBytes = maxContentBytes
        self.lock = open(str(path) + ".lock", "a+b")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.lock.close()
            raise AdapterError("CONFLICT", "Agent journal already in use") from error
        self.db = sqlite3.connect(path)
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA foreign_keys=ON")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            ensure(version in (0, 1, 2, 3, 4, 5), "Unknown journal version")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK(id=1), body BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS executions (
                    key TEXT PRIMARY KEY, taskId TEXT UNIQUE NOT NULL,
                    contextId TEXT UNIQUE NOT NULL,
                    claimed INTEGER NOT NULL DEFAULT 0, body BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, key TEXT NOT NULL REFERENCES executions(key),
                    binding BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, scope TEXT UNIQUE NOT NULL, body BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS content (digest TEXT PRIMARY KEY, raw BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS finalized (height TEXT PRIMARY KEY, hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS flags (name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chain_binding (
                    id INTEGER PRIMARY KEY CHECK(id=1), body BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS native_operations (
                    id TEXT PRIMARY KEY, body BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS streams (
                    name TEXT PRIMARY KEY, body BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS observed_logs (
                    stream TEXT NOT NULL, hash TEXT NOT NULL, position TEXT NOT NULL,
                    body BLOB NOT NULL, PRIMARY KEY(stream,hash,position));
                CREATE TABLE IF NOT EXISTS task_versions (
                    key TEXT NOT NULL, height TEXT NOT NULL, body BLOB NOT NULL,
                    PRIMARY KEY(key,height));
                CREATE TABLE IF NOT EXISTS deliveries (
                    key TEXT PRIMARY KEY, body BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS discovered_agents (
                    key TEXT PRIMARY KEY, body BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS notifications (
                    key TEXT PRIMARY KEY, body BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS economic_candidates (
                    key TEXT PRIMARY KEY, body BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS economic_estimates (
                    key TEXT PRIMARY KEY, body BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS forecast_attempts (
                    key TEXT PRIMARY KEY, body BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS usage_reports (
                    key TEXT PRIMARY KEY, body BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS usage_active (
                    key TEXT PRIMARY KEY, body BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS usage_receipts (
                    key TEXT PRIMARY KEY, body BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS economic_meta (
                    key TEXT PRIMARY KEY, body BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS execution_records (
                    kind TEXT NOT NULL, key TEXT NOT NULL, body BLOB NOT NULL,
                    PRIMARY KEY(kind,key));
                CREATE TABLE IF NOT EXISTS validation_records (
                    kind TEXT NOT NULL, key TEXT NOT NULL, body BLOB NOT NULL,
                    PRIMARY KEY(kind,key));
                PRAGMA user_version=5;
            """)
            with self.db:
                old = self.db.execute("SELECT body FROM settings WHERE id=1").fetchone()
                if old:
                    ensure(
                        strictJson(old[0]) == settings,
                        "Journal identity/session mismatch",
                        "CONFLICT",
                    )
                else:
                    self.db.execute("INSERT INTO settings VALUES (1,?)", (jsonBytes(settings),))
            for row in self.rows():
                if (
                    row["phase"] == "STARTED"
                    and not self.db.execute(
                        "SELECT 1 FROM execution_records WHERE kind='run' AND key=?",
                        (executionKey(row["extension"]["executionRef"]),),
                    ).fetchone()
                ):
                    self.update(
                        row["extension"]["executionRef"],
                        phase="INTERRUPTED",
                        detail="Process lost after execution claim; not restarted",
                    )
        except BaseException:
            self.close()
            raise

    def close(self):
        self.db.close()
        self.lock.close()

    def get(self, ref):
        result = self.db.execute(
            "SELECT body FROM executions WHERE key=?", (executionKey(ref),)
        ).fetchone()
        return strictJson(result[0]) if result else None

    def byTaskId(self, taskId):
        result = self.db.execute("SELECT body FROM executions WHERE taskId=?", (taskId,)).fetchone()
        return strictJson(result[0]) if result else None

    def rows(self):
        return [strictJson(row[0]) for row in self.db.execute("SELECT body FROM executions")]

    def checkMessage(self, message, extension):
        found = self.db.execute(
            "SELECT key,binding FROM messages WHERE id=?", (message["messageId"],)
        ).fetchone()
        if found and (
            found[0] != executionKey(extension["executionRef"]) or strictJson(found[1]) != extension
        ):
            raise ProfileError("PROFILE_CONFLICT")
        row = self.get(extension["executionRef"])
        if row and row["extension"] != extension:
            raise ProfileError("PROFILE_CONFLICT")
        if message.get("taskId") and (
            not row or row["correlation"]["a2aTaskId"] != message["taskId"]
        ):
            raise ProfileError("PROFILE_CONFLICT")
        if message.get("contextId") and (
            not row or row["correlation"]["a2aContextId"] != message["contextId"]
        ):
            raise ProfileError("PROFILE_CONFLICT")
        return row

    def remember(self, message, extension):
        key = executionKey(extension["executionRef"])
        with self.db:
            row = self.checkMessage(message, extension)
            if row is None:
                correlation = {
                    "schemaVersion": 1,
                    "executionRef": deepcopy(extension["executionRef"]),
                    "a2aTaskId": str(uuid4()),
                    "a2aContextId": str(uuid4()),
                }
                row = {
                    "correlation": correlation,
                    "extension": deepcopy(extension),
                    "task": None,
                    "bid": None,
                    "stamp": None,
                    "ownWorkReserveAtoms": "0",
                    "phase": "WAIT",
                    "startClaimed": False,
                    "result": None,
                    "artifactId": str(uuid4()),
                    "diagnostic": None,
                    "detail": "Awaiting finalized obligation",
                }
                self.db.execute(
                    "INSERT INTO executions(key,taskId,contextId,body) VALUES (?,?,?,?)",
                    (key, correlation["a2aTaskId"], correlation["a2aContextId"], jsonBytes(row)),
                )
            self.db.execute(
                "INSERT OR IGNORE INTO messages VALUES (?,?,?)",
                (message["messageId"], key, jsonBytes(extension)),
            )
        return row

    def update(self, ref, **changes):
        with self.db:
            row = self.get(ref)
            ensure(row is not None, "Missing correlation")
            if row["phase"] in {"RESULT_RECORDED", "STOPPED", "INTERRUPTED"}:
                ensure(
                    "phase" not in changes or changes["phase"] == row["phase"],
                    "Terminal run immutable",
                )
            row.update(deepcopy(changes))
            self.db.execute(
                "UPDATE executions SET body=? WHERE key=?", (jsonBytes(row), executionKey(ref))
            )
        return row

    def claim(self, ref):
        with self.db:
            row = self.get(ref)
            ensure(row is not None and row["phase"] == "READY", "Run not ready")
            row.update(phase="STARTED", startClaimed=True, detail="Executing")
            changed = self.db.execute(
                "UPDATE executions SET claimed=1,body=? WHERE key=? AND claimed=0",
                (jsonBytes(row), executionKey(ref)),
            ).rowcount
        return changed == 1

    def storeContent(self, raw):
        digest = contentDigest(raw)
        with self.db:
            self.insertContent(digest, raw)
        return digest

    def insertContent(self, digest, raw):
        existing = self.db.execute("SELECT raw FROM content WHERE digest=?", (digest,)).fetchone()
        ensure(existing is None or existing[0] == raw, "Content collision", "CONFLICT")
        if existing is None:
            used = self.db.execute("SELECT COALESCE(sum(length(raw)),0) FROM content").fetchone()[0]
            ensure(
                used + len(raw) <= self.maxContentBytes, "Content storage capacity", "UNAVAILABLE"
            )
            self.db.execute("INSERT INTO content VALUES (?,?)", (digest, raw))

    def readContent(self, digest):
        value = self.db.execute("SELECT raw FROM content WHERE digest=?", (digest,)).fetchone()
        ensure(value is not None, "Content missing", "NOT_FOUND")
        ensure(contentDigest(value[0]) == digest, "Stored content corrupt")
        return value[0]

    def storeResult(self, ref, artifact, raw):
        ensure(contentDigest(raw) == artifact["digest"], "Result hash")
        with self.db:
            row = self.get(ref)
            ensure(row is not None and row["startClaimed"], "No execution claim")
            if row["result"] is not None:
                if row["result"] != artifact:
                    raise ProfileError("PROFILE_CONFLICT")
                ensure(self.readContent(artifact["digest"]) == raw, "Result bytes conflict")
                return
            ensure(row["phase"] == "STARTED", "Result outside execution")
            # Same transaction binds available bytes to the immutable final artifact.
            self.insertContent(artifact["digest"], raw)
            ensure(self.readContent(artifact["digest"]) == raw, "Content collision")
            row.update(
                result=deepcopy(artifact), phase="ARTIFACT_READY", detail="Artifact available"
            )
            self.db.execute(
                "UPDATE executions SET body=? WHERE key=?", (jsonBytes(row), executionKey(ref))
            )

    def intent(self, scope, command, valueAtoms, signer):
        with self.db:
            found = self.db.execute(
                "SELECT body FROM operations WHERE scope=?", (scope,)
            ).fetchone()
            if found:
                intent = strictJson(found[0])
                ensure(
                    (intent["command"], intent["valueAtoms"], intent["signer"])
                    == (command, valueAtoms, signer),
                    "Changed command intent",
                    "CONFLICT",
                )
                return intent
            intent = {
                "operationId": str(uuid4()),
                "command": deepcopy(command),
                "valueAtoms": valueAtoms,
                "signer": signer,
                "attempted": False,
                "result": None,
            }
            self.db.execute(
                "INSERT INTO operations VALUES (?,?,?)",
                (intent["operationId"], scope, jsonBytes(intent)),
            )
        return intent

    def saveIntent(self, intent):
        with self.db:
            self.db.execute(
                "UPDATE operations SET body=? WHERE id=?",
                (jsonBytes(intent), intent["operationId"]),
            )

    def findIntent(self, scope):
        row = self.db.execute("SELECT body FROM operations WHERE scope=?", (scope,)).fetchone()
        return strictJson(row[0]) if row else None

    def bindChain(self, binding):
        settings = strictJson(self.db.execute("SELECT body FROM settings WHERE id=1").fetchone()[0])
        ensure(
            "sessionId" not in settings, "Fixture journal cannot become a chain journal", "CONFLICT"
        )
        with self.db:
            old = self.db.execute("SELECT body FROM chain_binding WHERE id=1").fetchone()
            ensure(
                old is None or strictJson(old[0]) == binding,
                "Chain journal identity changed",
                "CONFLICT",
            )
            self.db.execute(
                "INSERT OR IGNORE INTO chain_binding VALUES (1,?)", (jsonBytes(binding),)
            )

    def bindSender(self, signer):
        with self.db:
            old = self.db.execute("SELECT value FROM flags WHERE name='sender'").fetchone()
            ensure(old is None or old[0] == signer, "Journal sender changed", "CONFLICT")
            self.db.execute("INSERT OR IGNORE INTO flags VALUES ('sender',?)", (signer,))

    def nativeOperation(self, operationId):
        row = self.db.execute(
            "SELECT body FROM native_operations WHERE id=?", (operationId,)
        ).fetchone()
        return strictJson(row[0]) if row else None

    def nativeOperations(self):
        return [strictJson(row[0]) for row in self.db.execute("SELECT body FROM native_operations")]

    def saveNative(self, operation):
        with self.db:
            self.db.execute(
                "INSERT INTO native_operations VALUES (?,?) "
                "ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                (operation["operationId"], jsonBytes(operation)),
            )

    def latestFinalized(self):
        return self.db.execute(
            "SELECT height,hash FROM finalized ORDER BY length(height) DESC,height DESC LIMIT 1"
        ).fetchone()

    def observe(self, stamp):
        ensure(not self.halted(), "Finalized history conflict", "FINALITY_CONFLICT")
        if stamp["finality"] != "FINALIZED":
            return
        with self.db:
            row = self.db.execute(
                "SELECT hash FROM finalized WHERE height=?", (stamp["blockNumber"],)
            ).fetchone()
            if row and row[0] != stamp["blockHash"]:
                self.db.execute("INSERT OR REPLACE INTO flags VALUES ('halted','true')")
            else:
                self.db.execute(
                    "INSERT OR IGNORE INTO finalized VALUES (?,?)",
                    (stamp["blockNumber"], stamp["blockHash"]),
                )
        ensure(not self.halted(), "Finalized history conflict", "FINALITY_CONFLICT")

    def halted(self):
        return self.db.execute("SELECT 1 FROM flags WHERE name='halted'").fetchone() is not None

    def halt(self, detail):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO flags VALUES ('halted','true')")
        raise AdapterError("FINALITY_CONFLICT", detail)

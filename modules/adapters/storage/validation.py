"""Bounded private L7 records; transactions also cover consumer checkpoints."""

from modules.adapters.a2a.profile import jsonBytes, strictJson
from modules.agent_client.ports import ensure

FIELDS = {
    "job": (
        "version view event profile stage resume attempts nextAttempt diagnostic "
        "evidenceDigest evidence record typed operationId expiryOperationId cpuReserved retryGrants"
    ),
    "receipt": "version task result receipt event stamp",
    "export": (
        "version taskRef state operationId attempts nextAttempt diagnostic publication retryGrants"
    ),
    "publication": "version digest contentRef",
    "cursor": "version position",
    "reconciliation": "version executionRef inputs observations missing",
    "selection": "version key",
    "binding": "version configuration",
}


class ValidationStore:
    def __init__(self, journal, *, maxRows=10000, maxBytes=32 * 1024 * 1024):
        ensure(
            type(maxRows) is int
            and 1 <= maxRows <= 100000
            and type(maxBytes) is int
            and 1024 <= maxBytes <= 256 * 1024 * 1024,
            "Validation storage limits",
        )
        self.journal, self.db, self.maxRows, self.maxBytes = journal, journal.db, maxRows, maxBytes

    def get(self, kind, key):
        row = self.db.execute(
            "SELECT body FROM validation_records WHERE kind=? AND key=?", (kind, key)
        ).fetchone()
        return strictJson(row[0]) if row else None

    def rows(self, kind):
        return [
            (key, strictJson(body))
            for key, body in self.db.execute(
                "SELECT key,body FROM validation_records WHERE kind=? ORDER BY key", (kind,)
            )
        ]

    def put(self, kind, key, body, *, immutable=False):
        ensure(
            kind in FIELDS
            and set(body) == set(FIELDS[kind].split())
            and type(body["version"]) is int
            and body["version"] == 1,
            "Validation record shape",
        )
        old = self.get(kind, key)
        ensure(not immutable or old is None or old == body, "Validation record changed", "CONFLICT")
        raw = jsonBytes(body)
        count, size = self.db.execute(
            "SELECT count(*),coalesce(sum(length(body)),0) FROM validation_records"
        ).fetchone()
        ensure(
            count + (old is None) <= self.maxRows
            and size - (len(jsonBytes(old)) if old else 0) + len(raw) <= self.maxBytes,
            "Validation storage full",
            "UNAVAILABLE",
        )
        self.db.execute(
            "INSERT INTO validation_records VALUES (?,?,?) "
            "ON CONFLICT(kind,key) DO UPDATE SET body=excluded.body",
            (kind, key, raw),
        )
        return body

    def save(self, kind, key, body, **options):
        with self.db:
            return self.put(kind, key, body, **options)

    def reserveContent(self):
        # Each admitted job can retain 1 MiB input/evidence + 2 MiB result + schema/policy.
        used = self.db.execute("SELECT coalesce(sum(length(raw)),0) FROM content").fetchone()[0]
        unfetched = sum(
            row["evidenceDigest"] is None
            and row["stage"] not in {"SETTLED", "EXPIRED", "EXHAUSTED"}
            for _, row in self.rows("job")
        )
        ensure(
            used + (unfetched + 1) * (4 * 1048576 + 131072) <= self.journal.maxContentBytes,
            "Validation retention capacity",
            "UNAVAILABLE",
        )

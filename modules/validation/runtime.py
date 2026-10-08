"""Bounded recoverable validator jobs. Only finalized market receipts establish outcomes."""

import asyncio
import base64
import logging
import time
from copy import deepcopy
from secrets import randbits

from eth_account.messages import encode_typed_data

from modules.adapters.a2a.profile import strictJson
from modules.agent_client.ports import AdapterError, ensure, recordCheck
from modules.agent_client.signing import buildValidationVerdictTypedData, contentDigest, verifyEoa
from modules.economics.records import recordDigest
from modules.validation.evaluator import evidenceBinding, evidenceBytes
from modules.validation.observer import OutcomeObserver

LOG = logging.getLogger(__name__)


def evaluatorProfile(image):
    ensure(
        isinstance(image, str) and image.startswith("sha256:") and len(image) == 71,
        "Pinned evaluator image ID",
    )
    return {
        "executor": {"image": image},
        "limits": {
            "requestBytes": 8 * 1048576,
            "cpuMillis": 1000,
            "cpuSeconds": 5,
            "memoryBytes": 128 * 1048576,
            "pids": 16,
            "workspaceBytes": 1048576,
            "requestSeconds": 10,
            "outputBytes": 65536,
            "logBytes": 65536,
        },
    }


class ValidatorRuntime:
    def __init__(
        self,
        market,
        watcher,
        store,
        content,
        uploader,
        docker,
        account,
        types,
        image,
        *,
        publisher=None,
        clock=time.time,
        maxAttempts=3,
        concurrency=1,
    ):
        ensure(
            type(maxAttempts) is int
            and 1 <= maxAttempts <= 3
            and type(concurrency) is int
            and 1 <= concurrency <= 4,
            "Validator limits",
        )
        ensure(account.address.lower() == market.chain.facts["validator"], "Validator key binding")
        ensure(
            store.journal.settings.get("role") == "validator", "Separate validator journal required"
        )
        self.market, self.watcher, self.store, self.content = market, watcher, store, content
        self.uploader, self.docker, self.account, self.types = uploader, docker, account, types
        self.profile, self.publisher, self.clock = evaluatorProfile(image), publisher, clock
        self.maxAttempts, self.concurrency, self.pending = maxAttempts, concurrency, {}
        self.domain = {
            "name": "AgentLance",
            "version": "1",
            "chainId": int(market.chain.facts["chainId"]),
            "verifyingContract": market.chain.facts["market"],
        }
        store.save(
            "binding",
            "validator",
            {
                "version": 1,
                "configuration": {
                    "profile": self.profile,
                    "maxAttempts": maxAttempts,
                    "maxGas": market.native.maxGas,
                    "maxGasPriceWei": str(market.native.maxGasPriceWei),
                    "publisher": publisher.address if publisher else None,
                },
            },
            immutable=True,
        )
        self.observer = OutcomeObserver(
            market,
            watcher,
            store,
            admit=self.admit,
            publisher=publisher.address if publisher else None,
        )

    def admit(self, view, event):
        task, result = view["task"], view["result"]
        ensure(
            view["status"] == "SUBMITTED" and result is not None and view["winningBid"] is not None,
            "Submission state",
        )
        ensure(
            result == event["event"]["payload"]["result"]
            and task["terms"]["validator"] == self.account.address.lower(),
            "Submission authority",
        )
        key = recordDigest(evidenceBinding(task, result))
        if self.store.get("job", key):
            return
        self.store.reserveContent()
        self.store.put(
            "job",
            key,
            {
                "version": 1,
                "view": view,
                "event": event,
                "profile": self.profile,
                "stage": "DISCOVERED",
                "resume": None,
                "attempts": {},
                "nextAttempt": 0,
                "diagnostic": None,
                "evidenceDigest": None,
                "evidence": None,
                "record": None,
                "typed": None,
                "operationId": recordDigest(["verdict-v1", key]),
                "expiryOperationId": recordDigest(["validation-expiry-v1", key]),
                "cpuReserved": 0,
                "retryGrants": 0,
            },
        )
        LOG.info("event=validation_admitted task_id=%s job_id=%s", task["taskRef"]["taskId"], key)

    def saveJob(self, key, job, **changes):
        saved = self.store.save("job", key, job | changes)
        if saved["stage"] != job["stage"]:
            LOG.info(
                "event=validation_stage task_id=%s job_id=%s previous=%s stage=%s",
                job["view"]["task"]["taskRef"]["taskId"],
                key,
                job["stage"],
                saved["stage"],
            )
        return saved

    def attempt(self, key, job, name):
        attempts = job["attempts"].copy()
        ensure(
            attempts.get(name, 0) < self.maxAttempts + job["retryGrants"],
            name + " attempts exhausted",
            "EXHAUSTED",
        )
        attempts[name] = attempts.get(name, 0) + 1
        return self.saveJob(key, job, attempts=attempts)

    async def advance(self, key):
        job = self.store.get("job", key)
        ref, old = job["view"]["task"]["taskRef"], job["view"]
        try:
            view = await self.market.readTask(ref)
            ensure(
                view["task"] == old["task"]
                and view["result"] == old["result"]
                and view["winningBid"] == old["winningBid"],
                "Validation frozen binding",
                "CONFLICT",
            )
            if view["receipt"] is not None:
                self.saveJob(key, job, stage="SETTLED", diagnostic=None)
                return
            deadline = int(view["task"]["terms"]["validationBy"])
            if max(int(self.clock()), int(view["stamp"]["blockTimestamp"])) >= deadline:
                job = self.saveJob(key, job, stage="EXPIRED")
                if int(view["stamp"]["blockTimestamp"]) >= deadline:
                    job = self.attempt(key, job, "expiry")
                    await self.market.expireTask(
                        {"schemaVersion": 1, "command": "expireTask", "input": {"taskRef": ref}},
                        job["expiryOperationId"],
                    )
                return
            if job["stage"] == "EXHAUSTED":
                return
            stage = job["resume"] if job["stage"] == "WAITING" else job["stage"]
            if stage == "EVALUATING":
                await self.docker.stop(ref, key)
                stage = "FETCHED"
            job = self.saveJob(key, job, stage=stage, resume=None, diagnostic=None)
            if stage == "DISCOVERED":
                job = self.attempt(key, job, "fetch")
                terms = view["task"]["terms"]
                refs = [
                    terms["input"],
                    terms["outputSchema"],
                    terms["validationPolicy"],
                    view["result"]["artifact"],
                ]
                for contentRef, limit in zip(refs, (1048576, 65536, 65536, 2097152), strict=True):
                    raw = await self.content.fetchBytes(
                        contentRef["uri"], limit, contentRef["digest"]
                    )
                    ensure(contentDigest(raw) == contentRef["digest"], "Fetched digest mismatch")
                    self.store.journal.storeContent(raw)
                self.saveJob(key, job, stage="FETCHED")
            elif stage == "FETCHED":
                await self.docker.stop(ref, key)
                job = self.attempt(key, job, "evaluate")
                ensure(
                    job["cpuReserved"] + 5 <= 5 * (self.maxAttempts + job["retryGrants"]),
                    "Total evaluator CPU budget",
                    "EXHAUSTED",
                )
                job = self.saveJob(key, job, stage="EVALUATING", cpuReserved=job["cpuReserved"] + 5)
                terms = view["task"]["terms"]
                request = {"task": view["task"], "result": view["result"]}
                for field, item in zip(
                    ("input", "shape", "policy", "artifact"),
                    (
                        terms["input"],
                        terms["outputSchema"],
                        terms["validationPolicy"],
                        view["result"]["artifact"],
                    ),
                    strict=True,
                ):
                    request[field] = base64.b64encode(
                        self.store.journal.readContent(item["digest"])
                    ).decode()
                profile = deepcopy(job["profile"])
                profile["limits"]["requestSeconds"] = min(
                    10,
                    max(1, deadline - max(int(self.clock()), int(view["stamp"]["blockTimestamp"]))),
                )
                try:
                    raw = await self.docker.run(ref, key, request, profile)
                finally:
                    await self.docker.stop(ref, key)
                evidence = strictJson(raw)
                recordCheck(evidence, "EvaluationEvidence", self.market.schema)
                ensure(
                    all(
                        evidence[k] == v
                        for k, v in evidenceBinding(view["task"], view["result"]).items()
                    )
                    and (evidence["verdict"] == "PASS") == (evidence["reason"] == "PASS")
                    and (evidence["failedPredicate"] is not None)
                    == (evidence["reason"] == "PREDICATE_FAILED")
                    and evidenceBytes(evidence) == raw,
                    "Evaluator output binding",
                    "CONFLICT",
                )
                with self.store.db:
                    digest = contentDigest(raw)
                    self.store.journal.insertContent(digest, raw)
                    self.store.put(
                        "job", key, job | {"stage": "EVIDENCE_READY", "evidenceDigest": digest}
                    )
            elif stage == "EVIDENCE_READY":
                job = self.attempt(key, job, "upload")
                raw = self.store.journal.readContent(job["evidenceDigest"])
                evidence = await self.uploader.publishEvidence(raw, key)
                # Read through the configured artifact transport as well as Kubo's admin readback.
                retrieved = await self.content.fetchBytes(
                    evidence["uri"], 1048576, evidence["digest"]
                )
                ensure(
                    retrieved == raw and evidence["digest"] == job["evidenceDigest"],
                    "Published evidence retrieval mismatch",
                    "CONFLICT",
                )
                self.saveJob(key, job, stage="EVIDENCE_PUBLISHED", evidence=evidence)
            elif stage == "EVIDENCE_PUBLISHED":
                if job["record"] is None:
                    evidence = strictJson(self.store.journal.readContent(job["evidenceDigest"]))
                    record = {
                        "schemaVersion": 1,
                        "executionRef": view["result"]["executionRef"],
                        "agentRef": view["winningBid"]["offer"]["agentRef"],
                        "resultDigest": view["result"]["artifact"]["digest"],
                        "validationPolicyDigest": view["task"]["terms"]["validationPolicy"][
                            "digest"
                        ],
                        "verdict": evidence["verdict"],
                        "evidence": job["evidence"],
                        "validator": self.account.address.lower(),
                        "nonce": str(randbits(256)),
                        "expiry": str(deadline),
                        "signature": "0x",
                    }
                    typed = buildValidationVerdictTypedData(record, self.domain, self.types)
                    self.saveJob(key, job, record=record, typed=typed)
                else:
                    job = self.attempt(key, job, "sign")
                    ensure(not self.store.journal.halted(), "Finality halt", "FINALITY_CONFLICT")
                    ensure(
                        job["typed"]
                        == buildValidationVerdictTypedData(job["record"], self.domain, self.types),
                        "Saved typed binding",
                        "CONFLICT",
                    )
                    signature = (
                        "0x"
                        + self.account.sign_message(
                            encode_typed_data(full_message=job["typed"])
                        ).signature.hex()
                    )
                    ensure(
                        verifyEoa(job["typed"], signature, self.account.address.lower()),
                        "Signature verification",
                    )
                    record = job["record"] | {"signature": signature}
                    recordCheck(record, "ValidationRecord", self.market.schema)
                    self.saveJob(key, job, stage="SIGNED", record=record)
            elif stage in {"SIGNED", "RELAY_PENDING"}:
                job = self.attempt(key, job, "relay")
                job = self.saveJob(key, job, stage="RELAY_PENDING")
                await self.market.settleVerdict(
                    {
                        "schemaVersion": 1,
                        "command": "settleVerdict",
                        "input": {"record": job["record"]},
                    },
                    job["operationId"],
                )
        except (AdapterError, TimeoutError) as error:
            LOG.warning(
                "event=validation_attempt_failed task_id=%s job_id=%s error_kind=%s",
                ref["taskId"],
                key,
                error.kind if isinstance(error, AdapterError) else "TIMEOUT",
            )
            if isinstance(error, AdapterError) and error.kind == "FINALITY_CONFLICT":
                raise
            current = self.store.get("job", key)
            exhausted = isinstance(error, AdapterError) and error.kind in {
                "EXHAUSTED",
                "CONFLICT",
                "INVALID_DATA",
            }
            self.saveJob(
                key,
                current,
                stage="EXHAUSTED" if exhausted else "WAITING",
                resume=current["resume"] or current["stage"],
                diagnostic=str(error)[:400],
            )
        finally:
            current = self.store.get("job", key)
            self.saveJob(key, current, nextAttempt=int(self.clock()) + 2)

    async def export(self, key, job):
        try:
            publication = await self.publisher.readPublication(job["taskRef"])
            if publication["published"]:
                self.store.save(
                    "export",
                    key,
                    job | {"state": "PUBLISHED", "publication": publication, "diagnostic": None},
                )
                LOG.info(
                    "event=feedback_published task_id=%s operation_id=%s feedback_index=%s",
                    job["taskRef"]["taskId"],
                    job["operationId"],
                    publication["feedbackIndex"],
                )
                return
            if job["attempts"] >= self.maxAttempts + job["retryGrants"]:
                return
            job = self.store.save(
                "export",
                key,
                job | {"attempts": job["attempts"] + 1, "nextAttempt": int(self.clock()) + 2},
            )
            prior = self.store.journal.nativeOperation(job["operationId"])
            if (
                prior
                and prior["result"]
                and prior["receipt"]
                and int(prior["receipt"]["status"], 16) == 0
            ):
                await self.market.chain.checkCanonical(prior["result"]["stamp"])
                job = self.store.save(
                    "export",
                    key,
                    job | {"operationId": recordDigest(["feedback-retry-v1", job["operationId"]])},
                )
            await self.publisher.publish(job["taskRef"], job["operationId"])
        except AdapterError as error:
            LOG.warning(
                "event=feedback_attempt_failed task_id=%s operation_id=%s attempt=%s error_kind=%s",
                job["taskRef"]["taskId"],
                job["operationId"],
                job["attempts"],
                error.kind,
            )
            if error.kind == "FINALITY_CONFLICT":
                raise
            self.store.save(
                "export",
                key,
                job | {"diagnostic": str(error)[:400], "nextAttempt": int(self.clock()) + 2},
            )

    async def tick(self):
        if self.store.journal.halted():
            await self.close()
            raise AdapterError("FINALITY_CONFLICT", "Validator halted")
        for key, future in list(self.pending.items()):
            if future.done():
                del self.pending[key]
                future.result()
        try:
            await self.watcher.scanMarket()
            await self.observer.consume()
        except AdapterError as error:
            if error.kind == "FINALITY_CONFLICT":
                await self.close()
            raise
        now = int(self.clock())
        work = [
            (key, "job", job) for key, job in self.store.rows("job") if job["stage"] != "SETTLED"
        ]
        if self.publisher:
            work += [
                ("export:" + key, "export", job)
                for key, job in self.store.rows("export")
                if job["state"] == "PENDING"
            ]
        cursor = self.store.get("cursor", "scheduler")
        start = int(cursor["position"][0]) % len(work) if cursor and work else 0
        for offset in range(len(work)):
            index = (start + offset) % len(work)
            key, kind, job = work[index]
            if len(self.pending) >= self.concurrency:
                break
            if key in self.pending or job["nextAttempt"] > now:
                continue
            action = self.advance(key) if kind == "job" else self.export(key[7:], job)
            self.pending[key] = asyncio.create_task(action)
            self.store.save(
                "cursor", "scheduler", {"version": 1, "position": [str(index + 1), "0"]}
            )

    async def grantRetry(self, taskRef, kind):
        """Explicit operator action: add three attempts; retain every signed intent."""
        ensure(kind in {"job", "export"}, "Retry scope")
        await self.market.chain.qualify()
        if kind == "job":
            matches = [
                (k, row)
                for k, row in self.store.rows(kind)
                if row["view"]["task"]["taskRef"] == taskRef
            ]
        else:
            matches = [(k, row) for k, row in self.store.rows(kind) if row["taskRef"] == taskRef]
        ensure(len(matches) == 1, "Retry record not found", "NOT_FOUND")
        key, row = matches[0]
        ensure(row["retryGrants"] < 999, "Manual retry ceiling", "LIMIT")
        changes = {"retryGrants": row["retryGrants"] + 3, "nextAttempt": 0, "diagnostic": None}
        if kind == "job":
            view = await self.market.readTask(taskRef)
            ensure(
                view["status"] == "SUBMITTED"
                and self.clock() < int(view["task"]["terms"]["validationBy"]),
                "Validation retry deadline",
                "CONFLICT",
            )
            ensure(row["stage"] in {"WAITING", "EXHAUSTED"}, "Validation retry state", "CONFLICT")
            self.store.reserveContent()
            changes["stage"] = row["resume"] or "DISCOVERED"
            changes["resume"] = None
        else:
            ensure(row["state"] == "PENDING", "Export retry state", "CONFLICT")
        return self.store.save(kind, key, row | changes)

    async def close(self):
        for future in self.pending.values():
            future.cancel()
        await asyncio.gather(*self.pending.values(), return_exceptions=True)
        self.pending.clear()
        for key, job in self.store.rows("job"):
            if job["stage"] == "EVALUATING":
                await self.docker.stop(job["view"]["task"]["taskRef"], key)

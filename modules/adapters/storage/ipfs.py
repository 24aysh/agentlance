"""Explicit trusted Kubo administration; task locators never select this endpoint."""

import asyncio
import re
from urllib.parse import urlsplit

import httpx

from modules.agent_client.ports import AdapterError, ensure
from modules.agent_client.signing import contentDigest

ADD_OPTIONS = {
    "cid-version": "1",
    "raw-leaves": "true",
    "hash": "sha2-256",
    "chunker": "size-262144",
    "wrap-with-directory": "false",
    "pin": "true",
    "progress": "false",
}


class KuboPublisher:
    def __init__(self, rpcUrl, store, *, headers=None, http=None):
        parts = urlsplit(rpcUrl)
        ensure(
            parts.scheme == "https"
            or (parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost", "::1"}),
            "Kubo requires HTTPS or explicit loopback RPC",
        )
        ensure(
            parts.hostname
            and not parts.username
            and not parts.password
            and not parts.query
            and not parts.fragment
            and parts.path in {"", "/"},
            "Kubo RPC origin",
        )
        self.url, self.store = rpcUrl.rstrip("/") + "/api/v0/", store
        self.http = http or httpx.AsyncClient(
            timeout=10, trust_env=False, follow_redirects=False, headers=headers
        )

    async def request(self, name, *, maximum=1048576, **kwargs):
        try:
            async with (
                asyncio.timeout(10),
                self.http.stream("POST", self.url + name, **kwargs) as response,
            ):
                ensure(response.status_code == 200, "Kubo RPC failed", "UNAVAILABLE")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    ensure(len(raw) + len(chunk) <= maximum, "Kubo response limit", "UNAVAILABLE")
                    raw.extend(chunk)
                return bytes(raw)
        except (httpx.HTTPError, TimeoutError) as error:
            raise AdapterError("UNAVAILABLE", "Kubo transport unavailable") from error

    async def publishEvidence(self, raw, publicationId):
        from modules.adapters.a2a.profile import strictJson

        ensure(type(raw) is bytes and len(raw) <= 1048576, "Evidence byte limit")
        digest = contentDigest(raw)
        old = self.store.get("publication", publicationId)
        ensure(old is None or old["digest"] == digest, "Publication payload changed", "CONFLICT")
        if old is None:
            with self.store.db:
                self.store.journal.insertContent(digest, raw)
                old = self.store.put(
                    "publication",
                    publicationId,
                    {"version": 1, "digest": digest, "contentRef": None},
                )
        ref = old["contentRef"]
        if ref is None:
            response = strictJson(
                await self.request(
                    "add",
                    maximum=65536,
                    params=ADD_OPTIONS,
                    files={"file": ("evidence.json", raw, "application/json")},
                )
            )
            cid = response.get("Hash")
            ensure(isinstance(cid, str) and re.fullmatch(r"b[a-z2-7]{20,120}", cid), "Kubo CID")
            ref = {"uri": "ipfs://" + cid, "digest": digest}
            self.store.save("publication", publicationId, old | {"contentRef": ref})
        retrieved = await self.request("cat", params={"arg": ref["uri"][7:]})
        ensure(
            retrieved == raw and contentDigest(retrieved) == digest,
            "Kubo readback mismatch",
            "CONFLICT",
        )
        return ref

    async def close(self):
        await self.http.aclose()

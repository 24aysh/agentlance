"""Exact-byte content and HTTPS with connection-time address validation."""

import asyncio
import ipaddress
import socket
import ssl
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend

from modules.agent_client.ports import AdapterError, ensure
from modules.agent_client.signing import contentDigest
from modules.domain.records import isContentUri


class NetworkPolicy:
    def __init__(self, fixtureOrigins=()):
        self.fixtureOrigins = frozenset(fixtureOrigins)
        for origin in self.fixtureOrigins:
            parts = urlsplit(origin)
            ensure(
                parts.scheme == "https"
                and parts.hostname in {"localhost", "127.0.0.1", "::1"}
                and parts.path == "",
                "Fixture exception must be a localhost HTTPS origin",
            )

    def checkUri(self, uri):
        ensure(isContentUri(uri) and uri.startswith("https://"), "HTTPS URI required")
        return uri

    def checkAddresses(self, host, port, addresses):
        local = f"https://{host}:{port}" in self.fixtureOrigins
        ensure(bool(addresses), "Empty DNS result", "UNAVAILABLE")
        for address in addresses:
            ip = ipaddress.ip_address(address)
            ensure(ip.is_loopback if local else ip.is_global, "Forbidden network destination")


class SafeBackend(AutoBackend):
    def __init__(self, policy):
        self.policy = policy

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        # Resolve once and connect to a literal address. httpcore retains the original TLS SNI.
        records = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(item[4][0] for item in records))
        self.policy.checkAddresses(host, port, addresses)
        lastError = None
        for address in addresses:
            try:
                return await super().connect_tcp(
                    address, port, timeout, local_address, socket_options
                )
            except (OSError, httpcore.ConnectError) as error:
                lastError = error
        raise httpcore.ConnectError(str(lastError))


class SafeTransport(httpx.AsyncHTTPTransport):
    def __init__(self, policy, caFile=None):
        super().__init__()
        # HTTPX exposes transport injection; httpcore provides the DNS/backend boundary.
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(cafile=caFile),
            network_backend=SafeBackend(policy),
            max_connections=8,
            max_keepalive_connections=4,
        )


def createHttpClient(policy, caFile=None):
    return httpx.AsyncClient(
        transport=SafeTransport(policy, caFile),
        trust_env=False,
        follow_redirects=False,
        timeout=10,
        headers={"Accept-Encoding": "identity"},
    )


class ContentStore:
    def __init__(self, http, networkPolicy, journal=None, artifactOrigin=None, ipfsGateway=None):
        self.http, self.policy, self.journal = http, networkPolicy, journal
        self.artifactOrigin, self.ipfsGateway = artifactOrigin, ipfsGateway

    async def fetchBytes(self, uri, maximumBytes, expectedDigest=None):
        ensure(isContentUri(uri), "Invalid content URI")
        if (
            self.journal is not None
            and self.artifactOrigin is not None
            and expectedDigest is not None
            and uri == self.artifactOrigin + "/artifacts/" + expectedDigest[2:] + ".json"
        ):
            raw = self.journal.readContent(expectedDigest)
            ensure(len(raw) <= maximumBytes, "Content byte limit")
            return raw
        target = uri
        if uri.startswith("ipfs://"):
            ensure(self.ipfsGateway is not None, "No configured IPFS gateway", "UNSUPPORTED")
            target = self.ipfsGateway.rstrip("/") + "/ipfs/" + uri[len("ipfs://") :]
        try:
            async with asyncio.timeout(10):
                for redirect in range(4):
                    self.policy.checkUri(target)
                    async with self.http.stream(
                        "GET", target, headers={"Accept-Encoding": "identity"}
                    ) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            ensure(
                                redirect < 3 and "location" in response.headers, "Redirect limit"
                            )
                            target = urljoin(target, response.headers["location"])
                            continue
                        ensure(response.status_code == 200, "Content unavailable", "UNAVAILABLE")
                        ensure(
                            response.headers.get("content-encoding", "identity") == "identity",
                            "Compressed content forbidden",
                        )
                        data = bytearray()
                        async for chunk in response.aiter_raw():
                            ensure(len(data) + len(chunk) <= maximumBytes, "Content byte limit")
                            data.extend(chunk)
                        raw = bytes(data)
                        ensure(
                            expectedDigest is None or contentDigest(raw) == expectedDigest,
                            "Content digest mismatch",
                        )
                        return raw
        except (TimeoutError, httpx.HTTPError, OSError) as error:
            raise AdapterError("UNAVAILABLE", "Content transport failed") from error

    def publishResult(self, executionRef, raw):
        ensure(self.journal is not None and self.artifactOrigin is not None, "No artifact store")
        ensure(len(raw) <= 1048576, "Result limit")
        digest = contentDigest(raw)
        ref = {"uri": self.artifactOrigin + "/artifacts/" + digest[2:] + ".json", "digest": digest}
        self.journal.storeResult(executionRef, ref, raw)
        return ref

    def publishContent(self, raw):
        ensure(self.journal is not None and self.artifactOrigin is not None, "No content store")
        ensure(isinstance(raw, bytes) and len(raw) <= 1048576, "Content limit")
        digest = self.journal.storeContent(raw)
        return {"uri": self.artifactOrigin + "/artifacts/" + digest[2:] + ".json", "digest": digest}

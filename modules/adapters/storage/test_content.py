"""Bounded byte fetching and connection destination policy."""

import asyncio
import hashlib

import httpx
import pytest

from modules.adapters.storage.content import ContentStore, NetworkPolicy
from modules.agent_client.ports import AdapterError
from modules.agent_client.signing import contentDigest


@pytest.mark.parametrize(
    "case",
    [
        "exact",
        "whitespace",
        "sha3",
        "over-limit",
        "redirect",
        "compressed",
        "timeout",
        "status",
        "ipfs",
        "mutable",
    ],
)
def testFetchBoundary(case):
    async def run():
        raw = b'{"value":7}\n'
        requests = []

        def reply(request):
            requests.append(request)
            if case == "timeout":
                raise httpx.ReadTimeout("timeout")
            if case == "redirect":
                return httpx.Response(302, headers={"location": "/again"})
            if case == "compressed":
                return httpx.Response(
                    200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(raw)
                )
            return httpx.Response(
                404 if case == "status" else 200,
                stream=httpx.ByteStream(raw if len(requests) == 1 else b"changed"),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
            content = ContentStore(http, NetworkPolicy())
            digest = contentDigest(raw)
            if case == "whitespace":
                digest = contentDigest(raw.rstrip())
            if case == "sha3":
                digest = "0x" + hashlib.sha3_256(raw).hexdigest()
            if case == "exact":
                assert (
                    await content.fetchBytes("https://example.com/input", len(raw), digest) == raw
                )
            elif case == "mutable":
                assert await content.fetchBytes("https://example.com/input", 100, digest) == raw
                with pytest.raises(AdapterError):
                    await content.fetchBytes("https://example.com/input", 100, digest)
            else:
                with pytest.raises(AdapterError):
                    await content.fetchBytes(
                        "ipfs://bafytest/input" if case == "ipfs" else "https://example.com/input",
                        len(raw) - 1 if case == "over-limit" else 100,
                        digest,
                    )
            if case == "redirect":
                assert len(requests) == 4

    asyncio.run(run())


@pytest.mark.parametrize(
    "host,port,ips,allowed",
    [
        ("example.com", 443, ["8.8.8.8"], True),
        ("example.com", 443, ["8.8.8.8", "127.0.0.1"], False),
        ("example.com", 443, ["10.0.0.1"], False),
        ("example.com", 443, ["169.254.169.254"], False),
        ("example.com", 443, ["::1"], False),
        ("localhost", 8741, ["127.0.0.1"], True),
        ("localhost", 8742, ["127.0.0.1"], False),
        ("localhost", 8741, ["8.8.8.8"], False),
        ("example.com", 443, [], False),
    ],
)
def testDestinationPolicy(host, port, ips, allowed):
    policy = NetworkPolicy(["https://localhost:8741"])
    if allowed:
        policy.checkAddresses(host, port, ips)
    else:
        with pytest.raises(AdapterError):
            policy.checkAddresses(host, port, ips)


@pytest.mark.parametrize(
    "uri",
    [
        "http://example.com",
        "file:///etc/passwd",
        "https://user:secret@example.com",
        "https://localhost:8741/#fragment",
    ],
)
def testBadUri(uri):
    with pytest.raises(AdapterError):
        NetworkPolicy().checkUri(uri)


def testConnectionRebindingRejected(monkeypatch):
    from modules.adapters.storage.content import SafeBackend

    async def run():
        async def dns(*args, **kwargs):
            return [(2, 1, 6, "", ("127.0.0.1", 443))]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", dns)
        with pytest.raises(AdapterError, match="Forbidden network"):
            await SafeBackend(NetworkPolicy()).connect_tcp("public.example", 443)

    asyncio.run(run())


def testTlsFailureAndIncompleteBody():
    async def run():
        for error in (
            httpx.ConnectError("certificate verify failed"),
            httpx.RemoteProtocolError("incomplete chunk"),
        ):

            def fail(request, error=error):
                raise error

            async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as http:
                with pytest.raises(AdapterError, match="UNAVAILABLE"):
                    await ContentStore(http, NetworkPolicy()).fetchBytes("https://example.com", 100)

    asyncio.run(run())

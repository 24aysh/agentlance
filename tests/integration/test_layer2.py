"""Two OS processes, real TLS sockets and SDK traffic; bounded supervisor cleanup."""

import asyncio
import socket

import pytest

from conftest import SCHEMA
from modules.domain.records import validateRecord
from scripts.demo_layer2 import runDemo


@pytest.mark.l2socket
def testTwoProcessDemo(tmp_path):
    # Reserve distinct available ports before setup; the subprocess readiness gate catches races.
    with socket.socket() as first, socket.socket() as second:
        first.bind(("127.0.0.1", 0))
        second.bind(("127.0.0.1", 0))
        ports = (first.getsockname()[1], second.getsockname()[1])
    report = asyncio.run(runDemo(tmp_path, *ports))
    assert report["passed"] and report["invocations"] == 1
    assert len(set(report["servicePids"])) == 3
    validateRecord(report["correlation"], "A2ACorrelation", SCHEMA)
    validateRecord(report["receipt"], "SettlementReceipt", SCHEMA)
    assert (
        report["artifactDigest"]
        == "0xd587891f0356b0fec2d227918ec777c9626ce70567e90299ef32e0ce37045c2b"
    )

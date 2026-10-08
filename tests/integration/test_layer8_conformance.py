"""Run the unchanged Layer 2 black-box suite against the independent Node server."""

import os
import signal
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from modules.adapters.a2a.profile import jsonBytes, strictJson
from tests.layer3_support import localNode
from tests.layer8_support import PublicTls, freePort

pytestmark = pytest.mark.l3socket


def testIndependentExternalProfileConformance(tmp_path):
    public = PublicTls(tmp_path)
    control = tmp_path / "control.json"
    try:
        with localNode(tmp_path) as rpc:
            origin = f"https://127.0.0.1:{freePort()}"
            control.write_bytes(
                jsonBytes(
                    {
                        "rpc": str(rpc.client.base_url),
                        "certificate": str(public.certificate),
                        "tlsKey": str(public.key),
                        "publicOrigin": public.origin,
                        "agentOrigin": origin,
                        "prerequisites": public.terms({}),
                    }
                )
            )
            descriptor = {
                "agentOrigin": origin,
                "cardUrl": origin + "/.well-known/agent-card.json",
                "fixtureOrigins": [origin, public.origin],
                "caFile": str(public.certificate),
                "invocationLog": str(tmp_path / "invocations.jsonl"),
                "controlCommand": [sys.executable, "-m", "tests.layer8_controller", str(control)],
            }
            target = tmp_path / "target.json"
            target.write_bytes(jsonBytes(descriptor))
            report = tmp_path / "external.xml"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "tests/conformance/test_agent_profile.py",
                    "--l2-target",
                    str(target),
                    "--junitxml",
                    str(report),
                    "-q",
                    "-x",
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            cases = ET.parse(report).findall(".//testcase")
            assert len(cases) == 24 and not any(case.find("skipped") is not None for case in cases)
    finally:
        if control.exists():
            pid = strictJson(control.read_bytes()).get("pid")
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        public.close()

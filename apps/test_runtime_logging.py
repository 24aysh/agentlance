"""Process logging must preserve machine-readable stdout and avoid HTTP URL logs."""

import json
import re
import subprocess
import sys


def testRuntimeLogsUseUtcStderrWithoutHttpPayloads():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from apps.runtime_logging import configureLogging; import logging, json; "
            "logging.basicConfig(level=logging.DEBUG); "
            "configureLogging(); configureLogging(); "
            "logging.getLogger('modules.example').info('event=ready task_id=7'); "
            "logging.getLogger('httpx').info('SECRET_HTTP_URL'); "
            "print(json.dumps({'ok': True}))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == {"ok": True}
    assert re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ INFO modules.example event=ready task_id=7\n",
        result.stderr,
    )
    assert "SECRET" not in result.stderr

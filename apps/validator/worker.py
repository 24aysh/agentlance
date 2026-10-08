"""One bounded, exact-byte request per isolated evaluator container."""

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# JSON integers are exact, including values outside the permitted output shape.
# CPU/memory limits are enforced by the host before this parser runs.
sys.set_int_max_str_digits(0)
from modules.validation.evaluator import evaluateArtifact, evidenceBytes  # noqa: E402


def main():
    raw = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("Frame limit")
    request = json.loads(raw)
    if set(request) != {"task", "result", "input", "shape", "policy", "artifact"}:
        raise ValueError("Frame fields")
    evidence = evaluateArtifact(
        request["task"],
        request["result"],
        *(
            base64.b64decode(request[k], validate=True)
            for k in ("input", "shape", "policy", "artifact")
        ),
    )
    sys.stdout.buffer.write(evidenceBytes(evidence))


if __name__ == "__main__":
    main()

"""Pinned image entry point: the two reference transforms, never arbitrary task code."""

import json
import sys


def transform(request):
    kind, value = request["kind"], request["input"]
    if kind == "PLAN":
        return request["proposal"]
    if kind == "SYNTHESIZE":
        return {name: value[name]["value"] for name in ("left", "right")}
    if kind in {"SOLO", "FALLBACK"}:
        keys = set(value)
        if keys not in ({"value"}, {"left", "right"}) or any(
            type(v) is not int or not -1000 <= v <= 1000 for v in value.values()
        ):
            raise ValueError("Unsupported structured input")
        return value
    raise ValueError("Unknown transform")


def main():
    raw = sys.stdin.buffer.read(1048577)
    if len(raw) > 1048576:
        raise ValueError("Input limit")
    sys.stdout.write(json.dumps(transform(json.loads(raw)), separators=(",", ":")))


if __name__ == "__main__":
    main()

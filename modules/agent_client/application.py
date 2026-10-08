"""Versioned application output validation, referencing the frozen protocol schema."""

import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from modules.agent_client.ports import ensure

ROOT = Path(__file__).resolve().parents[2]


@lru_cache
def outputValidator(kind):
    path = ROOT / "specs/schemas"
    schema = json.loads((path / "application.schema.json").read_text())
    protocol = json.loads((path / "protocol.schema.json").read_text())
    registry = Registry().with_resources(
        [
            (schema["$id"], Resource.from_contents(schema)),
            (protocol["$id"], Resource.from_contents(protocol)),
        ]
    )
    return Draft202012Validator({"$ref": schema["$id"] + "#/$defs/" + kind}, registry=registry)


def validateOutput(value, kind="Envelope"):
    errors = list(outputValidator(kind).iter_errors(value))
    ensure(
        not errors,
        "Invalid application output" + (": " + str(errors[0].json_path) if errors else ""),
    )
    return value

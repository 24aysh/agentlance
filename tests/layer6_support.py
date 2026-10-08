"""Isolated SDK model and real Docker profiles; no paid model requests."""

from copy import deepcopy
from pathlib import Path

from agents import Model, ModelResponse
from agents.usage import Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from modules.adapters.execution.docker import DockerExecutor
from modules.adapters.execution.sdk import AgentsExecutor
from modules.execution.coordinator import ExecutionCoordinator
from modules.execution.rules import executionDigest, quantities
from tests.layer3_support import readJson


class IsolatedModel(Model):
    def __init__(self):
        self.calls = 0

    async def get_response(self, system_instructions, input, *args, **kwargs):
        self.calls += 1
        toolOutputs = (
            [item for item in input if item.get("type") == "function_call_output"]
            if isinstance(input, list)
            else []
        )
        if toolOutputs:
            output = ResponseOutputMessage(
                id="fixture-message",
                role="assistant",
                status="completed",
                content=[
                    ResponseOutputText(
                        type="output_text", text=toolOutputs[-1]["output"], annotations=[]
                    )
                ],
                type="message",
            )
        else:
            output = ResponseFunctionToolCall(
                id="fixture-tool",
                call_id="fixture-call",
                name="transform",
                arguments="{}",
                type="function_call",
            )
        return ModelResponse(
            output=[output],
            usage=Usage(requests=1, input_tokens=10, output_tokens=10, total_tokens=20),
            response_id=None,
        )

    def stream_response(self, *args, **kwargs):
        raise AssertionError("No streaming")


def executionProfile(participant, *, sdk=False, manager=False):
    economic = readJson("specs/fixtures/layer-5/economics.json")
    profile = readJson("specs/fixtures/layer-6/execution.json")
    profile["operator"] = participant.signer
    profile["executor"]["image"] = Path(".scratch/layer6/image-id").read_text().strip()
    profile["executor"]["kind"] = "AGENTS" if sdk else "DETERMINISTIC"
    profile["runtime"] = deepcopy(economic["runtime"])
    profile["pricing"] = deepcopy(economic["pricing"])
    profile["runtime"]["outputSchemaDigest"], profile["runtime"]["validationPolicyDigest"] = (
        participant.supportedDigests
    )
    if sdk:
        profile["runtime"]["resources"] += [
            {"resource": "modelInput", "quantityUnit": "token"},
            {"resource": "modelOutput", "quantityUnit": "token"},
        ]
        for resource in profile["runtime"]["resources"][1:]:
            profile["pricing"]["rates"].append(
                resource
                | {
                    "rate": {"numerator": "1", "denominator": "1"},
                    "unit": {"currency": "MON", "decimals": 18},
                }
            )
    usage = quantities(profile, transform=8, modelInput=160000, modelOutput=65536)
    profile["runtime"]["envelope"]["usage"] = usage
    profile["runtime"]["envelope"]["justification"] = (
        "SYNTHETIC: metered calls, including planning, fallback and synthesis."
    )
    profile["runtime"]["scenarios"][0]["usage"] = usage
    if manager:
        profile["delegation"].update(enabled=True, maxChildren=2, ownWorkReserveAtoms="20")
    profile["runtime"]["configDigest"] = executionDigest(profile)
    return profile


def composeExecution(participant, clock, *, sdk=False, manager=False, profile=None):
    profile = profile or executionProfile(participant, sdk=sdk, manager=manager)
    docker = DockerExecutor()
    model = IsolatedModel()
    agents = AgentsExecutor(docker, model=model)
    coordinator = ExecutionCoordinator(participant, profile, docker, agents, clock=clock)
    return coordinator, model


async def drainExecution(coordinator):
    for _ in range(4):
        if not coordinator.jobs:
            return
        await __import__("asyncio").gather(*coordinator.jobs.values())
        await coordinator.tick()


def parentFixtures():
    shape = {
        "type": "object",
        "properties": {
            k: {"type": "integer", "minimum": -1000, "maximum": 1000} for k in ("left", "right")
        },
        "required": ["left", "right"],
        "additionalProperties": False,
    }
    policy = deepcopy(readJson("specs/fixtures/layer-2/policy.json"))
    from modules.adapters.a2a.profile import jsonBytes
    from modules.agent_client.signing import contentDigest

    policy["outputSchemaDigest"] = contentDigest(jsonBytes(shape))
    predicate = policy["predicates"][0]
    policy["predicates"] = [
        predicate | {"outputPointer": "/" + k, "inputPointer": "/" + k} for k in ("left", "right")
    ]
    return jsonBytes({"left": 7, "right": 9}), jsonBytes(shape), jsonBytes(policy)

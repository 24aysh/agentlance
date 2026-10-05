"""ERC-8004 discovery provenance, never transaction authority."""

from a2a.types import AgentCard
from google.protobuf.json_format import ParseError

from modules.adapters.a2a.profile import URN, strictJson, toProto
from modules.agent_client.ports import ResolvedProfile, checkIdentity, ensure, recordCheck
from modules.agent_client.signing import contentDigest
from modules.domain.records import isContentUri


def selectInterface(card):
    ensure(isinstance(card, dict), "Agent Card object required")
    ensure(isinstance(card.get("capabilities"), dict), "Capabilities object required")
    for field in ("skills", "supportedInterfaces"):
        ensure(
            isinstance(card.get(field), list)
            and all(isinstance(item, dict) for item in card[field]),
            "Card object array required",
        )
    extensions = card["capabilities"].get("extensions", [])
    ensure(
        isinstance(extensions, list) and all(isinstance(item, dict) for item in extensions),
        "Extensions array required",
    )
    for field in ("defaultInputModes", "defaultOutputModes"):
        ensure(isinstance(card.get(field), list), "Modes array required")
    try:
        toProto(card, AgentCard)
    except (ParseError, TypeError, ValueError) as error:
        ensure(False, f"Invalid SDK Agent Card: {error}")
    for field in ("name", "description", "version"):
        ensure(isinstance(card.get(field), str) and bool(card[field]), f"Missing {field}")
    for field in ("defaultInputModes", "defaultOutputModes"):
        ensure("application/json" in card.get(field, []), "JSON mode required")
    skills = card.get("skills", [])
    ensure(bool(skills), "Skill required")
    ids = [skill.get("id") for skill in skills]
    ensure(
        all(isinstance(value, str) and 0 < len(value) <= 64 for value in ids)
        and len(ids) == len(set(ids)) <= 32,
        "Invalid skills",
    )
    extensions = card.get("capabilities", {}).get("extensions", [])
    ours = [item for item in extensions if item.get("uri") == URN]
    ensure(len(ours) == 1 and ours[0].get("required") is True, "Required profile missing")
    ensure(
        all(item.get("uri") == URN or not item.get("required") for item in extensions),
        "Unknown required extension",
        "UNSUPPORTED",
    )
    for index, interface in enumerate(card.get("supportedInterfaces", [])):
        if (
            interface.get("protocolVersion") == "1.0"
            and interface.get("protocolBinding") == "HTTP+JSON"
        ):
            ensure(not interface.get("tenant"), "Tenancy unsupported", "UNSUPPORTED")
            uri = interface.get("url", "")
            ensure(isContentUri(uri) and uri.startswith("https://"), "HTTPS interface required")
            return index
    ensure(False, "No A2A 1.0 HTTP+JSON interface", "UNSUPPORTED")


async def resolveProfile(agentRef, registry, content, schema, chainId, identityRegistry):
    recordCheck(agentRef, "AgentRef", schema)
    ensure(
        agentRef["chainId"] == str(chainId) and agentRef["identityRegistry"] == identityRegistry,
        "Registry namespace",
    )
    snapshot = await registry.readIdentity(agentRef)
    checkIdentity(snapshot, schema)
    ensure(snapshot["agentRef"] == agentRef, "Identity binding")
    ensure(snapshot["stamp"]["finality"] == "FINALIZED", "Identity not finalized", "UNAVAILABLE")
    raw = await content.fetchBytes(snapshot["registrationUri"], 65536)
    registration = strictJson(raw)
    ensure(isinstance(registration, dict), "Registration object")
    ensure(
        registration.get("type") == "https://eips.ethereum.org/EIPS/eip-8004#registration-v1",
        "Registration type",
    )
    ensure(
        all(
            isinstance(registration.get(field), str) and registration[field]
            for field in ("name", "description")
        ),
        "Registration description",
    )
    ensure(registration.get("active", True) is True, "Inactive registration")
    entries = registration.get("registrations", [])
    ensure(
        isinstance(entries, list) and all(isinstance(item, dict) for item in entries),
        "Registrations",
    )
    namespace = f"eip155:{agentRef['chainId']}:{agentRef['identityRegistry']}"
    ensure(
        any(
            item.get("agentRegistry") == namespace
            and type(item.get("agentId")) is int
            and item["agentId"] == int(agentRef["agentId"])
            for item in entries
        ),
        "Registration binding",
    )
    services = registration.get("services")
    ensure(
        isinstance(services, list) and all(isinstance(item, dict) for item in services), "Services"
    )
    ensure(
        all(
            isinstance(item.get("endpoint"), str) for item in services if item.get("name") == "A2A"
        ),
        "Endpoint string required",
    )
    endpoints = {
        item.get("endpoint")
        for item in services
        if item.get("name") == "A2A" and item.get("version") in (None, "1.0", "1.0.0")
    }
    ensure(len(endpoints) == 1, "Ambiguous or missing A2A endpoint")
    endpoint = endpoints.pop()
    ensure(isContentUri(endpoint) and endpoint.startswith("https://"), "Card HTTPS URL")
    cardBytes = await content.fetchBytes(endpoint, 65536)
    card = strictJson(cardBytes)
    index = selectInterface(card)
    profile = {
        "schemaVersion": 1,
        "agentRef": agentRef,
        "registration": {"uri": snapshot["registrationUri"], "digest": contentDigest(raw)},
        "agentCard": {"uri": endpoint, "digest": contentDigest(cardBytes)},
        "capabilities": sorted(skill["id"] for skill in card["skills"]),
        "a2aVersion": "1.0",
        "binding": "HTTP+JSON",
        "profileVersion": 1,
        "extensionUri": URN,
    }
    recordCheck(profile, "AgentProfile", schema)
    return ResolvedProfile(profile, snapshot, raw, cardBytes, index)

"""EIP-712 verification, separate from market nonce and admission authority."""

from copy import deepcopy

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_keys.exceptions import BadSignature
from eth_utils import keccak

from modules.agent_client.ports import ensure

CURVE_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def contentDigest(raw):
    return "0x" + keccak(raw).hex()


def buildBidPermitTypedData(permit, configuredDomain, types):
    ref, agent = permit["taskRef"], permit["agentRef"]
    ensure(
        ref["chainId"] == agent["chainId"] == str(configuredDomain["chainId"])
        and ref["market"] == configuredDomain["verifyingContract"],
        "Permit namespace",
    )
    ensure(
        configuredDomain["name"] == "AgentLance" and configuredDomain["version"] == "1",
        "Permit domain",
    )
    fields = {
        key: deepcopy(value) for key, value in permit.items() if key not in {"taskRef", "agentRef"}
    }
    fields.update(
        taskId=ref["taskId"], identityRegistry=agent["identityRegistry"], agentId=agent["agentId"]
    )
    return {
        "types": {name: deepcopy(types[name]) for name in ("EIP712Domain", "BidPermit")},
        "primaryType": "BidPermit",
        "domain": deepcopy(configuredDomain),
        "message": fields,
    }


def typedDigest(typed):
    encoded = encode_typed_data(full_message=typed)
    return contentDigest(b"\x19\x01" + encoded.header + encoded.body)


def verifyEoa(typed, signature, owner):
    try:
        raw = bytes.fromhex(signature[2:])
        if (
            not signature.startswith("0x")
            or len(raw) != 65
            or raw[64] not in (27, 28)
            or not 0 < int.from_bytes(raw[:32]) < CURVE_ORDER
            or not 0 < int.from_bytes(raw[32:64]) <= CURVE_ORDER // 2
        ):
            return False
        return (
            Account.recover_message(encode_typed_data(full_message=typed), signature=raw).lower()
            == owner
        )
    except (BadSignature, ValueError, TypeError, OverflowError):
        return False


def verifyContractReturn(result):
    if not isinstance(result, dict) or set(result) != {"outcome", "returnData"}:
        return False
    if result["outcome"] != "RETURNED" or not isinstance(result["returnData"], str):
        return False
    value = result["returnData"]
    # bytes4 is left-aligned in a complete 32-byte ABI word, not a four-byte RPC response.
    return value == "0x1626ba7e" + "00" * 28


async def verifyBidPermit(
    offer, permit, signature, registrySnapshot, configuredDomain, types, registryPort
):
    if (
        permit["owner"] != registrySnapshot["owner"]
        or registrySnapshot["agentRef"] != offer["agentRef"]
        or registrySnapshot["verifiedWallet"] != offer["payout"]
        or any(permit.get(key) != value for key, value in offer.items())
    ):
        return False
    typed = buildBidPermitTypedData(permit, configuredDomain, types)
    if not registrySnapshot["ownerHasCode"]:
        return verifyEoa(typed, signature, permit["owner"])
    if len(signature) > 2 + 4096 * 2:
        return False
    result = await registryPort.checkContractSignature(
        permit["owner"], typedDigest(typed), signature, registrySnapshot["stamp"], gasLimit=50000
    )
    return verifyContractReturn(result)

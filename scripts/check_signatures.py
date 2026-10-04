"""Verify stored EIP-712 data with eth-account; never regenerate expected outputs."""

import copy

from eth_account import Account
from eth_account._utils.encode_typed_data.encoding_and_hashing import hash_type
from eth_account.messages import encode_typed_data
from eth_utils import keccak


def hexBytes(value):
    return "0x" + value.hex()


def verifySignatures(vectors, types):
    for vector in vectors["positive"]:
        data = vector["typedData"]
        primary = data["primaryType"]
        if data["types"] != {n: types[n] for n in ["EIP712Domain", primary]}:
            raise ValueError("Typed definition differs from frozen types")
        signature = bytes.fromhex(vector["signature"][2:])
        curveOrder = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
        if (
            len(signature) != 65
            or signature[64] not in (27, 28)
            or not 0 < int.from_bytes(signature[32:64]) <= curveOrder // 2
        ):
            raise ValueError("Noncanonical ECDSA signature")
        encoded = encode_typed_data(full_message=data)
        actual = {
            "typeHash": hexBytes(hash_type(primary, data["types"])),
            "domainSeparator": hexBytes(encoded.header),
            "structHash": hexBytes(encoded.body),
            "digest": hexBytes(keccak(b"\x19\x01" + encoded.header + encoded.body)),
            "recoveredSigner": Account.recover_message(
                encoded, signature=vector["signature"]
            ).lower(),
        }
        for field, value in actual.items():
            if vector[field] != value:
                raise ValueError(f"{vector['id']}: incorrect {field}")
    for vector in vectors["negative"]:
        base = next(v for v in vectors["positive"] if v["id"] == vector["base"])
        if vector["mutation"] is not None:
            data = copy.deepcopy(base["typedData"])
            mutation = vector["mutation"]
            if mutation["section"] == "primaryType":
                data["types"][mutation["value"]] = data["types"].pop(data["primaryType"])
                data["primaryType"] = mutation["value"]
            else:
                data[mutation["section"]][mutation["field"]] = mutation["value"]
            recovered = Account.recover_message(
                encode_typed_data(full_message=data), signature=base["signature"]
            ).lower()
            if (
                recovered == base["recoveredSigner"]
                or vector["expectedError"] != "INVALID_SIGNATURE"
            ):
                raise ValueError(f"{vector['id']}: mutation did not invalidate signer")
        else:
            context = vector["context"]
            message = base["typedData"]["message"]
            predicates = {
                "NONCE_USED": context.get("usedNonce") is True,
                "SIGNATURE_EXPIRED": int(context.get("now", "0")) >= int(message["expiry"]),
                "OWNER_CHANGED": context.get("currentOwner", message.get("owner"))
                != message.get("owner"),
            }
            if not predicates.get(vector["expectedError"], False):
                raise ValueError(f"{vector['id']}: invalid authorization example")
    for vector in vectors["contentHashes"]:
        if hexBytes(keccak(bytes.fromhex(vector["bytesHex"][2:]))) != vector["keccak256"]:
            raise ValueError(f"{vector['id']}: incorrect content digest")

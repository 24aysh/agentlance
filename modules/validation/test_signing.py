from copy import deepcopy

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from modules.agent_client.ports import AdapterError
from modules.agent_client.signing import buildValidationVerdictTypedData, typedDigest, verifyEoa
from modules.validation.qualification import buildValidatorEvidence, verifyValidatorEvidence
from tests.layer3_support import readJson


def testVerdictBuilderMatchesFrozenVectorAndLocatorSemantics():
    objects = {x["type"]: x["value"] for x in readJson("specs/fixtures/objects.json")["objects"]}
    vectors = readJson("specs/fixtures/signatures.json")
    vector = next(x for x in vectors["positive"] if x["id"] == "ValidationVerdict")
    record = deepcopy(objects["ValidationRecord"])
    typed = buildValidationVerdictTypedData(
        record, vector["typedData"]["domain"], readJson("specs/signing/types.json")
    )
    assert typedDigest(typed) == typedDigest(vector["typedData"])
    signer = Account.from_key(vectors["testPrivateKeys"][1])
    signature = "0x" + signer.sign_message(encode_typed_data(full_message=typed)).signature.hex()
    assert verifyEoa(typed, signature, signer.address.lower())
    record["evidence"]["uri"] = "ipfs://different-locator"
    assert typedDigest(
        buildValidationVerdictTypedData(
            record, typed["domain"], readJson("specs/signing/types.json")
        )
    ) == typedDigest(typed)
    for field in ["resultDigest", "validationPolicyDigest", "evidenceDigest"]:
        wrong = deepcopy(typed)
        wrong["message"][field] = "0x" + "ff" * 32
        assert not verifyEoa(wrong, signature, signer.address.lower())
    for field in ["taskId", "agentId", "awardId", "nonce", "expiry"]:
        wrong = deepcopy(typed)
        wrong["message"][field] = int(wrong["message"][field]) + 1
        assert not verifyEoa(wrong, signature, signer.address.lower())


def testValidatorQualificationBindsImageKeyAndExpiry():
    signer = Account.from_key(bytes.fromhex("00" * 31 + "01"))
    facts = {"chainId": "31337", "market": "0x" + "11" * 20, "validator": signer.address.lower()}
    image = "sha256:" + "ab" * 32
    evidence = buildValidatorEvidence(signer, facts, image, 2000, now=1000)
    verifyValidatorEvidence(evidence, facts, image, now=1500)
    with pytest.raises(AdapterError):
        verifyValidatorEvidence(evidence, facts, image, now=2000)
    with pytest.raises(AdapterError):
        verifyValidatorEvidence(evidence, facts, "sha256:" + "cd" * 32, now=1500)

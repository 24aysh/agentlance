import copy
import tempfile
import unittest
from pathlib import Path

from check_specs import readJson, repoRoot, validateMarketVectors
from jsonschema import ValidationError


class SpecChecks(unittest.TestCase):
    def setUp(self):
        self.schema = readJson(repoRoot / "specs/schemas/market-vectors.schema.json")
        self.document = readJson(repoRoot / "specs/fixtures/market.json")

    def testPublishedFixtures(self):
        self.assertEqual(validateMarketVectors(self.document, self.schema), 18)

    def testRejectDuplicateCaseIds(self):
        self.document["cases"].append(copy.deepcopy(self.document["cases"][0]))
        with self.assertRaisesRegex(ValueError, "Duplicate fixture ID"):
            validateMarketVectors(self.document, self.schema)

    def testRejectAmbiguousOrInvalidJson(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            for text in [
                '{"budget":"1","budget":"2"}',
                '{"p":NaN}',
                '{"p":Infinity}',
                '{"p":1.0}',
                '{"p":1e3}',
            ]:
                with self.subTest(text=text):
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        readJson(path)

    def testRejectInvalidAmountEncodingAndOverflow(self):
        for value in [1, "-1", "01", "+1", "1e3", " 1", "1\n", str(2**96)]:
            with self.subTest(value=value):
                document = copy.deepcopy(self.document)
                document["cases"][0]["input"]["budgetAtoms"] = value
                with self.assertRaises(ValidationError):
                    validateMarketVectors(document, self.schema)

    def testUnsignedAndSignedBoundaries(self):
        case = self.document["cases"][0]
        case["input"]["bids"][0]["agentRef"]["agentId"] = str(2**256 - 1)
        case["expected"]["winningScore"] = str(-(2**255))
        validateMarketVectors(self.document, self.schema)
        for value in [str(-(2**255) - 1), str(2**255), "-0"]:
            with self.subTest(score=value):
                case["expected"]["winningScore"] = value
                with self.assertRaises(ValidationError):
                    validateMarketVectors(self.document, self.schema)
        case["expected"]["winningScore"] = str(2**255 - 1)
        validateMarketVectors(self.document, self.schema)
        case["input"]["bids"][0]["agentRef"]["agentId"] = str(2**256)
        with self.assertRaises(ValidationError):
            validateMarketVectors(self.document, self.schema)

    def testRejectInvalidProbabilityAndAddress(self):
        bid = self.document["cases"][0]["input"]["bids"][0]
        for value in [-1, 1000001, 0.5, "500000", True]:
            with self.subTest(probability=value):
                bid["p"] = value
                with self.assertRaises(ValidationError):
                    validateMarketVectors(self.document, self.schema)
        bid["p"] = 500000
        bid["agentRef"]["identityRegistry"] += "\n"
        with self.assertRaises(ValidationError):
            validateMarketVectors(self.document, self.schema)

    def testRejectUnknownFieldAndNonSyntheticFixture(self):
        self.document["cases"][0]["input"]["costAtoms"] = "1"
        with self.assertRaises(ValidationError):
            validateMarketVectors(self.document, self.schema)
        del self.document["cases"][0]["input"]["costAtoms"]
        self.document["synthetic"] = False
        with self.assertRaises(ValidationError):
            validateMarketVectors(self.document, self.schema)


class ProtocolArtifactChecks(unittest.TestCase):
    def setUp(self):
        from check_specs import validateObject

        self.validateObject = validateObject
        self.schema = readJson(repoRoot / "specs/schemas/protocol.schema.json")
        self.objects = {
            record["id"]: record["value"]
            for record in readJson(repoRoot / "specs/fixtures/objects.json")["objects"]
        }

    def testClosedCanonicalTypes(self):
        for name in [
            "AgentRef",
            "TaskRef",
            "AgentProfile",
            "TaskSpec",
            "Bid",
            "Allocation",
            "ExecutionRef",
            "ResultCommitment",
            "ValidationRecord",
            "SettlementReceipt",
            "ReputationEvidence",
            "CostEstimate",
            "CostReport",
        ]:
            with self.subTest(type=name):
                value = copy.deepcopy(self.objects[name])
                value["unknown"] = 1
                with self.assertRaises(ValidationError):
                    self.validateObject(value, name, self.schema)

    def testNullIsRequiredAndTaskIdCannotBeZero(self):
        task = copy.deepcopy(self.objects["TaskSpec"])
        del task["parentRef"]
        with self.assertRaises(ValidationError):
            self.validateObject(task, "TaskSpec", self.schema)
        ref = {**self.objects["TaskRef"], "taskId": "0"}
        with self.assertRaises(ValidationError):
            self.validateObject(ref, "TaskRef", self.schema)

    def testUriByteBoundsAndSchemes(self):
        task = copy.deepcopy(self.objects["TaskSpec"])
        for uri in [
            "file:///etc/passwd",
            "https://user:secret@example.com/a",
            "https://example.com/" + "é" * 1024,
            "https://example.com/a#fragment",
        ]:
            with self.subTest(uri=uri[:60]):
                task["terms"]["input"]["uri"] = uri
                with self.assertRaises(ValidationError):
                    self.validateObject(task, "TaskSpec", self.schema)

    def testNoAwardCannotCarryWinner(self):
        value = copy.deepcopy(self.objects["Allocation"])
        value["outcome"] = "UNALLOCATED"
        with self.assertRaises(ValidationError):
            self.validateObject(value, "Allocation", self.schema)

    def testUnsupportedEvaluatorKeyword(self):
        shape = copy.deepcopy(self.objects["OutputShape"])
        shape["$ref"] = "https://example.com/arbitrary-schema"
        with self.assertRaises(ValidationError):
            self.validateObject(shape, "OutputShape", self.schema)

    def testTamperedCryptographicExpectations(self):
        from check_signatures import verifySignatures

        types = readJson(repoRoot / "specs/signing/types.json")
        vectors = readJson(repoRoot / "specs/fixtures/signatures.json")
        for field in ["typeHash", "structHash", "domainSeparator", "digest", "recoveredSigner"]:
            with self.subTest(field=field):
                mutated = copy.deepcopy(vectors)
                mutated["positive"][0][field] = "0x" + "00" * 32
                with self.assertRaises(ValueError):
                    verifySignatures(mutated, types)

    def testAbiDriftIsRejected(self):
        from check_abi import verifyAbi

        abi = readJson(repoRoot / "specs/protocol.abi.json")
        abi[0]["inputs"][0]["components"][0]["type"] = "uint256"
        with self.assertRaisesRegex(ValueError, "ABI differs"):
            verifyAbi(abi, self.schema)

    def testMalleableSignatureIsRejected(self):
        from check_signatures import verifySignatures

        vectors = readJson(repoRoot / "specs/fixtures/signatures.json")
        signature = bytearray.fromhex(vectors["positive"][0]["signature"][2:])
        order = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
        signature[32:64] = (order - int.from_bytes(signature[32:64])).to_bytes(32)
        signature[64] = 55 - signature[64]
        vectors["positive"][0]["signature"] = "0x" + signature.hex()
        with self.assertRaisesRegex(ValueError, "Noncanonical"):
            verifySignatures(vectors, readJson(repoRoot / "specs/signing/types.json"))


if __name__ == "__main__":
    unittest.main()

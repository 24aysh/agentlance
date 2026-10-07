"""Real EVM differential URI parsing against Python's independent URL parser."""

import ipaddress
import json
import platform
import random
import unicodedata

import pytest
from eth_abi import decode, encode
from eth_utils import keccak

from modules.domain.records import isContentUri
from tests.layer3_support import ROOT, localNode, readJson

SEED = 0xC017E17


def uriCorpus(fixture):
    cases = [(case["id"], case["uri"].encode("utf-8")) for case in fixture["cases"]]
    cases.extend((case["id"], bytes.fromhex(case["hexValue"][2:])) for case in fixture["rawCases"])
    randomSource = random.Random(SEED)
    originals = [case["uri"] for case in fixture["cases"] if len(case["uri"]) < 128]
    alphabet = list("httpsipfs:/?#[].@%0123456789abcdefABCDEFvV+-\\") + [
        "\x00",
        "\x01",
        "\x08",
        "\x09",
        "\x1f",
        "\x7f",
        "\x85",
        "\u0080",
        "\u00a0",
        "\u1680",
        "\u200b",
        "\u2028",
        "\u2047",
        "\u2100",
        "\uff1a",
        "\uff20",
        "🍋",
    ]
    for index in range(512):
        source = randomSource.choice(originals)
        position = randomSource.randrange(len(source) + 1)
        width = randomSource.randrange(4)
        replacement = "".join(randomSource.choices(alphabet, k=randomSource.randrange(5)))
        mutated = source[:position] + replacement + source[position + width :]
        cases.append((f"mutation-{index}", mutated.encode("utf-8")))
    for index in range(256):
        address = ipaddress.IPv6Address(randomSource.getrandbits(128))
        address = address.exploded if index % 2 else address.compressed
        scope = randomSource.choice(["", "%eth0", "%接口", "%", "%two%scopes"])
        port = randomSource.choice(["", ":", ":0", ":1", ":65535", ":65536"])
        cases.append((f"ip-literal-{index}", f"https://[{address}{scope}]{port}".encode()))
    for index in range(256):
        # Every Unicode scalar is eligible; lone surrogates are tested as raw UTF-8 below.
        scalar = randomSource.randrange(0x110000)
        if 0xD800 <= scalar <= 0xDFFF:
            scalar = 0x20
        character = chr(scalar)
        uri = f"https://a{character}.example/{character}" if index % 2 else f"ipfs://x/{character}"
        cases.append((f"unicode-scalar-{index}", uri.encode()))
    for index in range(256):
        raw = b"https://x/" + randomSource.randbytes(randomSource.randrange(1, 16))
        cases.append((f"raw-utf8-{index}", raw))
    return cases


@pytest.mark.l3socket
def testUriEvmMatchesPinnedPython(tmp_path):
    fixture = readJson("specs/fixtures/layer-3-uri.json")
    assert platform.python_version() == fixture["oracle"]["python"]
    assert unicodedata.unidata_version == fixture["oracle"]["unicode"]
    compiled = readJson(".scratch/layer3/out/ContentUri.t.sol/ContentUriHarness.json")
    corpus = uriCorpus(fixture)
    accepted = 0
    with localNode(tmp_path / "uri-node") as rpc:
        caller = rpc.call("eth_accounts")[0]
        receipt = rpc.receipt(
            rpc.call(
                "eth_sendTransaction",
                {
                    "from": caller,
                    "data": compiled["bytecode"]["object"],
                    "gas": hex(5_000_000),
                },
            )
        )
        assert int(receipt["status"], 16) == 1
        address = receipt["contractAddress"]
        for caseId, raw in corpus:
            try:
                expected = isContentUri(raw.decode("utf-8"))
            except UnicodeDecodeError:
                expected = False
            # bytes and string have identical ABI encoding; bytes admits malformed UTF-8.
            calldata = keccak(text="isContentUri(string)")[:4] + encode(["bytes"], [raw])
            returned = rpc.call(
                "eth_call",
                {
                    "to": address,
                    "data": "0x" + calldata.hex(),
                    "gas": hex(30_000_000),
                },
                "latest",
            )
            actual = decode(["bool"], bytes.fromhex(returned[2:]))[0]
            assert actual == expected, (caseId, raw.hex(), expected, actual, SEED)
            accepted += actual
    report = {
        "python": platform.python_version(),
        "unicode": unicodedata.unidata_version,
        "oracle": "modules/domain/records.py:isContentUri",
        "seed": SEED,
        "network": "monad",
        "hardfork": "MonadTen",
        "caseCount": len(corpus),
        "accepted": accepted,
        "rejected": len(corpus) - accepted,
        "fixtureIds": [
            case["id"] for section in ("cases", "rawCases") for case in fixture[section]
        ],
    }
    reportPath = ROOT / ".scratch/layer3/uri-differential.json"
    reportPath.parent.mkdir(parents=True, exist_ok=True)
    reportPath.write_text(json.dumps(report, indent=2) + "\n")

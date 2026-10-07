"""Reviewed ContentUri parity boundaries, tied to the unchanged Python oracle."""

import json
import sys
import unicodedata
from pathlib import Path

import pytest

from modules.domain.records import isContentUri

fixture = json.loads(Path("specs/fixtures/layer-3-uri.json").read_text())


@pytest.mark.parametrize("case", fixture["cases"], ids=lambda case: case["id"])
def testReviewedUriBoundary(case):
    assert isContentUri(case["uri"]) is case["valid"]


@pytest.mark.parametrize("case", fixture["rawCases"], ids=lambda case: case["id"])
def testInvalidUtf8Boundary(case):
    with pytest.raises(UnicodeDecodeError):
        bytes.fromhex(case["hexValue"][2:]).decode("utf-8")
    assert case["valid"] is False


def testUriUnicodeTablesAreComplete():
    assert sys.version_info[:2] == (3, 12)
    assert unicodedata.unidata_version == fixture["oracle"]["unicode"]
    assert [value for value in range(0x110000) if chr(value).isspace()] == fixture["oracle"][
        "whitespaceCodepoints"
    ]
    assert [
        value
        for value in range(128, 0x110000)
        if set(unicodedata.normalize("NFKC", chr(value))) & set("/?#@:")
    ] == fixture["oracle"]["nfkcAuthorityDelimiters"]

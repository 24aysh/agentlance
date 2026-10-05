import pytest

from apps.reference_agent.worker import executeFixture
from modules.agent_client.ports import AdapterError


@pytest.mark.parametrize("raw", [b'{"value":7}\n', b'{ "value": -1000 }', b'{"value":1000}'])
def testExactWorkerBytes(raw):
    assert executeFixture(raw) == raw


@pytest.mark.parametrize(
    "raw",
    [
        b'{"value":true}',
        b'{"value":1.0}',
        b'{"value":1001}',
        b'{"value":-1001}',
        b'{"value":1,"other":2}',
        b'{"value":1,"value":2}',
        b"[]",
        b"null",
        b" " * 1048577,
    ],
)
def testUnsupportedInput(raw):
    with pytest.raises(AdapterError):
        executeFixture(raw)

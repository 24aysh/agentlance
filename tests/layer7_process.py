"""Local fixture clock/transport around the production validator, in a separate process."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

from modules.adapters.a2a.profile import jsonBytes, strictJson
from tests.layer3_support import Rpc, WireCodec, readJson
from tests.layer6_process import publicTransport
from tests.layer7_support import ValidatorHarness


async def main(config):
    rpc = Rpc(config["rpc"])
    rig = SimpleNamespace(
        market=config["facts"]["market"],
        keys={"validator": bytes.fromhex(config["validatorKey"][2:])},
        codec=WireCodec(),
        readAbi=readJson("specs/contracts.read.abi.json"),
    )
    rig.now = lambda: int(rpc.call("eth_getBlockByNumber", "latest", False)["timestamp"], 16)
    rig.advance = lambda now: (rpc.call("evm_setNextBlockTimestamp", now), rpc.call("evm_mine"))
    env = SimpleNamespace(
        rig=rig,
        rpc=rpc,
        facts=config["facts"],
        genesis=config["genesis"],
        finalize=lambda: rpc.call("anvil_mine", "0x40"),
    )
    transport = publicTransport(Path(config["public"]))
    validator = ValidatorHarness(
        env,
        Path(config["directory"]),
        config["kubo"],
        transport.handler,
        deployment=config["deployment"],
    )
    print('{"ready":true}', flush=True)
    try:
        while line := await asyncio.to_thread(sys.stdin.buffer.readline):
            request = strictJson(line)
            if request == {"command": "close"}:
                break
            if request == {"command": "restart"}:
                await validator.restart()
            elif request == {"command": "tick"}:
                await validator.tick()
            else:
                raise ValueError("Unsupported fixture control")
            result = {
                "outcomes": [
                    validator.runtime.observer.readOutcome(row["task"]["taskRef"])
                    for _, row in validator.store.rows("receipt")
                ],
                "jobs": validator.store.rows("job"),
                "transactions": [
                    {
                        "operationId": op["operationId"],
                        "kind": op.get("command", {}).get("command", "publish"),
                        "transactionHash": op["transactionHash"],
                        "result": op["result"],
                    }
                    for op in validator.journal.nativeOperations()
                ],
            }
            print(jsonBytes(result).decode(), flush=True)
    finally:
        await validator.close()
        rpc.client.close()


if __name__ == "__main__":
    asyncio.run(main(strictJson(Path(sys.argv[1]).read_bytes())))

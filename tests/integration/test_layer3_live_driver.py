"""Exercise the actual testnet scenario driver only against isolated Monad Anvil.

The test transport advances its own local clock; the production driver contains no
Anvil RPC or registry mutation. Qualification/finality failures have separate unit tests.
"""

from types import SimpleNamespace

import pytest
from eth_abi import encode
from eth_account import Account

import scripts.layer3_testnet as live
from tests.layer3_support import Layer3Rig, localNode, readJson

pytestmark = pytest.mark.l3socket


@pytest.mark.parametrize("scenario", ["solo", "parent-child", "timeouts", "malicious"])
def testRealDriverScenariosOnLocalChain(tmp_path, monkeypatch, scenario):
    with localNode(tmp_path / "node") as rpc:
        rig = Layer3Rig(rpc)
        accounts = {name: Account.from_key(key) for name, key in rig.keys.items()}
        actors = dict(rig.actors)
        # A contract-owner identity is a real registry fact even in this isolated test.
        compiled = readJson(
            ".scratch/layer3/out/ControlledSignatureOwner.sol/ControlledSignatureOwner.json"
        )
        creation = compiled["bytecode"]["object"] + encode(["address"], [actors["owner"]]).hex()
        deployed = rpc.receipt(
            rpc.call(
                "eth_sendTransaction",
                {
                    "from": actors["requester"],
                    "data": creation,
                    "gas": hex(2_000_000),
                },
            )
        )
        assert int(deployed["status"], 16) == 1
        actors["contractOwner"] = deployed["contractAddress"]
        identities = {}
        names = [
            "solo",
            "parent",
            "child",
            "noShow",
            "executionTimeout",
            "validatorTimeout",
            "validationFailed",
            "malicious",
            "contractOwner",
        ]
        for index, name in enumerate(names):
            owner = (
                "contractOwner"
                if name == "contractOwner"
                else "childOwner"
                if name == "child"
                else "owner"
            )
            signer = "childSigner" if name == "child" else "signer"
            payout = "childPayout" if name == "child" else "payout"
            rig.external(
                rig.registry,
                "setIdentity",
                ["uint256", "address", "address"],
                [index, actors[owner], actors[payout]],
            )
            identities[name] = {
                "agentId": str(index),
                "owner": owner,
                "signer": signer,
                "payout": payout,
                "permitSigner": "owner" if name == "contractOwner" else owner,
                "profileDigest": "0x" + "00" * 32,
            }
        now = [rig.now()]

        def sleep(seconds):
            now[0] += seconds
            rpc.call("evm_setNextBlockTimestamp", now[0])
            rpc.call("evm_mine")

        monkeypatch.setattr(live, "time", SimpleNamespace(time=lambda: now[0], sleep=sleep))
        originalWait = live.waitFinalized
        monkeypatch.setattr(
            live,
            "waitFinalized",
            lambda rpc_, tx, deadline: originalWait(
                rpc_,
                tx,
                deadline,
                clock=lambda: now[0],
                sleep=sleep,
            ),
        )
        content = {"ref": {"uri": "ipfs://local-only-fixture", "digest": "0x" + "03" * 32}}
        config = {
            "windowSeconds": 120,
            "maxGas": 30_000_000,
            "maxGasPriceWei": str(10**12),
            "identities": identities,
            "artifacts": {
                name: content
                for name in ("input", "outputSchema", "validationPolicy", "result", "evidence")
            },
        }
        facts = {
            "chainId": "31337",
            "market": rig.market,
            "identityRegistry": rig.registry,
            "validator": actors["validator"],
            "identityVersion": "local-test-double",
            "marketCodeHash": live.digest(
                bytes.fromhex(rpc.call("eth_getCode", rig.market, "latest")[2:])
            ),
            "identityCodeHash": live.digest(
                bytes.fromhex(rpc.call("eth_getCode", rig.registry, "latest")[2:])
            ),
        }
        evidence = {
            "identityVerification": {
                "identityRegistry": rig.registry,
                "identityVersion": "local-test-double",
                "validUntil": str(now[0] + 100000),
                "proxyKind": "none",
                "codeObservations": [],
                "storageObservations": [],
                "probes": {
                    name: True
                    for name in ("transferClearsWallet", "restoredWalletControl", "contractOwner")
                },
            }
        }

        class FinalizedTestRpc:
            # Anvil's default finalized tag trails its automined head. This explicit
            # test transport supplies a finalized boundary; production never does so.
            def call(self, method, *params):
                if method == "eth_getBlockByNumber" and params[0] == "finalized":
                    params = ("latest", *params[1:])
                return rpc.call(method, *params)

        run = live.TestnetRun(
            FinalizedTestRpc(),
            {"manifestFacts": facts},
            config,
            accounts,
            actors,
            evidence,
            tmp_path / "scenario.json",
        )
        # This directly exercises funded scenarios, and deliberately does not claim
        # the synthetic chain/registry could pass the production qualification step.
        run.runScenario(scenario)
        assert run.journal["status"] == "passed"
        assert all(entry["status"] == "finalized" for entry in run.journal["transactions"])
        assert (
            sum(int(receipt["budgetAtoms"]) for receipt in run.receipts)
            == {
                "solo": 100,
                "parent-child": 260,
                "timeouts": 700,
                "malicious": 200,
            }[scenario]
        )
        assert run.journal["accounting"]["escrowAtoms"] == "0"
        assert run.journal["accounting"]["creditAtoms"] == "0"

# Independent local AgentLance example

This Node.js/ethers process implements one deterministic integer-copy template using the frozen protocol ABI, read ABI, schema/catalog and A2A profile. It never imports or invokes Python/reference-agent execution code. It discovers finalized TaskCreated events, applies its own template/budget/deadline policy, submits an owner-authorized bid, independently observes/accepts an award and publishes/commits its artifact only after finalized RUNNING.

Run `make demo-l8` for a configured, funded local example and real validation/payment/withdrawal evidence. `make check-l8` also runs all 24 unchanged external A2A assertions against it. Use the existing locked Node/ethers installation; no package install in this directory is needed.

```sh
node examples/external-agent/main.mjs /absolute/path/to/external-config.json
node --test examples/external-agent/test_wire.mjs
```

The demo generates a complete configuration under `.scratch/layer8/demo-*/independent/external-config.json`. It names a loopback chain-31337 manifest/RPC/genesis, AgentRef, verified payout address, encrypted owner keystore and password environment variable, HTTPS origin/certificate/key, card/registration files, journal and invocation-log paths, gas limits, fixed atomic bid and explicit fixture origins. Set `observeOnly: false` for autonomous participation. `observeOnly: true` is only for quiescent black-box award fixtures. Keep keys, certificates, journals and generated configurations ignored. The test harness uses clearly labeled encrypted fixture keys; live credential parameters are never inferred from them.

This example deliberately accepts only LOCAL_FIXTURE configuration and explicitly listed loopback HTTPS origins with certificate verification. It cannot serve as a qualified live runtime. It supports the existing L2 integer-copy schema/policy, a single EOA owner/execution signer and a separately configured current verified payout wallet. It does not delegate, forecast cost, choose a market winner, allocate roots or issue verdicts.

The JSON journal is atomically renamed and fsynced, locked to one process and bound to deployment/genesis/identity/key/card facts. Signed bytes and transaction hashes are saved before broadcast. Unknown transactions retain the same nonce and bytes; attempts are capped at three with a two-second interval. A finalized receipt is bound to the transaction and expected command event. Finalized contradictions halt participation. The journal is capped at 8 MiB/1,000 tasks, and discovery scans at most 128 blocks/2,000 logs per tick; up to eight retained tasks are considered per tick. Reaching capacity fails visibly; there is no silent eviction of recovery evidence.

One durable execution claim precedes the deterministic action. Restart after a claim without saved output marks it interrupted and never repeats the invocation. Completed artifacts, A2A task/context IDs and message correlations survive restart. Hints only admit verified correlations; they do not invoke an executor. Polling uses standard HTTP+JSON SendMessage/GetTask and exact extension metadata. Card retrieval and A2A COMPLETED do not establish canonical success/payment.

The test controller in `tests/layer8_controller.py` can reset its isolated local EVM and prepare awarded/submitted wire-test fixtures. It is not imported by the agent, not a public endpoint and not used to tell the autonomous demo which task won. Shared test ownership, local clocks and synthetic identity registration are disclosed; independent code/processes do not prove independent operators or Sybil resistance.

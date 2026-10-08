// Independent, deterministic local interoperability example. No Python runtime imports.
import fs from "node:fs";
import path from "node:path";
import https from "node:https";
import { Wallet, keccak256, randomBytes, hexlify } from "ethers";
import {
  ensure,
  equal,
  strictJson,
  commandData,
  readAbi,
  writeAbi,
  event,
  taskView,
} from "./wire.mjs";

const URN = "urn:agentlance:a2a:1";
const config = JSON.parse(fs.readFileSync(process.argv[2]));
const facts = JSON.parse(fs.readFileSync(config.manifestPath));
const rpcUrl = new URL(config.rpcUrl),
  origin = new URL(config.origin);
ensure(
  facts.chainId === "31337" &&
    ["localhost", "127.0.0.1"].includes(rpcUrl.hostname),
  "LOCAL_FIXTURE_ONLY",
);
ensure(
  origin.protocol === "https:" &&
    ["localhost", "127.0.0.1"].includes(origin.hostname),
  "LOCAL_TLS_REQUIRED",
);
ensure(
  config.networkScope === "LOCAL_FIXTURE" &&
    config.maxGas >= 21000 &&
    config.maxGas <= 5000000 &&
    BigInt(config.maxGasPriceWei) > 0n,
  "CONFIG",
);
const password = process.env[config.keystore.passwordEnv];
ensure(password, "KEYSTORE_PASSWORD");
const wallet = await Wallet.fromEncryptedJson(
  fs.readFileSync(config.keystore.path, "utf8"),
  password,
);
const signer = wallet.address.toLowerCase();
const agentRef = config.agentRef;
ensure(
  agentRef.chainId === facts.chainId &&
    agentRef.identityRegistry === facts.identityRegistry,
  "AGENT_NAMESPACE",
);
const card = fs.readFileSync(config.cardPath),
  cardDigest = keccak256(card);
const parsedCard = JSON.parse(card);
ensure(
  parsedCard.supportedInterfaces[0].url === config.origin + "/a2a",
  "CARD_ORIGIN",
);
const template = JSON.parse(
  fs.readFileSync(
    new URL("../../specs/fixtures/layer-2/cases.json", import.meta.url),
  ),
).byteHashes;
const ca = fs.readFileSync(config.certificate);
const directory = path.dirname(config.journal);
fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
const lock = config.journal + ".lock";
if (fs.existsSync(lock)) {
  const pid = Number(fs.readFileSync(lock, "utf8"));
  let alive = true;
  try {
    process.kill(pid, 0);
  } catch (error) {
    if (error.code === "ESRCH") alive = false;
    else throw error;
  }
  ensure(!alive, "JOURNAL_IN_USE");
  fs.unlinkSync(lock);
}
fs.writeFileSync(lock, String(process.pid), { flag: "wx", mode: 0o600 });
const binding = {
  facts,
  genesisHash: config.genesisHash,
  agentRef,
  signer,
  payout: config.payout,
  cardDigest,
};
let state = {
  version: 1,
  binding,
  checkpoint: null,
  tasks: {},
  operations: {},
  jobs: {},
  messages: {},
  halt: null,
};
if (fs.existsSync(config.journal)) {
  ensure(fs.statSync(config.journal).size <= 8 * 1048576, "JOURNAL_LIMIT");
  state = JSON.parse(fs.readFileSync(config.journal));
  ensure(equal(state.binding, binding), "JOURNAL_BINDING");
}
function save() {
  const raw = JSON.stringify(state);
  ensure(Buffer.byteLength(raw) <= 8 * 1048576, "JOURNAL_LIMIT");
  const temp = config.journal + ".tmp";
  const fd = fs.openSync(temp, "w", 0o600);
  try {
    fs.writeFileSync(fd, raw);
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  fs.renameSync(temp, config.journal);
  const parent = fs.openSync(directory, "r");
  try {
    fs.fsyncSync(parent);
  } finally {
    fs.closeSync(parent);
  }
}
for (const job of Object.values(state.jobs))
  if (job.phase === "STARTED") job.phase = "INTERRUPTED";
save();
let sequence = 0,
  stopping = false,
  serial = Promise.resolve();
function exclusive(action) {
  const result = serial.then(action);
  serial = result.catch(() => {});
  return result;
}
async function rpc(method, ...params) {
  const response = await fetch(config.rpcUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: ++sequence, method, params }),
    signal: AbortSignal.timeout(10000),
  });
  ensure(response.ok, "RPC_UNAVAILABLE");
  const raw = await response.text();
  ensure(raw.length <= 8 * 1048576, "RPC_LIMIT");
  const value = JSON.parse(raw);
  if (value.error) throw Error("RPC_UNAVAILABLE");
  ensure(Object.hasOwn(value, "result"), "RPC_RESPONSE");
  return value.result;
}
const hex = (n) => "0x" + BigInt(n).toString(16);
async function block(tag) {
  const b = await rpc("eth_getBlockByNumber", tag, false);
  ensure(b?.hash && b.number && b.timestamp, "BLOCK_UNAVAILABLE");
  return b;
}
async function canonical(b) {
  ensure((await block(b.number)).hash === b.hash, "FINALITY_CONFLICT");
}
async function qualified() {
  ensure(!state.halt, "JOURNAL_HALTED");
  const head = await block("finalized");
  ensure(
    BigInt(await rpc("eth_chainId")) === BigInt(facts.chainId) &&
      (await block("0x0")).hash === config.genesisHash,
    "CHAIN_BINDING",
  );
  ensure(
    (await block(hex(facts.deploymentBlock))).hash ===
      facts.deploymentBlockHash,
    "DEPLOYMENT_BINDING",
  );
  ensure(
    keccak256(await rpc("eth_getCode", facts.market, head.number)) ===
      facts.marketCodeHash,
    "CODE_BINDING",
  );
  ensure(
    keccak256(await rpc("eth_getCode", facts.identityRegistry, head.number)) ===
      facts.identityCodeHash,
    "IDENTITY_CODE_BINDING",
  );
  const age = Date.now() / 1000 - Number(BigInt(head.timestamp));
  ensure(
    age <= config.maxFinalizedAgeSeconds &&
      age >= -config.maxFinalizedAgeSeconds,
    "STALE_FINALITY",
  );
  const policy = readAbi.decodeFunctionResult(
    "readPolicy",
    await rpc(
      "eth_call",
      { to: facts.market, data: readAbi.encodeFunctionData("readPolicy", []) },
      head.number,
    ),
  );
  ensure(
    policy[0] === BigInt(facts.chainId) &&
      policy[1].toLowerCase() === facts.market &&
      policy[2].toLowerCase() === facts.identityRegistry &&
      policy[3].toLowerCase() === facts.validator &&
      policy[4] === 1n,
    "POLICY_BINDING",
  );
  if (state.checkpoint) await canonical(state.checkpoint);
  return head;
}
async function readTask(ref, head) {
  ensure(
    ref.chainId === facts.chainId && ref.market === facts.market,
    "TASK_NAMESPACE",
  );
  const raw = await rpc(
    "eth_call",
    {
      to: facts.market,
      data: readAbi.encodeFunctionData("readTask", [
        [ref.chainId, ref.market, ref.taskId],
      ]),
    },
    head.number,
  );
  const view = taskView(raw);
  ensure(equal(view.task.taskRef, ref), "TASK_BINDING");
  await canonical(head);
  return view;
}
function fetchContent(ref, maximum) {
  return new Promise((resolve, reject) => {
    const target = new URL(ref.uri);
    // This intentionally local-only example trusts only explicitly configured TLS origins.
    ensure(
      config.fixtureOrigins.includes(target.origin) &&
        target.protocol === "https:" &&
        ["localhost", "127.0.0.1"].includes(target.hostname),
      "CONTENT_ORIGIN",
    );
    const req = https.get(
      target,
      { ca, timeout: 10000, headers: { "Accept-Encoding": "identity" } },
      (response) => {
        if (
          response.statusCode !== 200 ||
          (response.headers["content-encoding"] &&
            response.headers["content-encoding"] !== "identity")
        ) {
          response.resume();
          reject(Error("CONTENT_UNAVAILABLE"));
          return;
        }
        let size = 0;
        const chunks = [];
        response.on("data", (chunk) => {
          size += chunk.length;
          if (size > maximum) {
            response.destroy(Error("CONTENT_LIMIT"));
            return;
          }
          chunks.push(chunk);
        });
        response.on("error", reject);
        response.on("end", () => {
          const raw = Buffer.concat(chunks);
          try {
            ensure(keccak256(raw) === ref.digest, "CONTENT_DIGEST");
            resolve(raw);
          } catch (error) {
            reject(error);
          }
        });
      },
    );
    req.on("error", reject);
    req.on("timeout", () => req.destroy(Error("CONTENT_TIMEOUT")));
  });
}
function supported(task) {
  const t = task.terms;
  return (
    t.taskFamily === "structured-output-v1" &&
    t.outputSchema.digest === template["output-schema.json"] &&
    t.validationPolicy.digest === template["policy.json"] &&
    t.validator === facts.validator &&
    t.validator !== signer &&
    BigInt(t.budgetAtoms) >= BigInt(config.bidAtoms) &&
    t.delegation.maxChildren === 0
  );
}
async function recover(op, head) {
  if (op.receipt) {
    ensure(
      (await block(op.receipt.blockNumber)).hash === op.receipt.blockHash,
      "FINALITY_CONFLICT",
    );
    return op.status;
  }
  ensure(keccak256(op.raw) === op.hash, "INTENT_BINDING");
  const receipt = await rpc("eth_getTransactionReceipt", op.hash);
  if (receipt && BigInt(receipt.blockNumber) <= BigInt(head.number)) {
    ensure(
      (await block(receipt.blockNumber)).hash === receipt.blockHash,
      "FINALITY_CONFLICT",
    );
    const tx = await rpc("eth_getTransactionByHash", op.hash);
    ensure(
      tx?.hash === op.hash &&
        tx.from.toLowerCase() === signer &&
        tx.to.toLowerCase() === facts.market &&
        tx.input === op.data &&
        BigInt(tx.nonce) === BigInt(op.nonce) &&
        BigInt(tx.value) === 0n &&
        tx.blockHash === receipt.blockHash,
      "TRANSACTION_BINDING",
    );
    ensure(
      receipt.transactionHash === op.hash &&
        ["0x0", "0x1"].includes(receipt.status),
      "RECEIPT_BINDING",
    );
    if (receipt.status === "0x1") {
      const events = receipt.logs.filter(
        (l) => l.address.toLowerCase() === facts.market,
      );
      ensure(
        events.length === 1 &&
          events[0].blockHash === receipt.blockHash &&
          events[0].transactionHash === op.hash,
        "LOG_BINDING",
      );
      const decoded = event(events[0]),
        payload = decoded.payload;
      ensure(
        decoded.name ===
          {
            submitBid: "BidAccepted",
            acceptAward: "AwardAccepted",
            submitResult: "ResultSubmitted",
          }[op.name],
        "COMMAND_EVENT",
      );
      ensure(
        op.name === "submitBid"
          ? equal(payload.bid.offer, op.input.offer)
          : equal(
              (payload.result || payload).executionRef,
              op.input.executionRef,
            ),
        "RESULT_BINDING",
      );
      if (op.name === "submitResult")
        ensure(
          equal(payload.result.artifact, op.input.artifact),
          "ARTIFACT_BINDING",
        );
      if (op.name === "acceptAward")
        ensure(
          payload.ownWorkReserveAtoms === op.input.ownWorkReserveAtoms,
          "RESERVE_BINDING",
        );
    }
    op.receipt = receipt;
    op.status = receipt.status === "0x1" ? "APPLIED" : "REVERTED";
    save();
    return op.status;
  }
  if (await rpc("eth_getTransactionByHash", op.hash)) return "PENDING";
  ensure(
    BigInt(await rpc("eth_getTransactionCount", signer, "latest")) <=
      BigInt(op.nonce),
    "NONCE_CONFLICT",
  );
  if (
    BigInt(head.timestamp) < BigInt(op.deadline) &&
    op.attempts < 3 &&
    Date.now() - op.lastAttempt >= 2000
  ) {
    op.attempts++;
    op.lastAttempt = Date.now();
    save();
    ensure(
      (await rpc("eth_sendRawTransaction", op.raw)).toLowerCase() === op.hash,
      "SEND_HASH",
    );
  }
  return "UNKNOWN";
}
async function send(name, input, key, deadline, head) {
  let op = state.operations[key];
  if (op) {
    ensure(op.name === name && equal(op.input, input), "INTENT_CONFLICT");
    return recover(op, head);
  }
  for (const pending of Object.values(state.operations)) {
    if (!pending.receipt) {
      await recover(pending, head);
      if (!pending.receipt) return "PENDING";
    }
  }
  ensure(BigInt(head.timestamp) < BigInt(deadline), "ACTION_EXPIRED");
  const data = commandData(name, input),
    transaction = { from: signer, to: facts.market, data, value: "0x0" };
  await rpc("eth_call", transaction, head.number);
  const gas =
    (BigInt(await rpc("eth_estimateGas", transaction)) * 12n + 9n) / 10n;
  const gasPrice = BigInt(await rpc("eth_gasPrice"));
  ensure(
    gas <= BigInt(config.maxGas) && gasPrice <= BigInt(config.maxGasPriceWei),
    "GAS_CAP",
  );
  const nonce = BigInt(await rpc("eth_getTransactionCount", signer, "latest"));
  ensure(
    nonce === BigInt(await rpc("eth_getTransactionCount", signer, "pending")),
    "PENDING_NONCE",
  );
  const completed = Object.values(state.operations).filter((x) => x.receipt);
  ensure(
    !completed.length ||
      nonce ===
        completed.reduce(
          (n, x) => (BigInt(x.nonce) > n ? BigInt(x.nonce) : n),
          0n,
        ) +
          1n,
    "EXTERNAL_NONCE",
  );
  ensure(
    BigInt(await rpc("eth_getBalance", signer, "pending")) >= gas * gasPrice,
    "GAS_BALANCE",
  );
  await canonical(head);
  const raw = await wallet.signTransaction({
    to: facts.market,
    data,
    value: 0,
    nonce: Number(nonce),
    gasLimit: gas,
    gasPrice,
    chainId: BigInt(facts.chainId),
    type: 0,
  });
  op = {
    name,
    input,
    data,
    nonce: nonce.toString(),
    raw,
    hash: keccak256(raw),
    deadline,
    attempts: 0,
    lastAttempt: 0,
    receipt: null,
    status: null,
  };
  state.operations[key] = op;
  save();
  return recover(op, head);
}
function extension(view) {
  return {
    schemaVersion: 1,
    profileVersion: 1,
    executionRef: { taskRef: view.task.taskRef, awardId: 1 },
    agentRef,
    inputDigest: view.task.terms.input.digest,
    validationPolicyDigest: view.task.terms.validationPolicy.digest,
    result: null,
  };
}
function admit(view, now) {
  ensure(
    view.winningBid &&
      equal(view.winningBid.offer.agentRef, agentRef) &&
      view.winningBid.offer.executionSigner === signer &&
      view.winningBid.offer.profileDigest === cardDigest,
    "AWARD_BINDING",
  );
  const key = view.task.taskRef.taskId,
    expected = extension(view);
  let job = state.jobs[key];
  if (job) {
    ensure(equal(job.extension, expected), "CORRELATION_CONFLICT");
    updateExpiry(job, view, now);
    return job;
  }
  ensure(Object.keys(state.jobs).length < 1000, "JOB_LIMIT");
  job = {
    extension: expected,
    id: hexlify(randomBytes(16)),
    contextId: hexlify(randomBytes(16)),
    artifactId: hexlify(randomBytes(16)),
    phase: "WAIT",
    result: null,
  };
  state.jobs[key] = job;
  updateExpiry(job, view, now);
  save();
  return job;
}
function updateExpiry(job, view, now) {
  const cutoff =
    view.status === "AWARDED"
      ? view.task.terms.acceptBy
      : view.task.terms.resultBy;
  if (
    !job.result &&
    (view.status === "SETTLED" || BigInt(now) >= BigInt(cutoff))
  ) {
    job.phase = "EXPIRED";
    job.outcome = {
      reason: "PROFILE_EXPIRED",
      taskStatus: view.status,
      receipt: view.receipt,
    };
    save();
  }
}
function projection(job) {
  const ext = { ...job.extension, result: job.result };
  const stateName = job.result
    ? "COMPLETED"
    : ["INTERRUPTED", "EXPIRED"].includes(job.phase)
      ? "FAILED"
      : job.phase === "STARTED"
        ? "WORKING"
        : "INPUT_REQUIRED";
  const value = {
    id: job.id,
    contextId: job.contextId,
    status: { state: "TASK_STATE_" + stateName },
    metadata: { [URN]: ext },
  };
  if (job.outcome) value.metadata[URN + ":diagnostic"] = job.outcome;
  if (job.result)
    value.artifacts = [
      {
        artifactId: job.artifactId,
        extensions: [URN],
        metadata: { [URN]: ext },
        parts: [{ url: job.result.uri, mediaType: "application/json" }],
      },
    ];
  return value;
}
async function tick() {
  const head = await qualified();
  const first = state.checkpoint
    ? BigInt(state.checkpoint.number) + 1n
    : BigInt(facts.deploymentBlock);
  const end =
    first + 127n < BigInt(head.number) ? first + 127n : BigInt(head.number);
  if (first <= end) {
    const logs = await rpc("eth_getLogs", {
      address: facts.market,
      fromBlock: hex(first),
      toBlock: hex(end),
      topics: [writeAbi.getEvent("TaskCreated").topicHash],
    });
    ensure(logs.length <= 2000, "LOG_LIMIT");
    for (const log of logs) {
      ensure(
        !log.removed &&
          log.address.toLowerCase() === facts.market &&
          BigInt(log.blockNumber) >= first &&
          BigInt(log.blockNumber) <= end,
        "LOG_NAMESPACE",
      );
      const receipt = await rpc(
        "eth_getTransactionReceipt",
        log.transactionHash,
      );
      ensure(
        receipt?.status === "0x1" &&
          receipt.blockHash === log.blockHash &&
          (await block(log.blockNumber)).hash === log.blockHash &&
          receipt.logs.some((l) => equal(l, log)),
        "LOG_RECEIPT",
      );
      const task = event(log).payload.task;
      ensure(
        task.taskRef.chainId === facts.chainId &&
          task.taskRef.market === facts.market,
        "CREATION_NAMESPACE",
      );
      ensure(
        Object.keys(state.tasks).length < 1000 ||
          state.tasks[task.taskRef.taskId],
        "TASK_LIMIT",
      );
      state.tasks[task.taskRef.taskId] = task.taskRef;
    }
    await canonical(head);
    state.checkpoint = await block(hex(end));
    save();
  }
  for (const op of Object.values(state.operations))
    if (!op.receipt) await recover(op, head);
  if (config.observeOnly) return;
  const tasks = Object.values(state.tasks),
    offset = state.taskOffset || 0;
  const selected = tasks.slice(offset, offset + 8);
  state.taskOffset = offset + 8 >= tasks.length ? 0 : offset + 8;
  save();
  for (const ref of selected) {
    try {
      const view = await readTask(ref, head),
        t = view.task.terms;
      if (!supported(view.task)) continue;
      if (
        view.status === "OPEN" &&
        BigInt(head.timestamp) < BigInt(t.biddingClose)
      ) {
        await fetchContent(t.outputSchema, 65536);
        await fetchContent(t.validationPolicy, 65536);
        const raw = await fetchContent(t.input, 1048576),
          input = strictJson(raw);
        if (
          !input ||
          Object.keys(input).join() !== "value" ||
          !Number.isSafeInteger(input.value) ||
          Math.abs(input.value) > 1000
        )
          continue;
        await send(
          "submitBid",
          {
            offer: {
              taskRef: ref,
              agentRef,
              executionSigner: signer,
              payout: config.payout,
              bidAtoms: config.bidAtoms,
              profileDigest: cardDigest,
            },
            permit: null,
            signature: null,
          },
          "bid:" + ref.taskId,
          t.biddingClose,
          head,
        );
      } else if (
        view.winningBid &&
        equal(view.winningBid.offer.agentRef, agentRef)
      ) {
        const job = admit(view, head.timestamp);
        if (
          view.status === "AWARDED" &&
          BigInt(head.timestamp) < BigInt(t.acceptBy)
        )
          await send(
            "acceptAward",
            {
              executionRef: job.extension.executionRef,
              ownWorkReserveAtoms: "0",
            },
            "accept:" + ref.taskId,
            t.acceptBy,
            head,
          );
        if (
          view.status === "RUNNING" &&
          BigInt(head.timestamp) < BigInt(t.resultBy)
        ) {
          if (job.phase === "WAIT") {
            const input = strictJson(await fetchContent(t.input, 1048576));
            ensure(
              Object.keys(input).join() === "value" &&
                Number.isSafeInteger(input.value) &&
                Math.abs(input.value) <= 1000,
              "INPUT_TEMPLATE",
            );
            await canonical(head);
            job.phase = "STARTED";
            save();
            fs.appendFileSync(
              config.invocationLog,
              JSON.stringify(job.extension.executionRef) + "\n",
              { mode: 0o600 },
            );
            const raw = Buffer.from(
              JSON.stringify({ value: input.value }) + "\n",
            );
            job.result = {
              uri:
                config.origin +
                "/artifacts/" +
                keccak256(raw).slice(2) +
                ".json",
              digest: keccak256(raw),
            };
            job.bytes = raw.toString("base64");
            job.phase = "DONE";
            save();
          }
          if (job.result)
            await send(
              "submitResult",
              {
                executionRef: job.extension.executionRef,
                artifact: job.result,
              },
              "result:" + ref.taskId,
              t.resultBy,
              head,
            );
        }
      }
    } catch (error) {
      // One unavailable/unsupported task must not prevent observing other awards.
      if (
        /^(CONTENT_|JSON_|INPUT_TEMPLATE|RPC_UNAVAILABLE|GAS_)/.test(
          error.message,
        )
      ) {
        console.error(error.message);
      } else throw error;
    }
  }
}
async function hint(message) {
  ensure(
    message?.role === "ROLE_USER" &&
      typeof message.messageId === "string" &&
      message.messageId.length > 0 &&
      message.messageId.length <= 128 &&
      Array.isArray(message.parts) &&
      message.parts.length &&
      message.extensions?.includes(URN),
    "PROFILE",
  );
  const ext = message.metadata?.[URN];
  ensure(
    ext?.result === null && !(message.contextId && !message.taskId),
    "PROFILE_CONFLICT",
  );
  const head = await qualified(),
    view = await readTask(ext.executionRef.taskRef, head);
  ensure(equal(ext, extension(view)), "PROFILE_CONFLICT");
  const previous = state.jobs[view.task.taskRef.taskId];
  if (message.taskId)
    ensure(
      previous &&
        previous.id === message.taskId &&
        previous.contextId === message.contextId &&
        !previous.result &&
        !["INTERRUPTED", "EXPIRED"].includes(previous.phase),
      "PROFILE_CONFLICT",
    );
  if (state.messages[message.messageId])
    ensure(equal(state.messages[message.messageId], ext), "PROFILE_CONFLICT");
  const job = admit(view, head.timestamp);
  ensure(
    Object.keys(state.messages).length < 4000 ||
      state.messages[message.messageId],
    "MESSAGE_LIMIT",
  );
  state.messages[message.messageId] = ext;
  save();
  return { task: projection(job) };
}
const server = https.createServer(
  { cert: ca, key: fs.readFileSync(config.tlsKey) },
  async (req, res) => {
    function reply(status, value, type = "application/json") {
      res.writeHead(status, {
        "Content-Type": type,
        "A2A-Version": "1.0",
        "A2A-Extensions": URN,
      });
      res.end(Buffer.isBuffer(value) ? value : JSON.stringify(value));
    }
    try {
      if (req.method === "GET" && req.url === "/.well-known/agent-card.json")
        return reply(200, card);
      if (req.method === "GET" && req.url === "/registration.json")
        return reply(200, fs.readFileSync(config.registrationPath));
      if (req.method === "GET" && req.url.startsWith("/artifacts/")) {
        const job = Object.values(state.jobs).find(
          (j) => j.result?.uri === config.origin + req.url,
        );
        ensure(job, "NOT_FOUND");
        return reply(200, Buffer.from(job.bytes, "base64"));
      }
      ensure(
        req.headers["a2a-version"] === "1.0" &&
          req.headers["a2a-extensions"] === URN,
        "PROFILE_VERSION",
      );
      if (req.method === "GET" && req.url.startsWith("/a2a/tasks/")) {
        const url = new URL(req.url, config.origin);
        ensure(
          [...url.searchParams.keys()].every((k) => k === "historyLength") &&
            (!url.searchParams.has("historyLength") ||
              /^\d+$/.test(url.searchParams.get("historyLength"))),
          "POLL_PARAMETERS",
        );
        const job = Object.values(state.jobs).find(
          (j) => j.id === url.pathname.slice("/a2a/tasks/".length),
        );
        ensure(job, "NOT_FOUND");
        return reply(200, projection(job));
      }
      ensure(
        req.method === "POST" && req.url === "/a2a/message:send",
        "NOT_FOUND",
      );
      let size = 0;
      const chunks = [];
      for await (const chunk of req) {
        size += chunk.length;
        ensure(size <= 65536, "BODY_LIMIT");
        chunks.push(chunk);
      }
      const body = strictJson(Buffer.concat(chunks));
      const result = await exclusive(() => hint(body.message));
      reply(200, result);
    } catch (error) {
      reply(
        error.message === "NOT_FOUND"
          ? 404
          : error.message === "BODY_LIMIT"
            ? 413
            : 400,
        { error: { message: "PROFILE_REJECTED" } },
      );
    }
  },
);
server.requestTimeout = 10000;
server.headersTimeout = 10000;
await new Promise((resolve) =>
  server.listen(Number(origin.port), "127.0.0.1", resolve),
);
function cleanup() {
  try {
    fs.unlinkSync(lock);
  } catch {}
}
process.on("exit", cleanup);
for (const signal of ["SIGTERM", "SIGINT"])
  process.on(signal, () => {
    stopping = true;
    server.close();
  });
while (!stopping) {
  try {
    await exclusive(tick);
  } catch (error) {
    console.error(error.message);
    if (
      /CONFLICT|BINDING|LIMIT|ENCODING|DIRTY|NAMESPACE|EXTERNAL_NONCE/.test(
        error.message,
      )
    ) {
      state.halt = error.message;
      save();
      server.close();
      process.exitCode = 1;
      break;
    }
  }
  if (!stopping) await new Promise((resolve) => setTimeout(resolve, 2000));
}
server.close();

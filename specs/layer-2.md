# Layer 2 — external-agent compatibility

Status: implemented and acceptance gate passed on `layer-2-core`, based on merged main `e74c556`. Executed evidence and coverage limits are in §14. L0/L1 semantics remain unchanged; no Monad contracts or registry deployment are available.

This is the implementation plan and acceptance contract for **L2-01..L2-07** in [layers.md](../layers.md). The [protocol](protocol.md), [agent profile](agent-profile.md), [L0](layer-0.md), [L1](layer-1.md), [shared schema](schemas/protocol.schema.json), [signing definitions](signing/types.json), [signing rules](signing/README.md), [deployment policy](deployment-policy.md), existing fixtures and [stack](../tech_stack.md) remain authoritative. This document selects adapter/runtime behavior, not new economic semantics. The file ownership and sequence below describe the delivered implementation; executed evidence is recorded in §14.

## 1. Responsibilities and exclusions

An independently written agent must be able to resolve an ERC-8004 identity, publish a compatible card, participate through protocol ports, verify its awarded obligation and return an artifact over A2A. It need not import AgentLance Python modules, inherit a worker superclass, register with an AgentLance operator or use the reference worker's persistence implementation.

| Requirement | L2 responsibility | Acceptance groups (§11) |
|---|---|---|
| L2-01 | AgentRef → registry observation → registration bytes → Agent Card bytes → validated AgentProfile | R, H |
| L2-02 | Frozen extension, standard HTTP+JSON messages/tasks/artifacts, exact correlation | W, C |
| L2-03 | Task/bid/award/accept/child/result/settlement ports, including uncertain-write reconciliation | P, E |
| L2-04 | Owner/permit checks, verified wallet, execution signer and profile binding | S, I |
| L2-05 | Immutable admitted obligation across transfer, wallet clearing and endpoint updates | I, J |
| L2-06 | Durable correlation, concurrent duplicates, restart, forged/stale/out-of-order delivery | J, E |
| L2-07 | Deterministic reference worker and two-process HTTP demo | E |

Excluded: Solidity, contract deployment, live Monad/registry RPC, log discovery/indexing, chain reorg projection, Jev/cost policy, LLMs/tools/containers, decomposition or child-worker scheduling, real validators/verdict production, reputation export, UI, global routing and fleet management. Child publication is an exercised protocol port, not a delegation execution engine. A fixture timeout may exercise L1 settlement; L2 does not decide correctness or sign a verdict.

The demo has two service processes: a synthetic market/registry fixture host and one independently running reference agent. The fixture host invokes L1 transitions for simulated protocol state. The reference agent must not import `modules.market_core`. There is no alternate HTTP allocator and no local service that can override a deployed market.

## 2. Reuse and implementation choices

Reuse `modules/domain/records.py` for strict JSON, exact numeric/reference validation and the shared record definitions. Reuse L1 `CoreState`, `CorePolicy`, `CommandContext`, `IdentityObservation`, `SignatureObservation` and `applyCommand` **inside the fixture adapter only**. Reuse all relevant existing signing, replay and A2A fixtures unchanged. `conftest.py` provides useful synthetic record builders, but its assumed-valid signature observations must not be used as cryptographic verification in L2 integration tests.

New responsibilities are profile resolution, typed-data construction/verification, protocol ports, A2A adaptation, a durable execution journal and runnable fixture compositions. L2 introduces these modules; it reuses the existing domain/core rather than adding another market. Keep them within the existing single Python 3.12/uv project; use importable `agent_client`/`reference_agent` names, consistent with L1's `market_core` spelling.

| Choice | Decision and reason |
|---|---|
| A2A version | Wire version `1.0`, specification release [v1.0.0](https://a2a-protocol.org/v1.0.0/specification/); no silent 0.3 downgrade |
| SDK | Pin official [`a2a-sdk[http-server]==1.2.1`](https://github.com/a2aproject/a2a-python/releases/tag/v1.2.1); SDK package version is distinct from wire version. Lock its dependency graph in `uv.lock` |
| HTTP | FastAPI composition, Uvicorn, HTTPX client and httpcore. Direct httpcore use provides the connection-time DNS/IP guard while retaining TLS SNI; the locked graph is in uv.lock |
| Signatures | Promote the existing bounded `eth-account` dependency to runtime; declare `eth-utils` for `keccak` and `eth-keys` for its explicit `BadSignature` exception. Ethers remains the independent development verifier |
| Persistence | Standard-library SQLite, one database per agent instance, WAL, `synchronous=FULL`, foreign keys enabled; no ORM, Redis or SDK SQL extra |
| Execution | One small local byte-copy worker, one server process/worker, bounded background tasks; no broker or subprocess executor |
| Test TLS | Development-only `cryptography` to generate temporary localhost certificates. Real TLS verification stays enabled; no committed private keys |
| Tests | Existing pytest/Hypothesis/coverage tooling. Use `asyncio.run` at synchronous test boundaries; an async pytest plugin is unnecessary |

Load schemas/types/configuration explicitly at application composition. Domain/core modules retain their import boundaries. AgentLance functions use camelCase; SDK override names retain the spelling required by the SDK.

## 3. Architecture and authority

```text
Fixture host process                         Reference-agent process
┌──────────────────────────────┐             ┌──────────────────────────────┐
│ synthetic registry snapshots │ ← HTTPS →   │ registry/profile resolver    │
│ raw registration/input bytes │             │ immutable content cache      │
│ fixture command/read bridge  │ ← HTTPS →   │ protocol ports + local signer│
│ L1 applyCommand + state      │             │ SQLite journal/reconciliation│
│ SDK A2A client/demo driver   │ → A2A HTTP → │ SDK routes + profile handler │
└──────────────────────────────┘             │ deterministic worker         │
                                             │ card/artifact byte endpoints │
                                             └──────────────────────────────┘
```

The fixture bridge is explicitly test infrastructure. Its endpoint is configured by the operator, never learned from a message or Agent Card. A2A request text, URLs, metadata and HTTP credentials confer no transaction authority. The agent controls its configured execution signer. Bids require current owner authority independently of HTTP authentication.

Production replacement is at the registry/market ports: L3/L4 provide verified contract/RPC observations, native transaction submission and reconciliation. They must preserve the records and finality contract below. A future adapter cannot silently fall back to L1 when RPC is unavailable.

### 3.1 Internal observations and results

These are typed adapter results, not new versions of canonical protocol objects. Closed fixture JSON wrappers use the same field names. Canonical records embedded inside them are validated against `$defs`; never duplicate their fields in Pydantic/dataclass wire models.

| Internal value | Exact fields / meaning |
|---|---|
| `ReadStamp` | `source: FIXTURE\|MONAD`, `chainId:Uint256`, `blockNumber:Uint64`, `blockHash:Digest`, `blockTimestamp:Uint64`, `finality: PENDING\|FINALIZED`. MONAD is reserved for the later adapter. Fixture hashes/times are explicitly synthetic |
| `RegistrySnapshot` | `agentRef:AgentRef`, `owner:Address`, `verifiedWallet:Address\|null`, `registrationUri:Uri`, `ownerHasCode:bool`, `stamp:ReadStamp`. Nonexistent identity is an error, not a zero owner |
| `ContractSignatureResult` | `outcome: RETURNED\|REVERTED\|OUT_OF_GAS`, `returnData:string\|null`. RETURNED requires lowercase even-length `0x` hex of the raw ABI bytes; other outcomes require null. Malformed/oversized return data fails verification |
| `ResolvedProfile` | `profile:AgentProfile`, `registry:RegistrySnapshot`, `registrationBytes:bytes`, `cardBytes:bytes`, `selectedInterfaceIndex:int`. Local-only byte bundle; not serialized as another protocol object |
| `TaskView` | `task:TaskSpec`, `status` from catalog, `allocation:Allocation\|null`, `winningBid:Bid\|null`, `ownWorkReserveAtoms:Uint96`, `childrenCreated:int[0,4]`, `activeChildren:int[0,4]`, `reservedChildBudgets:Uint96`, `committedChildPayouts:Uint96`, `result:ResultCommitment\|null`, `receipt:SettlementReceipt\|null`, `stamp:ReadStamp` |
| `Operation` | `operationId:string[1,128]`, `state: PENDING\|APPLIED\|REJECTED\|UNKNOWN`, `error:string\|null`, `events:Event[]`, `stamp:ReadStamp\|null`. APPLIED requires stamp; REJECTED carries a catalog code; other states have null error and no events |

All fields are required, including nulls. Integer aliases use the existing canonical decimal-string encodings. `ReadStamp` is not `EventEnvelope`: the fixture must not manufacture real chain receipt provenance. `TaskView` consistency includes references matching, winningBid matching allocation, SETTLED iff receipt exists, and result/receipt bindings. Inconsistent views fail closed as adapter faults.

`APPLIED` does not by itself mean finalized. A write can be applied in a pending block; execution still waits for a finalized matching read. `UNKNOWN` means the adapter cannot yet establish whether a write happened, never permission to repeat it with a new key.

Use a small internal `AdapterError(kind, detail)` with kinds `UNAVAILABLE`, `NOT_FOUND`, `INVALID_DATA`, `UNSUPPORTED`, `CONFLICT`, `FINALITY_CONFLICT`. These are operational errors, not additions to `catalog.json` or terminal task outcomes. Never turn a timeout into an empty task, zero wallet/cost, failed verdict or refund.

### 3.2 Required ports

Methods are async where they perform I/O. The configured market domain and signer capability are constructor inputs, not caller addresses accepted from an A2A payload. Public methods take canonical mappings; outputs are defensive copies.

| Port / method | Input → output | Rule |
|---|---|---|
| Registry `readIdentity` | `AgentRef` → `RegistrySnapshot` | One consistent finalized snapshot for owner/wallet/URI/code; registry lookup failure stays explicit |
| Registry `checkContractSignature` | `owner, digest, signature, stamp, gasLimit=50000` → `ContractSignatureResult` | Only for ERC-1271; read from the same admission snapshot; unavailable transport is an AdapterError, never successful magic |
| Content `fetchBytes` | `uri, maximumBytes, expectedDigest\|null` → exact bytes | Enforce §4.2; null digest only for first-time mutable registration/card discovery |
| Content `publishResult` | `ExecutionRef, bytes` → `ContentRef` | Persist immutable bytes locally and expose at configured artifact origin; same execution/content idempotent, different content conflicts |
| Market `readTask` | `TaskRef` → `TaskView` | Read at adapter's latest available finalized boundary; unavailable finality returns UNAVAILABLE |
| Market `readBid` | `TaskRef, AgentRef` → `{bid:Bid\|null, stamp:ReadStamp}` | Needed to reconcile an uncertain bid; no allocation calculation |
| Market `submitSignedBid` | canonical `submitBid` Command, `operationId` → `Operation` | Submit unchanged offer/permit/signature; direct owner path uses both nullable fields as null |
| Market `observeAward` | `ExecutionRef` → `TaskView` | Point observation, not discovery subscription; may return PENDING to distinguish waiting from forged OPEN state |
| Market `acceptAward` | canonical `acceptAward` Command, `operationId` → `Operation` | Configured transaction signer must equal frozen executionSigner |
| Market `publishChild` | canonical `createChildTask` Command, `valueAtoms:Uint96`, `operationId` → `Operation` | Fresh funding from signer, exactly child's budget; no parent escrow advance |
| Market `commitResult` | canonical `submitResult` Command, `operationId` → `Operation` | No caller-chosen submittedAt; immutable artifact already available and hash checked |
| Market `readSettlement` | `TaskRef` → `{receipt:SettlementReceipt\|null, stamp:ReadStamp}` | Null means unsettled, not failure |
| Market `readOperation` | `operationId` → `Operation` | Reconcile submission uncertainty before any resend |

Thin wrappers may share one private submit implementation; do not create an abstract repository/service class for each method. `operationId` is a client correlation token, not an EIP-712 nonce or protocol replay key. Persist the exact command/value/signer alongside it before submission. Reusing an ID for different data is CONFLICT. IDs are opaque UUID strings generated once per attempted command and retained on restart.

The client does not auto-bid on public tasks. The reference composition receives one explicit fixture TaskRef and an explicitly configured bid amount; it checks support locally and submits that offer. Price prediction/ABSTAIN economics and continuous discovery belong to L4/L5. Reading an award never computes or selects a winner.

### 3.3 Fixture bridge while L3 is absent

`FixtureMarket` holds one L1 `CoreState`, registry snapshots, a controlled clock, synthetic block observations and an operation journal. Serialize commands through a lock. For each command, derive caller from the configured fixture credential, derive time/value/registry/signature facts on the host, validate `Command`, call `applyCommand`, and commit only its returned state/events. Never accept `valid:true`, a caller, p/score, owner, time, finality or a winning bid as trusted request fields.

The HTTPS test API is deliberately named `/fixture/*` and is **not A2A, a deployed protocol API or an alternate production market**:

| Route | Closed request / response |
|---|---|
| `POST /fixture/read` | `{method,args}`; method is readIdentity/checkContractSignature/readTask/readBid/observeAward/readSettlement, args are the named inputs in §3.2; return the corresponding wrapper |
| `POST /fixture/commands` | `{operationId,command,valueAtoms}`; canonical command, Uint96 value string; return Operation |
| `GET /fixture/operations/{operationId}` | Return Operation scoped to authenticated fixture actor |
| `GET /fixture/content/{name}` | Exact immutable registration/input/schema/policy bytes from the loaded fixture bundle |

Reads use the configured fixture session credential. Writes use a separate opaque fixture credential mapped server-side to an actor and allowed command set; the worker's credential allows submitBid/acceptAward/createChildTask/submitResult only. Missing/wrong credentials give HTTP 401/403 with no call to L1. Requester/setup/clock/finality controls remain in the fixture driver, not agent-accessible routes. Tokens are random per demo, passed through local files/environment, never logged or added to canonical records. This mapping simulates native transaction authentication; it is not a new production signature scheme. EIP-712 bid permits are nevertheless verified with real cryptography.

Successful bridge reads and Operation responses use HTTP 200. An Operation with state=REJECTED is explicitly a protocol rejection, not a successful command. Adapter errors use `{kind,detail}` with HTTP 400 for INVALID_DATA/UNSUPPORTED, 404 for NOT_FOUND, 409 for CONFLICT/FINALITY_CONFLICT and 503 for UNAVAILABLE. HTTP authentication errors use `{kind:"UNAUTHENTICATED"\|"FORBIDDEN",detail}` and never enter a protocol port. Contract-call outcome fixtures are host-controlled, selected by exact owner/digest/signature/stamp; no client-provided mock return value is accepted.

Fixture operation journal key is `(credential actor,operationId)`. Repeated identical writes return their original result/events without another L1 call; conflicting bodies reject. Keep the host and its journal in memory for L2. A host restart starts a new explicitly named fixture session and requires a new agent database; it is not transparent recovery. Configure a random session ID at startup, send/verify it on every bridge request/response as `X-AgentLance-Fixture-Session`, and persist the expected ID in the agent database. A mismatch halts that adapter. Never reset the host invisibly while retaining an agent's old obligations. L2's mandatory restart demo restarts the **agent**, keeping the host running; durable chain recovery belongs to L4.

## 4. ERC-8004 identity and profile resolution

### 4.1 Deterministic resolution algorithm

1. Validate AgentRef; chain and identityRegistry must match configured fixture/deployment domain. Agent ID zero remains valid if minted. Read a consistent finalized `RegistrySnapshot`; reject missing token, zero owner, unsupported registry behavior or unexpected registry implementation revision. No registry scan is needed.
2. Fetch `registrationUri` once, bounded to 64 KiB. Accept only L0 HTTPS/IPFS URIs. Compute keccak256 over the received document bytes before parsing. ERC-8004 permits broader URI schemes, but this profile's frozen ContentRef cannot represent `data:`; report UNSUPPORTED, without adding a URI encoding to L0.
3. Interpret the ERC-8004 registration document as external discovery data: required `type` equals `https://eips.ethereum.org/EIPS/eip-8004#registration-v1`; require nonempty name/description and a services array. Require at least one matching `registrations` entry: `agentRegistry = eip155:<decimal chainId>:<lowercase registry>`, exact integer `agentId` equal to AgentRef. Parse that external integer exactly, never through binary64. If `active` is present it must be true. Unknown ERC discovery fields are inert; they are not unknown fields in AgentProfile.
4. Select the unique distinct card endpoint among services whose `name` is exactly `A2A` and whose version is absent, `1.0` or `1.0.0`. Identical duplicate entries collapse; different compatible endpoints are ambiguous and reject INVALID_DATA. Require an HTTPS card URL. A present incompatible service version cannot be silently ignored in favor of treating that same service as 1.0. Unrelated MCP/web services are not fetched.
5. Fetch that exact card endpoint, at most 64 KiB. Hash exact bytes, parse and validate with the pinned SDK's AgentCard model plus the frozen AgentLance checks. Do not append a well-known path to a registration endpoint that already names the card. The reference worker serves `/.well-known/agent-card.json`.
6. Select the first supported interface in card order with `protocolVersion="1.0"`, `protocolBinding="HTTP+JSON"` and an allowed HTTPS URL. Require normal card fields, unique skill IDs, JSON input/output modes and exactly one extension declaration for `urn:agentlance:a2a:1` with required=true. Additional standard card capabilities do not give authority. Reject unsupported required extensions; ignore optional unrecognized extensions. The reference card advertises no streaming, push, tenancy or authenticated extended card. A nonempty tenant is UNSUPPORTED by this minimal client, not misrouted to a default tenant.
7. Construct the existing AgentProfile: registration/card ContentRefs are the actual locators and observed digests; capabilities are unique skill IDs sorted lexicographically, at most 32 IDs of length 1..64; other fields are the fixed schema constants. Validate it through `validateRecord`. Return bytes, selected interface and registry provenance alongside the canonical wrapper.
8. Immediately before a **new bid**, re-read current owner/wallet/registration and fetch a fresh profile. Wallet must be nonzero and verified by the registry, not copied from registration JSON. Owner transfer/changed card between preparation and admission may reject the bid; the market rechecks its admission facts. No TTL or cached metadata authorizes a new bid.

Registration is a locator, not an owner signature over HTTP content. The owner directly submits or signs the exact chosen card digest in the bid. That is the obligation's profile authorization. A JWS card signature, `agentWallet` string in JSON, domain verification claim or approved ERC-721 operator never substitutes for it. The resolver stores provenance; it does not publish reputation or certify skill quality. [ERC-8004 registration and identity interface](https://eips.ethereum.org/EIPS/eip-8004).

### 4.2 Byte fetching and profile retention

Reuse L0 limits: 64 KiB registration/card/output schema, 16 KiB AgentLance extension, 1 MiB input/result. Transport request cap is 64 KiB, excluding separately fetched artifacts. Enforce limits while streaming even without Content-Length. Use identity content encoding; reject compressed bodies to avoid ambiguous digest/decompression handling. Digest is Ethereum keccak256 of exact body bytes, including whitespace/newlines; never JSON reserialization, SHA3-256, URL text or an SDK ETag.

HTTPS certificate/hostname verification is required. No credentials in URIs, fragments or whitespace. Production fetch policy denies loopback/private/link-local/multicast/unspecified destinations, checks every redirect, pins a validated DNS result for the connection while retaining TLS hostname verification, and does not trust a preflight DNS check followed by an uncontrolled second lookup. At most three redirects, ten seconds total per fetch attempt, no ambient proxy configuration (`trust_env=False`). IPFS is fetched through one configured HTTPS gateway with the same controls; no gateway selection from agent data. Card/interface redirects must never carry authorization to another origin. A2A POSTs do not follow redirects.

Only explicit `fixtureMode` permits the exact two configured localhost HTTPS origins and the temporary test CA. It does not disable URI validation, hostname checks, digest checks or permit arbitrary local addresses. No `http://` substitutions inside canonical records. Tests cover blocked redirects and DNS rebinding using an isolated transport; they do not contact private networks.

Retain registration/card bytes by digest and associate the pinned card with each admitted bid. When an award arrives, use `winningBid.offer.profileDigest`; resolve from retained bytes, verify hash, and select that card's endpoint. If old bytes/endpoint cannot be recovered, do not replace them with the current card: wait/fail locally without modifying the obligation. The agent retains its own admitted card versions too. A transfer may clear the wallet/change owner/endpoint for **new** bids; it cannot redirect an existing signer or payout.

## 5. Signing and identity binding

`buildBidPermitTypedData(permit, configuredDomain, types)` and async `verifyBidPermit(offer, permit, signature, registrySnapshot, configuredDomain, types, registryPort)` belong in `modules/agent_client/signing.py`. The verifier uses registryPort only for the ERC-1271 read. They load no files implicitly. Reuse the ordered definitions in `specs/signing/types.json`; no duplicated type declaration, nested refs, alternative primary type or extra fields. Flatten TaskRef/AgentRef exactly as L0 specifies. Reject namespace mismatches before encoding.

| Boundary | Required behavior |
|---|---|
| Agent bid preparation | Locally configured signer/payout/agent, resolved card digest and task domain match offer; fetch committed input/schema/policy bytes and check hashes before participating |
| Direct admission | Caller is current owner, both permit/signature null; operator approval and payout control alone are insufficient |
| Permitted admission | Offer fields equal permit fields; signed owner equals current owner; registry wallet equals payout; EIP-712 cryptographic verification succeeds |
| Code-free owner | 65-byte signature, low-s, v=27/28, recovered address equals owner; reject compact/personal-sign/malformed signatures |
| Owner with code | ERC-1271 only, exact digest/signature call with 50,000 gas; full ABI response decoding and magic value check; revert/short return/wrong magic/out-of-gas fail; no ECDSA fallback |
| Consumption | L1 fixture / future L3 owns nonce usage, expiry (`now < expiry`), biddingClose, one bid per identity, wallet/role checks and successful-only consumption |
| Existing award | Read admitted Bid; compare AgentRef, executionSigner, ownerAtBid, payout and profileDigest to the stored obligation. No current-owner or current-wallet revalidation |

Cryptographic verification returns a bound observation, not general `authorized=true`. Fixture host builds `SignatureObservation` from independently verified exact permit/domain/signature and supplies it with independently resolved `IdentityObservation` to L1. Client-side preflight cannot grant admission; racing transfer/expiry remains a market rejection. Code classification must come from the registry adapter's chain context, never a claimant flag. Contract execution signers are representable by the ports; the reference runtime controls an EOA only and refuses an unsupported signer rather than impersonating it.

L2 implements EOA permit cryptography and deterministic ERC-1271 boundary fixtures. It does **not** claim an in-memory result proves a live wallet or registry implementation. Actual staticcall/code/proxy/transfer behavior is verified at L3 deployment. Use the existing smart-wallet replay fixtures for magic, revert and short response cases.

Nonce scope remains `(market, owner, BidPermit, nonce)`; the client journal is not a replacement. A consumed permit never admits again. Transfer invalidates an unconsumed old-owner permit; transfer back can re-enable it if unused/unexpired, as frozen in L0. An admitted obligation survives both transfers. Do not add ownership epochs or revoke accepted bids.

## 6. Actual A2A 1.0 HTTP+JSON behavior

### 6.1 SDK composition and wire contract

Mount official SDK `create_rest_routes(requestHandler, path_prefix="/a2a", enable_v0_3_compat=False)` into FastAPI. Implement the SDK `RequestHandler` hooks over the journal/coordinator below. Use the SDK for request dispatch, A2A types and standard errors. Do not use the default handler's independent in-memory task creation/execution path: ExecutionRef must be claimed before dispatching a worker, including when the caller omits taskId. A thin RequestHandler implementation is the necessary extension point; do not fork SDK code or create custom A2A methods. [Pinned SDK route factory](https://github.com/a2aproject/a2a-python/blob/v1.2.1/src/a2a/server/routes/rest_routes.py).

The client uses `ClientFactory` with `ClientConfig(streaming=False, polling=True, supported_protocol_bindings=["HTTP+JSON"], accepted_output_modes=["application/json"])` and a configured HTTPX client. Call SDK `send_message`/`get_task` with its request types; `send_message` is an async iterator, even without streaming. Pass both required service headers explicitly on every call. Do not use old JSON-RPC `message/send`/`tasks/get` bodies or 0.3 Pydantic examples. [Pinned client interface](https://github.com/a2aproject/a2a-python/blob/v1.2.1/src/a2a/client/client.py), [factory](https://github.com/a2aproject/a2a-python/blob/v1.2.1/src/a2a/client/client_factory.py).

| HTTP operation | Required behavior |
|---|---|
| `GET /.well-known/agent-card.json` | Return the exact immutable bytes whose digest is bid; validate those bytes with SDK types at startup. Serve raw bytes, not a per-request SDK reserialization |
| `POST /a2a/message:send` | A2A SendMessageRequest; return standard SendMessageResponse with Task for supported work. Nonblocking configuration uses `returnImmediately:true`; no SSE required |
| `GET /a2a/tasks/{id}` | Standard GetTask; return Task directly, including stable context/status/artifacts; unknown ID is TaskNotFoundError |
| `POST /a2a/tasks/{id}:cancel` | TaskNotFoundError for unknown task; TaskNotCancelableError for known tasks. Reference runtime offers no transport cancellation that could abandon an accepted obligation |
| Other SDK routes | Advertise unsupported optional features as false; standard UnsupportedOperationError for streaming/subscription/extended-card requests, PushNotificationNotSupportedError for push configuration. Do not expose an unscoped task list; ListTasks is UnsupportedOperationError in this minimal profile. No execution or economic effect |

Use `Content-Type: application/json`, `Accept: application/json`, `A2A-Version: 1.0`, `A2A-Extensions: urn:agentlance:a2a:1`. Missing version is not assumed to be 1.0; reject as unsupported. Parse extensions as header tokens, not substring matches. Required extension must also be in `message.extensions`. Return acknowledged extension header on successful profile responses. The pinned SDK serves successful responses as `application/a2a+json`; clients also accept `application/json`. Validate response bytes before protobuf conversion and require the acknowledged extension. Protocol rejections are never HTTP 200 fake successes. The actual SDK error envelope is `{"error":{"code":400,"status":"INVALID_ARGUMENT","message":"…","details":[{"@type":"type.googleapis.com/google.rpc.ErrorInfo","reason":"INVALID_PARAMS","domain":"a2a-protocol.org","metadata":{…}}]}}`, using the appropriate standard code/status/reason for each error. This corrects the design draft's unverified “problem response” wording; no L0 field or reason changes.

Honor the standard blocking flag: if returnImmediately is false/absent, wait for terminal/interrupted transport state, bounded by the HTTP attempt timeout. On timeout return an operational error while retaining the existing task; never start a replacement. GetTask honors historyLength, including zero, and rejects invalid values through SDK validation. Polling clients explicitly set returnImmediately=true.

Reference card has public unauthenticated A2A notification/polling and public fixture artifacts, no proprietary access token. Requests still pass canonical award checks. Client supports a card with no auth or one advertised HTTP bearer scheme, supplied credentials out of band for that exact origin; unsupported schemes report UNSUPPORTED and do not send. No tokens in card/metadata/URI and no inference that an HTTP bearer controls a wallet. An auth failure causes no execution, acceptance or refund.

### 6.2 Extension and serialization boundary

Every supported award message contains `metadata["urn:agentlance:a2a:1"]` validated as the existing **A2AExtension**, with exactly:

```text
schemaVersion, profileVersion, executionRef, agentRef,
inputDigest, validationPolicyDigest, result
```

No owner, price, payout, permit, finality, local run ID or command is added to that payload. Initial hints require result=null. `executionRef.taskRef` is the economic task; `awardId` is 1. A2A text is informational and never a prompt for the deterministic executor. The reference card advertises both application/json and text/plain input modes, allowing the text notice used by the frozen `a2a.json` example; output includes application/json. Additional parts cannot override committed input. Initial messages must have ROLE_USER; client-supplied status/artifacts or assistant-role messages cannot impersonate worker progress.

SDK v1 uses protobuf types. Validate raw request JSON and extract the extension **before** protobuf Struct conversion can normalize numbers. Keep canonical economic integers as strings. For outbound Struct metadata, render the three bounded integer constants as JSON integer tokens (not `1.0`), retain result:null, and preserve exactly the validated extension mapping. A small boundary serializer/interceptor may restore this mapping after SDK envelope conversion; it must not invent a second A2A model. Reject incoming float tokens, bool-as-int, unknown/missing extension fields and duplicate JSON keys before execution. Tests exercise SDK-to-SDK and raw-HTTP round trips, including large string IDs. Hash only independently retained artifact/card bytes, never the rendered envelope.

Standard A2A fields follow the SDK schema, rather than applying AgentLance's closed object rules to the whole third-party protocol. Unknown AgentLance fields always reject. Unrecognized optional standard extension data is never acted upon.

Diagnostic data lives beside the closed extension at `metadata["urn:agentlance:a2a:1:diagnostic"]`, not inside it. This local profile diagnostic has exactly `{reason, taskStatus, receipt}`: reason is one of the four existing PROFILE_* reasons or null; taskStatus is a canonical catalog state or null; receipt is the existing SettlementReceipt or null. Preserve the diagnostic's canonical numeric/null types through the same raw JSON boundary. Operational details go in ordinary status/error text. It is not another required card extension or an economic authority. When an error lacks a Task, serialize this object to strict JSON in SDK `ErrorInfo.metadata["urn:agentlance:a2a:1:diagnostic"]`. That SDK map requires string values; decode the string once to recover the same diagnostic. Task metadata retains the object directly.

### 6.3 Correlation and artifacts

Key all work by `(chainId, market, taskId, awardId)`, using validated tuple components, never concatenated unescaped text or messageId. One key has one existing A2ACorrelation. The server generates UUID task/context IDs and persists them atomically before execution; they satisfy the existing length bounds. First SendMessage omits taskId/contextId. Subsequent explicit IDs must match the stored correlation; arbitrary supplied IDs cannot create a task. Context-only initial requests are rejected for this profile to prevent reassignment of an obligation.

Same execution and same commitments with a new initial messageId returns the same Task and IDs. If taskId explicitly names a terminal Task, return standard UnsupportedOperationError and let the client use GetTask; an initial retry without IDs can retrieve the existing terminal task after a lost response. Reusing one messageId for another execution or conflicting extension rejects PROFILE_CONFLICT. Store message IDs only after binding validation; a forged hint must not reserve a real execution's slot.

Return one final Artifact with a receiver-generated stable artifactId, `extensions` containing the URN, and identical extension metadata with `result:ContentRef`. Its part is `{url:result.uri, mediaType:"application/json"}`. Task metadata and status message carry the same binding; SDK status values use `TASK_STATE_*`. For GetTask, artifacts are returned even with zero history. Fetch the artifact and verify exact bytes against ContentRef before result commitment. Artifact ID/context ID/URL alone never proves correctness.

Reference artifact route is `GET /artifacts/{digestWithout0x}.json`, with immutable response bytes. No caller-directed output URL or upload callback. Duplicate final content is a no-op; different URI or digest for an already committed record is PROFILE_CONFLICT, even if a changed URI serves equivalent bytes. Input/policy/agent binding never changes as status progresses.

## 7. Durable execution and failure behavior

### 7.1 Journal ownership

Use one SQLite database for correlations, operation intents, raw pinned content and A2A task projections. This is local run/recovery state, never economic state. No independent SDK TaskStore database. Generate SDK Task objects from the stored projection when serving reads.

| Table | Minimal contents / constraint |
|---|---|
| `executions` | ExecutionRef tuple primary key; unique taskId and contextId; validated initial extension; admitted Bid and pinned TaskSpec (null until finalized binding); last trusted ReadStamp; ownWorkReserveAtoms; phase; startClaimed boolean; result ContentRef nullable; standard A2A status; optional diagnostic. Bytes live in content, not a duplicate blob column. Persist canonical mappings as strict JSON, numeric key components as canonical TEXT to avoid SQLite int64 truncation |
| `messages` | messageId primary key; execution key; validated binding; foreign key to execution. A retry with different informational text but identical binding is harmless |
| `operations` | operationId primary key; execution key where applicable; canonical command, value, signer; last Operation; uniquely identify acceptance/result intent per execution. Child intents use distinct caller-created IDs and retain their returned TaskCreated ref |
| `content` | Digest primary key; exact bytes. The result route has fixed application/json media type. Verify on write/read; a digest collision/conflicting bytes is an integrity fault, not overwrite |
| `finalized`, `flags` | Observed finalized height/hash pairs and persistent market-halt flag; contradictory finalized facts survive restart |
| `settings` | One row binding schema instance to configured AgentRef, market domain and fixture session ID (null for a future real-chain adapter) |

Database schema version uses `PRAGMA user_version=1`; unknown version is a startup error, no automatic delete/recreate. Store configured agent, market domain and fixture session identity in a single settings row; startup mismatches fail closed. Configure one Uvicorn worker. A process lock on the database prevents two application instances from accidentally sharing it. Transactional unique keys and compare-and-set still guard concurrent HTTP requests within that instance. Bound async locks/maps by active requests; durable rows remain available for replay. L2 has no garbage collection that can erase an execution tombstone and permit rerun.

### 7.2 Start/accept/result algorithm

1. Validate HTTP/version/profile/refs/size/IDs before any side effects. Look up existing correlation and compare bindings. Pure GetTask never schedules a new run.
2. Observe award from configured market, ignoring caller claims of winner/finality. Check TaskRef, AWARDED/RUNNING state, awardId=1, winner AgentRef, frozen signer under local control, retained card digest, task input/policy digests and deadlines. A pending **matching** award may be retained as a non-running WAIT observation; an OPEN/no-award/wrong-agent view is PROFILE_AWARD_MISMATCH. Do not pin caller-provided terms or make a start claim from a pending observation.
3. On a finalized matching view, pin TaskSpec/Bid and create or promote the correlation under a transaction. Fetch/hash input/schema/policy; apply the worker's narrow support check (§9). Missing bytes, unavailable finality or absent signing credentials wait without spending. A valid unknown execution already SUBMITTED/SETTLED must never be executed to reconstruct a lost result.
4. If AWARDED and before acceptBy, persist acceptance intent with locally configured own-work reserve (reference worker: `"0"`). Submit through `acceptAward`, then reconcile and re-read. If RUNNING, verify the frozen obligation and stored reserve; no second acceptance. Require matching **FINALIZED RUNNING** before the next step and current observation time < resultBy.
5. Under a SQLite transaction, move READY → STARTED only if `startClaimed=false`; set it true and commit before invoking the worker. A2A becomes WORKING. Only the winning claimant calls the deterministic function; duplicate handlers return current status. No HTTP request handler may call it outside this path.
6. Persist output bytes, ContentRef and final artifact projection durably; expose the bytes before reporting COMPLETED. A2A completion records artifact availability. Separately persist a submitResult intent, check a fresh finalized RUNNING view/resultBy/activeChildren, and submit the immutable result. Read back exact ResultCommitment after finalization. Result commitment failure cannot cause re-execution or replacement of the artifact.
7. Read settlement separately on request. Until a canonical receipt exists, show no success/payment. L2 never calls a validator. Stop action retries at their protocol deadline; retain artifacts/correlation for later reads.

Clock used for economic decisions is the market observation's block timestamp, never a timestamp in a hint. A later live adapter must also detect stale/unavailable finalized reads per deployment policy; L2 cannot manufacture freshness from a local wall clock. HTTP waits/retries use local monotonic time: one background reconciliation tick per two seconds and ten seconds per attempt (the bounded test supervisor may poll readiness faster), bounded by the relevant deadline and a local demo timeout. No unbounded blocking SendMessage.

An existing WAIT/ACCEPT_PENDING/RESULT_PENDING row has one local bounded reconciliation loop, resumed at startup; a new hint is not required for finality progress. This loop observes only that already known obligation. It does not enumerate markets or discover tasks. Missing local credentials remain non-running until supplied by the operator, never extracted from a hint.

### 7.3 Recovery phases

| Local phase | A2A projection | Restart/retry action |
|---|---|---|
| WAIT | INPUT_REQUIRED (AUTH_REQUIRED only for missing local credentials) | Reobserve the configured market, validate bindings, fetch missing bytes; never start from old hint alone |
| ACCEPT_PENDING | SUBMITTED | Reconcile stored operation/read canonical state. RUNNING with same Bid/reserve is acceptance evidence; pending/unknown remains non-running |
| READY | SUBMITTED | Fresh finalized RUNNING check, then claim once |
| STARTED without durable result | WORKING while current process owns it; FAILED after process loss | Mark interrupted, retain startClaimed/tombstone; **do not invoke again** under this ExecutionRef |
| ARTIFACT_READY / RESULT_PENDING | COMPLETED | Return artifact; publish missing byte route/reconcile same submitResult intent; no worker call |
| RESULT_RECORDED | COMPLETED | GetTask returns stored artifact; settlement remains independent |
| STOPPED / INTERRUPTED | REJECTED / FAILED | Retain reason and correlation; no further work under this key |

This is at-most-once worker invocation with fail-closed recovery after an uncertain crash. A crash after the start claim but before invocation may lose work. L2 does not promise exactly-once execution for arbitrary external side effects. The normal demo invokes once; the crash tests prove it does not invoke twice. L6 may add provider idempotency/reconciliation without removing the immutable key.

Before retrying an uncertain command, use readOperation and canonical reads: matching admitted bid, accepted reserve or committed result can resolve it. A child creation must resolve its exact operation/transaction to its TaskCreated event; never infer success from a changed child count or resend with a fresh ID. L3 transaction hashes/nonces replace the fixture operation mapping; ambiguous live writes remain UNKNOWN until reconciled. A protocol rejection keeps its original code and never becomes an A2A success/payment.

### 7.4 Required failures and ordering

| Condition | Observable behavior; worker starts/economic writes |
|---|---|
| Missing/unsupported A2A version or mandatory extension | Standard version/extension error with PROFILE_VERSION; zero starts/writes |
| Malformed extension, unknown fields, bad JSON/size | Standard invalid-parameters/HTTP size error; zero starts/writes, no poisoned correlation |
| Wrong chain/market/task/award/agent, OPEN market posing as award | PROFILE_AWARD_MISMATCH in standard invalid-parameters response; zero starts/writes |
| Input/policy/agent/result differs from existing binding | PROFILE_CONFLICT; original row/artifact unchanged; zero additional starts/writes |
| Matching award not finalized, or finality unavailable | Non-running WAIT; zero acceptance/execution until trusted finality |
| Unaccepted AWARDED at now >= acceptBy | HTTP 400 InvalidParamsError with PROFILE_EXPIRED for a new hint; existing waiting task becomes REJECTED. No acceptance/start; protocol remains AWARDED until someone expires it |
| RUNNING at now >= resultBy | PROFILE_EXPIRED in HTTP 400 InvalidParamsError for an unknown run; do not regress an existing COMPLETED artifact. No new start/result write or invented timeout receipt |
| Canonically SUBMITTED/SETTLED | Existing run returned if known; otherwise HTTP 400 InvalidParamsError with PROFILE_EXPIRED and current state/receipt diagnostic. No new correlation, acceptance or start |
| Current identity transferred/wallet cleared after admission | Existing frozen signer may continue; new-owner credentials cannot hijack it; new bids use current registry facts |
| Profile endpoint changed after admission | Use retained card/version; new endpoint cannot silently receive old obligation |
| Duplicate message/concurrent hints/lost response | Same stored IDs/state/artifact; <=1 start claim and <=1 invocation |
| Older WORKING/INPUT_REQUIRED after terminal transport state | Ignore regression; preserve artifact and terminal transport state; re-read economics independently |
| Tentative observation disappears/reorgs before finality | A newer trusted finalized view with no matching award invalidates the pending observation. Stop waiting, retain correlation/audit entry, mark non-running rejection; no acceptance/start. Block graph/overlap recovery and any replacement-fork task reconciliation are L4 |
| New read contradicts previously finalized binding/hash at same height | FINALITY_CONFLICT; halt this market's new bids/starts, retain journal; no automatic rerun/export |
| A2A FAILED/REJECTED/CANCELED | No automatic protocol failure, refund, cancellation, reputation update or payment |
| Fetch/auth/storage/worker failure | Bounded operational error/wait or FAILED after claimed execution; never substitute fake output/success |

Check an existing binding for conflict before serving a duplicate. An invalid prebinding request does not create a terminal economic-key row. Pending observations are not admitted obligations: only the trusted provider view described above can invalidate them; arbitrary conflicting requests cannot do so. Retain their IDs/tombstone rather than automatically running a replacement under the same key. Once finalized, binding is immutable. Same-height hash conflicts are detected locally; implementing historical reorg scanning is explicitly outside L2.

## 8. File and module ownership

Create only these cohesive files as implementation reaches them; avoid empty packages and per-method modules. Colocated tests may combine related matrices instead of duplicating setup.

| Path | Owns |
|---|---|
| `modules/agent_client/ports.py` | Internal observation/result types and minimal Registry/Market/Content protocols from §3; no SDK/core imports |
| `modules/agent_client/signing.py` | Typed BidPermit construction, EOA verification, ERC-1271 response validation; consumes frozen types |
| `modules/agent_client/participant.py` | Bid preparation, award checks, acceptance/result reconciliation and coordinator; calls ports/journal/worker, no market math |
| `modules/adapters/registry/resolution.py` | Registration/card selection, SDK card validation, AgentProfile assembly and provenance |
| `modules/adapters/storage/content.py` | Bounded safe HTTPS/IPFS fetching, exact-byte cache/publication; configurable fixture-origin exception |
| `modules/adapters/storage/journal.py` | SQLite schema/transactions, durable correlation, messages, command intents, restart phases |
| `modules/adapters/a2a/profile.py` | SDK/strict-JSON conversion, frozen extension mapping, diagnostics, correlation checks |
| `modules/adapters/a2a/server.py` | SDK RequestHandler, official routes, standard status/error mapping; no worker starts bypassing coordinator |
| `modules/adapters/a2a/client.py` | SDK client setup, SendMessage/GetTask polling, result correlation and digest checks |
| `modules/adapters/fixtures.py` | In-memory registry snapshots, verified signature context, L1-backed market, fixture HTTP bridge client/server and deterministic clock; never production fallback |
| `apps/reference_agent/worker.py` | Deterministic narrow executor in §9; ordinary function with exact bytes in/out |
| `apps/reference_agent/main.py` | One agent's configuration, dependencies, HTTPS/card/artifact routes and startup recovery; runnable with `python -m` |
| `apps/fixture_market.py` | Fixture host composition/setup/driver; no independently implemented market logic |
| `modules/agent_client/test_*.py`, adjacent adapter `test_*.py`, `apps/reference_agent/test_worker.py` | Unit/property matrices for owned boundaries |
| `tests/conformance/test_agent_profile.py`, `target.py` | Black-box HTTP suite and optional external target setup; no reference-worker imports |
| `tests/layer2_support.py`, `tests/test_layer2_fixtures.py` | Isolated ASGI composition and reviewed-case/hash consumption |
| `tests/integration/test_layer2.py` | Two-process fixture flow, TLS, lost responses, restarts and durable-run assertions |
| `specs/fixtures/layer-2/` | Reviewed synthetic registration/card, exact input/schema/policy/result bytes and `cases.json` with expected outcomes; no secrets or deployment manifests |
| `scripts/demo_layer2.py` | Starts two service processes, drives executable acceptance, captures report, bounded cleanup via local stdin shutdown with kill fallback; optional subprocess coverage instrumentation, no business logic |
| `scripts/check_layer2.py`, `test_check_layer2.py` | L0/L1 gate plus L2 boundary/conformance/process tests, lint/format and requirement coverage check |
| `pyproject.toml`, `uv.lock`, `Makefile`, `README.md` | Locked runtime/dev dependencies, actual commands, implemented status after acceptance |

Do not add `modules/execution`, contract/RPC adapters, a service fleet or a new canonical schema for these wrappers. During implementation, the spec checker may validate the new fixture case grammar and embedded shared-schema records without rewriting existing goldens. A small L2 fixture schema is allowed only for the test-case wrapper; canonical objects remain references to the existing schema.

## 9. Minimal reference worker

Reference skill ID is `structured-output-v1`. Its advertised description states the exact narrow capability: copying an integer-valued JSON fixture unchanged. It does not claim to solve every task in that family.

`executeFixture(inputBytes) -> bytes` accepts strict JSON with exactly one field `value`, integer in [-1000,1000], rejects bool/float/extra keys, and returns the **same original bytes**. For the demo input and result are exactly UTF-8 `{"value":7}\n`. Own-work reserve is zero; bid is a configured fixture value, not a computed cost estimate.

Before accepting, the coordinator fetches and hashes committed input/schema/policy, validates their public shapes, and checks the task uses the reviewed fixture output schema and policy digests. Output shape is an object with only required integer value [-1000,1000], additionalProperties=false; policy uses the existing `equalsInput` predicate `/value` → `/value` and frozen evaluator limits. Checking support/digests is not running the policy evaluator or signing PASS. Arbitrary policy/schema combinations are not silently treated as supported. New variants require explicit worker support, not an LLM fallback.

The deterministic function reads no network/environment/clock and has no external effects. Instrument actual invocation in the reference test composition; instrumentation is local test evidence, not an A2A protocol field or claimed cost report. Output persistence and artifact serving belong to adapters. A thrown error is a transport execution failure; no validation evidence is manufactured.

## 10. Exact implementation sequence

1. **SDK compatibility probe and fixtures.** Install/lock the chosen SDK and HTTP dependencies. Prove its real SendMessage/GetTask routes, nonblocking request, Task return shape, errors, headers, strict extension numeric/null preservation and raw card serving in a small test. Read pinned SDK source where method signatures require it. Do not use a 0.3 shim if a test fails. Create reviewed synthetic exact-byte fixtures and hash/signature expectations with an independent verifier; preserve existing fixtures. No production worker until this boundary passes.
2. **Ports, signing and resolution.** Add §3 types/ports, typed BidPermit verification, profile resolver and bounded content fetcher. Test registry/wallet/profile changes and EOA/ERC-1271 matrices. Produce canonical AgentProfile with no duplicate models.
3. **Fixture bridge.** Wrap L1 transitions and synthetic registry/clock in one host. Verify commands with actual permit cryptography, credential-derived actor, atomic operation IDs, stable errors and read reconciliation. Exercise every required port, including a separately funded child and an unsettled/terminal receipt read.
4. **Journal and coordinator.** Implement claim/correlation/intent transactions and all restart phases. Test lost accept/result/child responses, concurrent messages, owner transfer and pending/finalized gating using ports. Keep monetary predicates in L1, transport safety predicates in coordinator.
5. **A2A server/client.** Wire official SDK transport to coordinator/journal. Serve exact card bytes, standard Task/status/artifact responses, profile diagnostics and bounded polling. Test with both official SDK and raw HTTP clients.
6. **Reference composition.** Add the pure worker, configured single-agent application and artifact byte route. No auto-discovery, auto-allocation or verdict runner. Make runtime restart with the same SQLite file recover as §7 specifies.
7. **External conformance and demo.** Execute §12 through two OS processes with TLS. Run every §11 case, existing L0/L1 checks and formatting. Inspect final diff/import boundaries. Update this document's status/acceptance evidence and README only when checks actually pass.

No economic design question is left for implementation. If the pinned SDK demonstrably cannot carry the frozen profile, record the exact failing wire test and adjust only the SDK adapter/dependency choice with evidence; do not relax L0 fields, versions or rejection rules to make it pass.

## 11. Tests and conformance suite

Use compact parametrized matrices plus composed HTTP tests. Golden inputs/expected outputs are reviewed data, never regenerated by the implementation being tested. Existing fixtures are abbreviated projections: expand their inputs with canonical examples, assert their expected projection, and add new exact-byte HTTP cases separately. Existing `a2a.json` has illustrative digests; it is not evidence that the example URLs serve those hashes.

| Group | Required cases and exact assertions |
|---|---|
| R — resolution | Valid identity/profile, agentId=0, large IDs; missing token/read failure/wrong registry; unset wallet; malformed registration/card; wrong registration identity; multiple distinct A2A endpoints; missing/incompatible version/binding/extension/JSON modes; unknown required extension; unsupported URI/tenant. No invalid result yields a usable profile |
| H — content | Exact-byte hash including whitespace, SHA3 mismatch, mutable document swap, max and max+1 sizes with chunked bodies, bad TLS, compressed response, timeout/redirect cap, private-address redirect/rebinding, fixture exception scoped to two origins, unknown/incomplete bytes. Failure occurs before bidding/start/result commitment |
| S — signing | Consume all existing BidPermit golden/tampering vectors; wrong chain/market/task/agent/primary type/nonce/expiry/owner; payout/profile/signer mismatch; low-s/v/length bounds; contract magic/revert/short/out-of-gas; approved operator direct bid rejected; no generic valid flag crosses HTTP. Existing verdict vectors still pass L0, but no L2 validator implemented |
| I — immutable identity | Transfer before permit consumption rejects; wallet unset blocks new bid; restore wallet/new owner can bid on a new task; transfer-back prior permit semantics unchanged; transfer after admission preserves ownerAtBid/signer/payout/card/reputation; new owner cannot accept old award; endpoint update cannot substitute profile |
| P — ports | All seven required capabilities execute against fixture bridge; valid direct and signed bid; guarded accept; child exact funding/envelope/lifetime/deadline rejection propagated from L1; result binding; null and terminal settlement reads; repeat same operation no duplicate event; changed body same ID conflict; uncertain child write reconciles one TaskCreated. No client-supplied caller/time/observation accepted |
| W — wire | Real SDK SendMessage/GetTask and independent raw HTTP; required headers; request/result:null preservation; string IDs >2^53; explicit integer constants; unknown fields/duplicates/floats rejected; standard unknown task/terminal SendMessage/unsupported feature/auth errors. No proprietary bid or settle A2A method |
| C — correlation | First request omits IDs, receiver assigns stable IDs; wrong context/task or context-only initial rejected; same execution/new messageId gives same task; reused messageId/different execution conflicts; artifact URI/digest/agent/input/policy binding checked; duplicate identical artifact harmless, differing artifact conflicts |
| J — replay/recovery | Concurrent identical messages, lost SendMessage response, agent restart at WAIT/ACCEPT_PENDING/READY/STARTED/ARTIFACT_READY/RESULT_PENDING/RESULT_RECORDED; fake/stale/pending hints; explicit tentative invalidation; finalized contradiction halts; terminal progress regression ignored. Normal run invokes once, every restart/duplicate <=1 total invocation; no automatic restart of interrupted work |
| E — composed | §12 successful transport/result commitment; forged/stale hints before and after it; restart with persisted IDs/artifact; host expires SUBMITTED to VALIDATOR_TIMEOUT with exact refund. Separate child-port integration proves independent funding without running a child agent |

Consume the relevant existing replay IDs unchanged: `duplicate-a2a`, `a2a-out-of-order`, `a2a-failure-no-refund`, `unfinalized-award-hint`, `forged-a2a-hint`, `profile-conflicting-digest`, identity-transfer/operator/wallet cases and ERC-1271 cases. Full event restart overlap/reorg projection and duplicate ERC-8004 export cases remain L4/L7; L2 tests only the provider invalidation/finality-conflict boundary. Do not label deferred cases implemented.

The external conformance suite takes card URL, AgentRef, fixture session configuration, and an operator-controlled restart command supplied locally by the test runner. It communicates only through documented fixture ports, content URLs and standard A2A HTTP. It imports no reference worker classes/SQLite schema. External participants can translate fixture ports in any language; the suite does not require a proprietary production gateway. The wire/status/binding suite is portable; internal crash-injection and invocation instrumentation form a separate reference-implementation suite. A final artifact alone cannot prove absence of hidden duplicate computation: a full execution-once claim requires an independently observable invocation counter/log from the system under test, supplied by its test operator, never trusted A2A metadata.

Every case has an ID, initial state/input, action, exact response/effect and unchanged-state assertions where rejected. Maintain L2-01..07 → case IDs → test names in the new fixture case file and have `check_layer2.py` reject missing/unused cases. Run subprocess tests with readiness checks and time bounds, use isolated temp databases/ports, and clean up both processes in a finally block. No live network/model keys or sleeps waiting for real block time.

## 12. Executable two-process end-to-end scenario

Run these commands from the repository root (use a fresh demo directory to preserve existing journals):

```sh
uv sync --locked
uv run --locked python scripts/demo_layer2.py --work-dir .scratch/layer2-demo
uv run --locked python scripts/check_layer2.py
```

`demo_layer2.py` is a supervisor/test driver, not a third protocol service. It creates an isolated fixture session, temporary TLS trust material and two subprocesses: `python -m apps.fixture_market` and `python -m apps.reference_agent.main`. Default HTTPS origins are `https://localhost:8740` and `https://localhost:8741`; fail clearly if occupied, or take two explicit alternate ports and construct/pin fixture bytes before bidding. All outputs are labeled synthetic. Neither process may share market memory; the worker accesses it over fixture HTTPS. Award and artifact exchange use real SDK A2A HTTP sockets, not ASGI TestClient or monkeypatched send methods.

Use fixture chainId `"31337"`, existing synthetic market/registry namespace, AgentRef agentId `"7"`, native MON/18, fresh Beta prior, and a requester/validator/payout distinct from the owner and execution signer. Use the public synthetic signing keys already documented in L0 to authorize the fixture bid; never fund them. Static fixtures pin exact input/schema/policy bytes; runtime card URI/bytes and their digest are recorded before signing when ports vary. Variable digest computations are setup values verified independently, not regenerated expected auction results.

| Step | Action | Required observation |
|---|---|---|
| 1 | Host creates OPEN task at synthetic time 1000/block 10: budget 100 atoms, alpha 1/100, biddingClose 1200, allocationBy 1300, acceptBy 1500, resultBy 2500, validationBy 3000, no delegation | TaskCreated, escrow 100, credits 0; no award or worker invocation |
| 2 | Before bidding/award, host sends a forged hint for that OPEN task through SDK client | PROFILE_AWARD_MISMATCH; no poisoned correlation, starts=0, no market write |
| 3 | Worker resolves registry/registration/card, checks fixture support, prepares bid 20 with frozen card digest and real owner permit; fixture host admits it at 1100 | One BidAccepted, p=500000, score=30000000; owner/signer/payout/profile match exactly |
| 4 | Host driver calls L1 allocateTask at 1200; marks resulting view pending and sends matching hint | AWARDED, critical/reserved price 50; worker non-running; accept count=0, starts=0 |
| 5 | Host marks award finalized; agent reobserves/reuses waiting correlation. Deliver duplicate hints with new message IDs, including concurrent deliveries | One acceptance with reserve 0 at 1400. Keep acceptance pending initially: starts remains 0 |
| 6 | Finalize acceptance; worker checks RUNNING, claims and executes | Exactly one invocation, input/result bytes exactly `{"value":7}\n`; stable A2A task/context; artifact digest matches independently computed keccak |
| 7 | Client polls GetTask and fetches artifact; worker commits result at 1600 and host finalizes | A2A COMPLETED, one ResultSubmitted, canonical SUBMITTED. Escrow still 100, paid/refunded/credits still 0, no reputation change |
| 8 | Restart only agent with same database/content; poll and resend initial hint without IDs | Same task/context/artifact bytes; starts remains 1. Explicit SendMessage to terminal taskId gets standard terminal-task error |
| 9 | Send wrong task/agent/input/policy, stale accepted/terminal hints and older progress; test unaccepted expired award in a separate fixture case | Existing result never changes; no second run/accept/result. Correct known terminal obligation returns stored run; actual conflicts reject, not a generic duplicate success |
| 10 | Host advances to 3000 and calls existing L1 expireTask; client reads settlement | VALIDATOR_TIMEOUT, paidAtoms=0, refundAtoms=100, counterEffect=NONE; one TaskSettled, escrow=0, refund credit=100. A2A artifact remains available; no validator was built |

Time advancement/finality barriers are controlled by the fixture driver; no action depends on waiting 2,000 real seconds. A separate child-port test uses an accepted parent with delegation enabled and calls publishChild with an exact fresh deposit. It asserts TaskCreated linkage, parent envelope counters, rejection on insufficient envelope and reconciliation after a lost child response; it does not create another reference-agent process or implement decomposition.

The report at `.scratch/layer2-demo/report.json` records fixture session ID, service PIDs, scenario/case results, schema-validated correlation, byte digests, actual invocation count, canonical event counts/status and final refund. It contains no credentials. Readiness deadline is 10 seconds per process; complete demo deadline is 60 seconds excluding dependency installation. Failure exits nonzero and includes the failed assertion; a transport timeout cannot produce a passing report.

## 13. Objective exit gate and later-layer facts

L2 is complete only when all of the following are demonstrated:

- L2-01..07 each map to passing §11 cases; no required row is skipped or represented only by a mocked function call.
- The official SDK client and a raw HTTP client interoperate with the real reference server using the exact frozen profile and headers; the external conformance suite runs without importing reference implementation internals.
- The §12 demo passes with two service processes, real HTTPS/A2A, a verified permit, finalized acceptance gating, exactly one normal invocation and a correlated immutable artifact; no forged/stale/duplicate request creates another invocation.
- Restart/uncertain-write/crash cases preserve the execution key and do not duplicate acceptance, child creation or result commitment. Interrupted work is reported honestly as interrupted.
- Every required port has an integration test, including separately funded child publication and null/terminal settlement reads. No A2A status creates payment or reputation effects.
- Existing `make check` passes unchanged in meaning: L0 fixtures/signatures, L1 behavior, **zero missing L1 production statements/branches**, checker regressions, Ruff and format. Extend Makefile with `check-l2` and `demo-l2`; keep `check` as the existing lower-layer gate until the L2 implementation is accepted.
- L2 tests cover every owned trust/replay failure branch in §11. Measure branch coverage for new modules/apps separately and report uncovered lines with justification; do not weaken L1's exact coverage gate or use exclusions to hide authority/recovery paths. Third-party SDK internals are not AgentLance coverage.
- Checkers reject missing fixture/test mappings; expected fixture results are reviewed static data. README documents the actual commands, public fixture status, at-most-once crash tradeoff and deferred live integrations. No unrelated modules, dependencies, secrets or fabricated deployment manifests appear in the diff.

Required checks after implementation:

```sh
make check
uv run --locked python scripts/check_layer2.py
uv run --locked python scripts/demo_layer2.py --work-dir .scratch/layer2-demo
git diff --check
```

Live values intentionally remain **deployment-time facts**, not L2 placeholders: actual Monad chain/network identity, market/registry addresses/code hashes/proxy revisions, verified ERC-8004 transfer/wallet/ERC-1271 behavior, finalized-tag support/freshness and receipt verification, transaction broadcast/nonce reconciliation, and validator/publisher deployments. Use the existing DeploymentManifest constraints when L3 is deployed; do not check in a half-populated manifest. The fixture's FINALIZED stamp is controlled test evidence, never a claim about Monad finality.

L3 owns native transaction authentication and the authoritative market; L4 owns discovery/event overlap/reorg recovery; L5 owns bid economics; L6 owns real execution/delegation; L7 owns validation and evidence publication. Their absence does not block this compatibility design or its offline HTTP gate. It does prevent calling L2 a deployed end-to-end AgentLance system.


### External conformance invocation

`uv run --locked pytest tests/conformance --l2-target /absolute/path/target.json` runs the same HTTP assertions against an operator-supplied agent. Omitting the option uses the isolated reference composition. The descriptor is local test configuration, never agent-supplied metadata:

```json
{
  "agentOrigin": "https://localhost:8741",
  "cardUrl": "https://localhost:8741/.well-known/agent-card.json",
  "fixtureOrigins": ["https://localhost:8740", "https://localhost:8741"],
  "caFile": "/absolute/path/local-ca.pem",
  "invocationLog": "/absolute/path/invocations.log",
  "controlCommand": ["/absolute/path/operator-fixture-controller"]
}
```

The runner invokes the controller with one argument, `award` or `complete`, without a shell. Within 30 seconds it must reset an isolated fixture session and prepare either a finalized AWARDED obligation with no starts, or a SUBMITTED obligation with one completed invocation and a retained artifact. It prints exactly `{"message": <valid initial award Message>}` on stdout, exits zero, and directs logs to stderr. Its local reset/restart behavior belongs to the operator; the suite never executes commands from an Agent Card. The invocation log must contain one line per actual invocation. HTTP requests, polling, artifact verification and rejected-message assertions are identical across implementations. The operator must keep the prepared award quiescent during the wire-only tests. Internal crash injection remains in the reference tests.

The fixture bridge authenticates synthetic actors only. Production account keys, wallet ownership, finalized RPC reads and real transactions require L3 adapters. Never deploy the fixture bridge as an authoritative public market.


## 14. Executed acceptance review

Verified 2026-10-05 with `uv run --locked python scripts/check_layer2.py` (exit 0). The gate runs the same lower-layer checks as `make check`, then L2 tests, subprocess coverage, requirement/fixture mappings, lint, formatting and `git diff --check`.

| Evidence | Result |
|---|---|
| L0 shared records/scenarios and arithmetic vectors | 37 protocol examples, 115 scenario cases, 18 market vectors validated |
| Frozen signatures | Python and independent ethers checks passed; 2 signing vectors, 9 mutations, 3 byte hashes |
| Checker regressions | 18 passed, including missing/skipped requirement evidence rejection |
| L1 | 270 tests; all 700 statements and 212 branches covered |
| L2 | 180 tests passed; includes raw HTTP, official SDK, all recovery phases and real two-process TLS integration |
| Static L2 bytes | All six hashes consumed and verified with eth-utils and independent ethers; five reviewed scenario outputs consumed unchanged |
| Requirement mapping | L2-01..07 and R/H/S/I/P/W/C/J/E groups resolve to executed passing tests; relevant frozen replay IDs retain their identities |
| Process execution | Distinct fixture host and agent PIDs; agent-only restart; stable task/context/artifact; one actual invocation |
| Economic isolation | One verified bid/award/accept/result; no A2A payment; canonical VALIDATOR_TIMEOUT yields paid=0, refund=100, counterEffect=NONE |
| Child port | Independent 20-atom deposit; exact parent counters; lost response reconciles one child; envelope/deadline/value errors retained |
| Imports and formatting | Reference-agent import does not load market_core; Ruff, format and diff checks pass |

The full gate report is `.scratch/l2-tests.xml`; coverage is `.scratch/l2-coverage.json`, combining the test process and three service lifetimes. Layer 2 covers **1230/1256 statements and 273/294 branches (96.97% combined)**. This is not a claim of 100% branch coverage. No production coverage exclusions were added. The resolver, durable journal and deterministic worker have no missing measured statements or branches.

Remaining measured paths, retained rather than hidden by exclusions:

| Source | Unexecuted lines/branches and interpretation |
|---|---|
| `apps/fixture_market.py:86,101` | Alternate explicit finalization control and unknown local operator command; demo finalization uses clock barriers |
| `apps/reference_agent/main.py:108-109,137`; entry-point false branch | Unexpected background-infrastructure failure shutdown and import-only entry path. Worker failure/storage corruption and the at-most-once restart policy are exercised separately; process-level disk exhaustion is not injected |
| `modules/adapters/a2a/client.py:88` | Removing an already-present matching bearer on a foreign-origin request. Normal credential attachment and absence on foreign origins are tested |
| `modules/adapters/a2a/profile.py:63` | Body extension-URI omission error; mandatory header/card negotiation and malformed closed metadata are covered |
| `modules/adapters/a2a/server.py:46,117-118,122,135,142,146`; response-body branch | Diagnostic task projection, bounded blocking-wait timeout, unknown cancel, unsupported push/stream hooks and empty non-JSON response. Polling, unknown task, noncancelable task, unsupported listing, terminal message and protocol diagnostics are tested; push/stream are never advertised |
| `modules/adapters/fixtures.py:45-46,314,354` | Local controller exception text, unknown content URL and transport-exception translation. HTTP authorization, UNKNOWN reconciliation and lost-response behavior are exercised |
| `modules/adapters/storage/content.py:94`; redirect-loop exhaustion arc | Optional configured-IPFS-gateway success translation and structurally unreachable loop fallthrough after the final redirect guard. HTTPS byte/digest/size/TLS/DNS/redirect/compression failures are covered |
| `modules/agent_client/participant.py:86,166,210,216`; false claim arc | Existing-bid short-circuit, non-NOT_FOUND hint provider failure, externally submitted result without local artifact, result withholding while children are active, and a losing claim within the serialized coordinator. L1 child/result guards, port errors, unknown writes, concurrent hints and journal claim/restart behavior are covered |
| `modules/agent_client/ports.py:99` | Settled receipt TaskRef cross-check inside a full TaskView; settlement reads independently validate the receipt binding |
| `modules/agent_client/signing.py:70,92` | Malformed ERC-1271 wrapper and over-4096-byte contract signature defenses; canonical command validation rejects oversized signature inputs before dispatch. Actual magic/short/revert/gas-cap and EOA recovery failures are exercised |

**Exit decision:** the L2 fixture-backed compatibility scope passes its executable gate. Live ERC-8004/Monad verification, discovery, real execution, validation and settlement deployment remain the explicitly deferred layers/facts in §13. The external-target suite is delivered; no independently operated third-party deployment was available or claimed tested.

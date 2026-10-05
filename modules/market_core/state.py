"""Explicit reference state. No registry, clock or storage access."""

from dataclasses import dataclass, field

from modules.domain.records import addressBytes, unsigned


@dataclass(frozen=True)
class CorePolicy:
    chainId: int
    market: str
    identityRegistry: str
    validator: str
    asset: dict = field(default_factory=lambda: {"kind": "NATIVE", "symbol": "MON", "decimals": 18})
    policyVersion: int = 1
    minimumAcceptanceWindow: int = 120
    synthesisSlack: int = 120
    maxDepth: int = 2
    maxChildren: int = 4

    def __post_init__(self):
        unsigned(self.chainId, 256)
        for value in (self.market, self.identityRegistry, self.validator):
            addressBytes(value)
        if self.chainId == 0 or (
            self.asset,
            self.policyVersion,
            self.minimumAcceptanceWindow,
            self.synthesisSlack,
            self.maxDepth,
            self.maxChildren,
        ) != ({"kind": "NATIVE", "symbol": "MON", "decimals": 18}, 1, 120, 120, 2, 4):
            raise ValueError("Unsupported v1 policy")


@dataclass(frozen=True)
class Checkpoint:
    blockNumber: int
    successes: int
    failures: int


@dataclass
class ReputationState:
    checkpoints: dict = field(default_factory=dict)
    seenReceipts: dict = field(default_factory=dict)


@dataclass
class TaskState:
    spec: dict
    status: str = "OPEN"
    bids: dict = field(default_factory=dict)
    topTwo: tuple = ()
    allocation: dict | None = None
    ownWorkReserveAtoms: int = 0
    childrenCreated: int = 0
    activeChildren: int = 0
    reservedChildBudgets: int = 0
    committedChildPayouts: int = 0
    result: dict | None = None
    receipt: dict | None = None
    escrowAtoms: int = 0


@dataclass
class CoreState:
    taskCount: int = 0
    tasks: dict = field(default_factory=dict)
    credits: dict = field(default_factory=dict)
    usedNonces: set = field(default_factory=set)
    reputation: ReputationState = field(default_factory=ReputationState)
    depositedAtoms: int = 0
    withdrawnAtoms: int = 0


@dataclass(frozen=True)
class IdentityObservation:
    agentRef: dict
    available: bool
    owner: str | None
    verifiedWallet: str | None


@dataclass(frozen=True)
class SignatureObservation:
    primaryType: str
    chainId: int
    market: str
    signer: str
    record: dict
    signature: str
    valid: bool


@dataclass(frozen=True)
class CommandContext:
    caller: str
    blockNumber: int
    blockTimestamp: int
    valueAtoms: int = 0
    reentrant: bool = False
    identityObservation: IdentityObservation | None = None
    signatureObservation: SignatureObservation | None = None
    transferSucceeded: bool | None = None


@dataclass(frozen=True)
class Applied:
    state: CoreState
    events: tuple
    evidence: tuple = ()


@dataclass(frozen=True)
class Rejected:
    code: str

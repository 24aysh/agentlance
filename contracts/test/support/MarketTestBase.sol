// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {Test} from "forge-std/Test.sol";
import {ProtocolTypes as P} from "../../src/ProtocolTypes.sol";
import {IAgentLanceMarket} from "../../src/IAgentLanceMarket.sol";
import {ProtocolSignatures as Signatures} from "../../src/ProtocolSignatures.sol";
import {MarketHarness} from "./MarketHarness.sol";
import {TestIdentityRegistry} from "./TestIdentityRegistry.sol";

abstract contract MarketTestBase is Test {
    uint256 internal constant OWNER_KEY = 0xA11;
    uint256 internal constant VALIDATOR_KEY = 0xB0B;
    address internal constant REQUESTER = address(0x100);
    address internal constant SIGNER = address(0x200);
    address internal constant PAYOUT = address(0x300);
    address internal constant CHILD_PAYOUT = address(0x301);
    address internal constant OTHER = address(0x400);
    bytes32 internal constant ARTIFACT = keccak256("fixture-result");
    bytes32 internal constant POLICY = keccak256("fixture-policy");
    MarketHarness internal market;
    TestIdentityRegistry internal registry;
    address internal owner;
    address internal validator;

    function setUp() public virtual {
        vm.warp(1000);
        vm.roll(10);
        owner = vm.addr(OWNER_KEY);
        validator = vm.addr(VALIDATOR_KEY);
        registry = new TestIdentityRegistry();
        market = new MarketHarness(address(registry), validator);
        registry.setIdentity(7, owner, PAYOUT);
        registry.setIdentity(9, owner, CHILD_PAYOUT);
        vm.deal(REQUESTER, 100 ether);
        vm.deal(SIGNER, 100 ether);
        vm.deal(owner, 100 ether);
        vm.deal(OTHER, 100 ether);
        vm.deal(address(this), 100 ether);
    }

    function taskRef(uint64 id) internal view returns (P.TaskRef memory) {
        return P.TaskRef(block.chainid, address(market), id);
    }

    function agentRef(uint256 id) internal view returns (P.AgentRef memory) {
        return P.AgentRef(block.chainid, address(registry), id);
    }

    function terms() internal view returns (P.TaskTerms memory value) {
        value.policyVersion = 1;
        value.refundAddress = REQUESTER;
        value.asset = P.Asset("NATIVE", "MON", 18);
        value.budgetAtoms = 100;
        value.input = P.ContentRef("ipfs://fixture-input", bytes32(0));
        value.outputSchema = P.ContentRef("https://example.invalid/schema", bytes32(0));
        value.taskFamily = "structured-output-v1";
        value.alphaNum = 1;
        value.alphaDen = 100;
        value.biddingClose = 1200;
        value.allocationBy = 1300;
        value.acceptBy = 1500;
        value.resultBy = 2500;
        value.validationBy = 3000;
        value.validator = validator;
        value.validationPolicy = P.ContentRef("ipfs://fixture-policy", POLICY);
        value.delegation = P.DelegationLimits(2, 4);
    }

    function spec(uint64 id) internal view returns (P.TaskSpec memory value) {
        value.schemaVersion = 1;
        value.taskRef = taskRef(id);
        value.requester = REQUESTER;
        value.terms = terms();
        value.rootRef = taskRef(id);
        value.createdAt = 1000;
        value.createdBlock = 10;
        value.reputationSnapshotBlock = 9;
    }

    function offer(uint64 taskId, uint256 agentId) internal view returns (P.BidOffer memory) {
        return P.BidOffer(
            taskRef(taskId),
            agentRef(agentId),
            20,
            SIGNER,
            agentId == 9 ? CHILD_PAYOUT : PAYOUT,
            bytes32(0)
        );
    }

    function bid(uint64 taskId, uint256 agentId) internal view returns (P.Bid memory) {
        return P.Bid(1, offer(taskId, agentId), owner, 9, P.Counters(0, 0), 500000, 30000000);
    }

    function allocation(uint64 id, uint256 agentId, uint96 reserve)
        internal
        view
        returns (P.Allocation memory value)
    {
        value.schemaVersion = 1;
        value.taskRef = taskRef(id);
        value.mechanismVersion = 1;
        value.winner = P.OptionalAgentRef(true, agentRef(agentId));
        value.awardId = 1;
        value.winningScore = P.OptionalInt256(true, 30000000);
        value.secondScore = P.OptionalInt256(true, 0);
        value.criticalAtoms = P.OptionalUint96(true, reserve);
        value.reservedAtoms = reserve;
        value.allocatedAt = 1200;
    }

    function result(uint64 id) internal view returns (P.ResultCommitment memory) {
        return P.ResultCommitment(
            1, P.ExecutionRef(taskRef(id), 1), P.ContentRef("ipfs://fixture-result", ARTIFACT), 2400
        );
    }

    function verdict(uint64 id, uint256 agentId, bool pass)
        internal
        view
        returns (P.ValidationRecord memory value)
    {
        value.schemaVersion = 1;
        value.executionRef = P.ExecutionRef(taskRef(id), 1);
        value.agentRef = agentRef(agentId);
        value.resultDigest = ARTIFACT;
        value.validationPolicyDigest = POLICY;
        value.verdict = pass ? 1 : 0;
        value.evidence = P.ContentRef("ipfs://fixture-evidence", keccak256("fixture-evidence"));
        value.validator = validator;
        value.nonce = id;
        value.expiry = 5000;
    }

    function signVerdict(P.ValidationRecord memory record)
        internal
        view
        returns (P.ValidationRecord memory)
    {
        bytes32 digest = Signatures.typedDigest(
            Signatures.domainSeparator(block.chainid, address(market)),
            Signatures.verdictStructHash(record)
        );
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(VALIDATOR_KEY, digest);
        record.signature = abi.encodePacked(r, s, v);
        return record;
    }

    function errorBytes(uint16 code) internal pure returns (bytes memory) {
        return abi.encodeWithSelector(IAgentLanceMarket.ProtocolError.selector, code);
    }

    function createRoot() internal returns (P.TaskRef memory ref) {
        vm.prank(REQUESTER);
        return market.createTask{value: 100}(terms());
    }

    function directBid(uint64 id, uint256 agentId) internal {
        P.OptionalBidPermit memory permit;
        P.OptionalSignature memory signature;
        vm.prank(owner);
        market.submitBid(offer(id, agentId), permit, signature);
    }

    function runTo(uint8 state) internal returns (P.TaskRef memory ref) {
        ref = createRoot();
        if (state == P.STATE_OPEN) return ref;
        directBid(ref.taskId, 7);
        vm.warp(1200);
        market.allocateTask(ref);
        if (state == P.STATE_AWARDED) return ref;
        vm.prank(SIGNER);
        market.acceptAward(P.ExecutionRef(ref, 1), 10);
        if (state == P.STATE_RUNNING) return ref;
        vm.prank(SIGNER);
        market.submitResult(P.ExecutionRef(ref, 1), result(ref.taskId).artifact);
        if (state == P.STATE_SUBMITTED) return ref;
        market.settleVerdict(signVerdict(verdict(ref.taskId, 7, true)));
    }
}

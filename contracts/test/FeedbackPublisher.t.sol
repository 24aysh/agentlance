// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {Test} from "forge-std/Test.sol";
import {FeedbackPublisher} from "../src/FeedbackPublisher.sol";
import {ProtocolTypes as P} from "../src/ProtocolTypes.sol";
import {MarketHarness} from "./support/MarketHarness.sol";
import {HardhatMinimalUUPS} from "erc8004/HardhatMinimalUUPS.sol";
import {IdentityRegistryUpgradeable} from "erc8004/IdentityRegistryUpgradeable.sol";
import {ReputationRegistryUpgradeable} from "erc8004/ReputationRegistryUpgradeable.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";

contract AdversarialReputation {
    address public identity;
    uint64 public calls;
    uint8 public mode;
    P.TaskRef private callback;

    constructor(address identity_) {
        identity = identity_;
    }

    function configure(uint8 mode_, P.TaskRef memory ref) external {
        mode = mode_;
        callback = ref;
    }

    function getIdentityRegistry() external view returns (address) {
        return identity;
    }

    function getLastIndex(uint256, address) external view returns (uint64) {
        return mode == 1 ? 0 : calls;
    }

    function giveFeedback(
        uint256,
        int128,
        uint8,
        string calldata,
        string calldata,
        string calldata,
        string calldata,
        bytes32
    ) external {
        ++calls;
        if (mode == 3) FeedbackPublisher(msg.sender).publish(callback);
        if (mode == 4) revert("unavailable");
    }

    function readFeedback(uint256, address, uint64)
        external
        view
        returns (int128, uint8, string memory, string memory, bool)
    {
        return (
            mode == 2 ? int128(0) : int128(1), 0, "agentlance-v1", "structured-output-v1", mode == 5
        );
    }
}

contract FeedbackPublisherTest is Test {
    IdentityRegistryUpgradeable private identity;
    ReputationRegistryUpgradeable private reputation;
    MarketHarness private market;
    FeedbackPublisher private publisher;
    address private constant OWNER = address(0x100);

    function setUp() public {
        HardhatMinimalUUPS bootstrap = new HardhatMinimalUUPS();
        address identityProxy = address(
            new ERC1967Proxy(address(bootstrap), abi.encodeCall(bootstrap.initialize, (address(0))))
        );
        HardhatMinimalUUPS(identityProxy)
            .upgradeToAndCall(
                address(new IdentityRegistryUpgradeable()),
                abi.encodeCall(IdentityRegistryUpgradeable.initialize, ())
            );
        identity = IdentityRegistryUpgradeable(identityProxy);
        address reputationProxy = address(
            new ERC1967Proxy(
                address(bootstrap), abi.encodeCall(bootstrap.initialize, (identityProxy))
            )
        );
        HardhatMinimalUUPS(reputationProxy)
            .upgradeToAndCall(
                address(new ReputationRegistryUpgradeable()),
                abi.encodeCall(ReputationRegistryUpgradeable.initialize, (identityProxy))
            );
        reputation = ReputationRegistryUpgradeable(reputationProxy);
        vm.prank(OWNER);
        assertEq(identity.register(), 0);
        market = new MarketHarness(identityProxy, address(0x200));
        publisher = new FeedbackPublisher(address(market), identityProxy, reputationProxy);
        seed(1, P.REASON_SUCCESS, P.EFFECT_SUCCESS);
        seed(2, P.REASON_VALIDATION_FAILED, P.EFFECT_FAILURE);
    }

    function ref(uint64 id) private view returns (P.TaskRef memory) {
        return P.TaskRef(block.chainid, address(market), id);
    }

    function seed(uint64 id, uint8 reason, uint8 effect) private {
        P.TaskView memory v;
        v.task.taskRef = ref(id);
        v.task.terms.taskFamily = "structured-output-v1";
        v.status = P.STATE_SETTLED;
        v.receipt.present = true;
        v.receipt.value.taskRef = ref(id);
        v.receipt.value.reason = reason;
        v.receipt.value.counterEffect = effect;
        v.receipt.value.winner =
            P.OptionalAgentRef(true, P.AgentRef(block.chainid, address(identity), 0));
        market.seedTask(v);
    }

    function testPinnedRegistryExactlyOnceAndMetadata() public {
        vm.recordLogs();
        assertEq(publisher.publish(ref(1)), 1);
        assertEq(vm.getRecordedLogs().length, 2);
        vm.recordLogs();
        vm.prank(address(0xabc));
        assertEq(publisher.publish(ref(1)), 1);
        assertEq(vm.getRecordedLogs().length, 0);
        assertEq(publisher.publish(ref(2)), 2);
        assertEq(reputation.getLastIndex(0, address(publisher)), 2);
        (int128 value, uint8 decimals, string memory tag1, string memory tag2, bool revoked) =
            reputation.readFeedback(0, address(publisher), 1);
        assertEq(value, 1);
        assertEq(decimals, 0);
        assertEq(tag1, "agentlance-v1");
        assertEq(tag2, "structured-output-v1");
        assertFalse(revoked);
        (value,,,,) = reputation.readFeedback(0, address(publisher), 2);
        assertEq(value, 0);
        (bool published, uint64 index) = publisher.readPublication(ref(1));
        assertTrue(published);
        assertEq(index, 1);
    }

    function testPinnedOwnerOperatorTransferRestriction() public {
        vm.prank(OWNER);
        identity.approve(address(publisher), 0);
        vm.expectRevert("Self-feedback not allowed");
        publisher.publish(ref(1));
        (bool published,) = publisher.readPublication(ref(1));
        assertFalse(published);
        vm.prank(OWNER);
        identity.approve(address(0), 0);
        vm.prank(OWNER);
        identity.setApprovalForAll(address(publisher), true);
        vm.expectRevert("Self-feedback not allowed");
        publisher.publish(ref(1));
        vm.prank(OWNER);
        identity.setApprovalForAll(address(publisher), false);
        vm.prank(OWNER);
        identity.transferFrom(OWNER, address(publisher), 0);
        vm.expectRevert("Self-feedback not allowed");
        publisher.publish(ref(1));
        vm.prank(address(publisher));
        identity.transferFrom(address(publisher), OWNER, 0);
        assertEq(publisher.publish(ref(1)), 1);
    }

    function testNoneUnknownForeignUnsettledAndBinding() public {
        seed(3, P.REASON_VALIDATOR_TIMEOUT, P.EFFECT_NONE);
        vm.recordLogs();
        assertEq(publisher.publish(ref(3)), 0);
        assertEq(vm.getRecordedLogs().length, 0);
        (bool published,) = publisher.readPublication(ref(3));
        assertFalse(published);
        vm.expectRevert();
        publisher.publish(ref(99));
        P.TaskView memory v;
        v.task.taskRef = ref(4);
        market.seedTask(v);
        vm.expectRevert();
        publisher.publish(ref(4));
        P.TaskRef memory other = ref(1);
        other.chainId++;
        vm.expectRevert();
        publisher.publish(other);
        other = ref(1);
        other.market = address(0xdead);
        vm.expectRevert();
        publisher.publish(other);
        vm.expectRevert();
        new FeedbackPublisher(address(market), address(0xdead), address(reputation));
    }

    function testAdversarialRegistryAtomicityAndReentrancy() public {
        AdversarialReputation bad = new AdversarialReputation(address(identity));
        FeedbackPublisher p =
            new FeedbackPublisher(address(market), address(identity), address(bad));
        for (uint8 mode = 1; mode <= 5; ++mode) {
            bad.configure(mode, ref(2));
            vm.expectRevert();
            p.publish(ref(1));
            assertEq(bad.calls(), 0);
            (bool published,) = p.readPublication(ref(1));
            assertFalse(published);
        }
        bad.configure(0, ref(2));
        assertEq(p.publish(ref(1)), 1);
    }

    function testFrozenOutcomeMapping() public {
        string memory fixtures = vm.readFile("specs/fixtures/layer-7/outcomes.json");
        for (uint64 i; i < 8; ++i) {
            uint8 effect = i == 0 ? P.EFFECT_SUCCESS : i < 4 ? P.EFFECT_FAILURE : P.EFFECT_NONE;
            seed(10 + i, uint8(i + 1), effect);
            uint64 index = publisher.publish(ref(10 + i));
            string memory path = string.concat(".cases[", vm.toString(i), "]");
            if (effect == P.EFFECT_NONE) {
                assertEq(index, 0);
            } else {
                (int128 value,,,,) = reputation.readFeedback(0, address(publisher), index);
                assertEq(value, vm.parseJsonInt(fixtures, string.concat(path, ".value")));
            }
        }
    }

    function testFuzzPublicationReplay(uint8 count) public {
        count = uint8(bound(count, 1, 50));
        for (uint256 i; i < count; ++i) {
            assertEq(publisher.publish(ref(1)), 1);
        }
        assertEq(reputation.getLastIndex(0, address(publisher)), 1);
    }
}

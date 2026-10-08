// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {ProtocolTypes as P} from "./ProtocolTypes.sol";
import {IAgentLanceViews} from "./IAgentLanceViews.sol";

interface IReputationRegistry {
    function getIdentityRegistry() external view returns (address);
    function getLastIndex(uint256 agentId, address client) external view returns (uint64);
    function giveFeedback(
        uint256,
        int128,
        uint8,
        string calldata,
        string calldata,
        string calldata,
        string calldata,
        bytes32
    ) external;
    function readFeedback(uint256, address, uint64)
        external
        view
        returns (int128, uint8, string memory, string memory, bool);
}

/// @notice One portable feedback entry per canonical receipt, independent of its gas payer.
contract FeedbackPublisher is ReentrancyGuard {
    uint256 public immutable chainId;
    address public immutable market;
    address public immutable identityRegistry;
    address public immutable reputationRegistry;
    mapping(uint64 => uint64) private publications;

    error PublicationError();
    event ReceiptPublished(P.TaskRef taskRef, uint64 feedbackIndex);

    constructor(address market_, address identityRegistry_, address reputationRegistry_) {
        (uint256 chain, address canonical, address identity,, uint8 version) =
            IAgentLanceViews(market_).readPolicy();
        if (
            chain != block.chainid || canonical != market_ || identity != identityRegistry_
                || version != 1
                || IReputationRegistry(reputationRegistry_).getIdentityRegistry() != identity
        ) revert PublicationError();
        chainId = chain;
        market = market_;
        identityRegistry = identity;
        reputationRegistry = reputationRegistry_;
    }

    function checkRef(P.TaskRef calldata ref) private view {
        if (ref.chainId != chainId || ref.market != market || ref.taskId == 0) {
            revert PublicationError();
        }
    }

    function readPublication(P.TaskRef calldata ref)
        external
        view
        returns (bool published, uint64 feedbackIndex)
    {
        checkRef(ref);
        feedbackIndex = publications[ref.taskId];
        return (feedbackIndex != 0, feedbackIndex);
    }

    function publish(P.TaskRef calldata ref) external nonReentrant returns (uint64 index) {
        checkRef(ref);
        index = publications[ref.taskId];
        if (index != 0) return index;
        P.TaskView memory view_ = IAgentLanceViews(market).readTask(ref);
        if (!view_.receipt.present || view_.status != P.STATE_SETTLED) revert PublicationError();
        P.SettlementReceipt memory receipt = view_.receipt.value;
        if (receipt.counterEffect == P.EFFECT_NONE) return 0;
        if (
            !receipt.winner.present || receipt.winner.value.identityRegistry != identityRegistry
                || receipt.winner.value.chainId != chainId
        ) revert PublicationError();
        int128 value;
        if (receipt.counterEffect == P.EFFECT_SUCCESS && receipt.reason == P.REASON_SUCCESS) {
            value = 1;
        } else if (
            receipt.counterEffect != P.EFFECT_FAILURE
                || (receipt.reason != P.REASON_VALIDATION_FAILED
                    && receipt.reason != P.REASON_NO_SHOW
                    && receipt.reason != P.REASON_EXECUTION_TIMEOUT)
        ) {
            revert PublicationError();
        }
        IReputationRegistry registry = IReputationRegistry(reputationRegistry);
        uint256 agentId = receipt.winner.value.agentId;
        uint64 beforeIndex = registry.getLastIndex(agentId, address(this));
        string memory family = view_.task.terms.taskFamily;
        registry.giveFeedback(agentId, value, 0, "agentlance-v1", family, "", "", bytes32(0));
        index = registry.getLastIndex(agentId, address(this));
        if (index != beforeIndex + 1) revert PublicationError();
        (int128 stored, uint8 decimals, string memory tag1, string memory tag2, bool revoked) =
            registry.readFeedback(agentId, address(this), index);
        if (
            stored != value || decimals != 0 || revoked
                || keccak256(bytes(tag1)) != keccak256("agentlance-v1")
                || keccak256(bytes(tag2)) != keccak256(bytes(family))
        ) revert PublicationError();
        publications[ref.taskId] = index;
        emit ReceiptPublished(ref, index);
    }
}

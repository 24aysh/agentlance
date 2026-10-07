// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {MarketTestBase} from "./support/MarketTestBase.sol";
import {SignatureOwner} from "./support/HostileActors.sol";
import {ProtocolTypes as P} from "../src/ProtocolTypes.sol";
import {ProtocolSignatures as Signatures} from "../src/ProtocolSignatures.sol";

contract SignaturesTest is MarketTestBase {
    function permitFor(P.BidOffer memory value, address signer)
        internal
        view
        returns (P.BidPermit memory)
    {
        return P.BidPermit(
            value.taskRef,
            value.agentRef,
            signer,
            value.executionSigner,
            value.payout,
            value.bidAtoms,
            value.profileDigest,
            0,
            1200
        );
    }

    function permitDigest(P.BidPermit memory permit) internal view returns (bytes32) {
        return Signatures.typedDigest(
            Signatures.domainSeparator(block.chainid, address(market)),
            Signatures.bidStructHash(permit)
        );
    }

    function signPermit(P.BidPermit memory permit) internal view returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(OWNER_KEY, permitDigest(permit));
        return abi.encodePacked(r, s, v);
    }

    function testEoaRelayedPermitFreezesOwnerAndPayout() public {
        createRoot();
        P.BidOffer memory value = offer(1, 7);
        P.BidPermit memory permit = permitFor(value, owner);
        market.submitBid(
            value, P.OptionalBidPermit(true, permit), P.OptionalSignature(true, signPermit(permit))
        );
        assertTrue(market.isNonceUsed(owner, Signatures.BID_TYPEHASH, 0));
        assertFalse(market.isNonceUsed(owner, Signatures.VERDICT_TYPEHASH, 0));
        vm.prank(owner);
        registry.transferIdentity(7, OTHER);
        registry.setUnavailable(true);
        vm.warp(1200);
        market.allocateTask(taskRef(1));
        vm.prank(SIGNER);
        market.acceptAward(P.ExecutionRef(taskRef(1), 1), 0);
        vm.prank(SIGNER);
        market.submitResult(P.ExecutionRef(taskRef(1), 1), result(1).artifact);
        market.settleVerdict(signVerdict(verdict(1, 7, true)));
        assertEq(market.readCredit(PAYOUT), 50);
        assertEq(market.readBid(taskRef(1), agentRef(7)).value.ownerAtBid, owner);
    }

    function testOldOwnerPermitAndTransferBack() public {
        createRoot();
        P.BidOffer memory value = offer(1, 7);
        P.BidPermit memory permit = permitFor(value, owner);
        bytes memory signature = signPermit(permit);
        vm.prank(owner);
        registry.transferIdentity(7, OTHER);
        vm.expectRevert(errorBytes(P.OWNER_CHANGED));
        market.submitBid(
            value, P.OptionalBidPermit(true, permit), P.OptionalSignature(true, signature)
        );
        vm.prank(OTHER);
        registry.transferIdentity(7, owner);
        vm.expectRevert(errorBytes(P.WALLET_UNSET));
        market.submitBid(
            value, P.OptionalBidPermit(true, permit), P.OptionalSignature(true, signature)
        );
        vm.prank(owner);
        registry.setWallet(7, PAYOUT);
        market.submitBid(
            value, P.OptionalBidPermit(true, permit), P.OptionalSignature(true, signature)
        );
    }

    function testContractOwnerExactMagic() public {
        contractOwnerCase(0, true);
    }

    function testContractOwnerRevert() public {
        contractOwnerCase(1, false);
    }

    function testContractOwnerShort() public {
        contractOwnerCase(2, false);
    }

    function testContractOwnerExtra() public {
        contractOwnerCase(3, false);
    }

    function testContractOwnerDirty() public {
        contractOwnerCase(4, false);
    }

    function testContractOwnerReturnBomb() public {
        contractOwnerCase(5, false);
    }

    function testContractOwnerGasExhaustion() public {
        contractOwnerCase(6, false);
    }

    function testContractOwnerStateful() public {
        contractOwnerCase(7, false);
    }

    function contractOwnerCase(uint256 mode, bool accepted) internal {
        createRoot();
        SignatureOwner contractOwner = new SignatureOwner();
        registry.setIdentity(7, address(contractOwner), PAYOUT);
        P.BidOffer memory value = offer(1, 7);
        P.BidPermit memory permit = permitFor(value, address(contractOwner));
        contractOwner.configure(mode, permitDigest(permit));
        if (!accepted) vm.expectRevert(errorBytes(P.INVALID_SIGNATURE));
        market.submitBid(
            value, P.OptionalBidPermit(true, permit), P.OptionalSignature(true, hex"01")
        );
        assertEq(market.readBid(taskRef(1), agentRef(7)).present, accepted);
        assertEq(market.isNonceUsed(address(contractOwner), Signatures.BID_TYPEHASH, 0), accepted);
    }

    function testEoaRejectsHighSBadVCompactAndZero() public {
        createRoot();
        P.BidOffer memory value = offer(1, 7);
        P.BidPermit memory permit = permitFor(value, owner);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(OWNER_KEY, permitDigest(permit));
        uint256 order = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141;
        bytes[] memory bad = new bytes[](5);
        bad[0] = abi.encodePacked(r, bytes32(order - uint256(s)), v == 27 ? uint8(28) : uint8(27));
        bad[1] = abi.encodePacked(r, s, uint8(0));
        bad[2] = abi.encodePacked(r, s);
        bad[3] = abi.encodePacked(bytes32(0), bytes32(0), uint8(27));
        bad[4] = hex"01";
        for (uint256 i; i < bad.length; i++) {
            vm.expectRevert(errorBytes(P.INVALID_SIGNATURE));
            market.submitBid(
                value, P.OptionalBidPermit(true, permit), P.OptionalSignature(true, bad[i])
            );
            assertFalse(market.isNonceUsed(owner, Signatures.BID_TYPEHASH, 0));
        }
    }

    function testInvalidVerdictSignaturePrecedesNonceAndExpiry() public {
        runTo(P.STATE_SUBMITTED);
        P.ValidationRecord memory record = signVerdict(verdict(1, 7, true));
        record.signature[64] = 0;
        market.seedNonce(validator, Signatures.VERDICT_TYPEHASH, record.nonce);
        vm.expectRevert(errorBytes(P.INVALID_SIGNATURE));
        market.settleVerdict(record);
        record.nonce = 999;
        record.expiry = uint64(block.timestamp);
        vm.expectRevert(errorBytes(P.INVALID_SIGNATURE));
        market.settleVerdict(record);
        assertFalse(market.isNonceUsed(validator, Signatures.VERDICT_TYPEHASH, 999));
    }

    function testValidatorWithCodeStillRequiresEcdsa() public {
        runTo(P.STATE_SUBMITTED);
        SignatureOwner verifier = new SignatureOwner();
        vm.etch(validator, address(verifier).code);
        P.ValidationRecord memory record = signVerdict(verdict(1, 7, true));
        market.settleVerdict(record);
        assertEq(market.readCredit(PAYOUT), 50);
    }

    function testPermitFailuresDoNotConsumeNonce() public {
        createRoot();
        P.BidOffer memory value = offer(1, 7);
        P.BidPermit memory permit = permitFor(value, owner);
        value.bidAtoms = 101;
        permit.bidAtoms = 101;
        vm.expectRevert(errorBytes(P.BID_OVER_BUDGET));
        market.submitBid(
            value, P.OptionalBidPermit(true, permit), P.OptionalSignature(true, signPermit(permit))
        );
        assertFalse(market.isNonceUsed(owner, Signatures.BID_TYPEHASH, 0));
        value.bidAtoms = permit.bidAtoms = 20;
        permit.profileDigest = bytes32(uint256(1));
        vm.expectRevert(errorBytes(P.INVALID_SIGNATURE));
        market.submitBid(
            value, P.OptionalBidPermit(true, permit), P.OptionalSignature(true, signPermit(permit))
        );
        assertFalse(market.isNonceUsed(owner, Signatures.BID_TYPEHASH, 0));
    }

    function testExpiredPermitRollbackAndNonceTypeSeparation() public {
        createRoot();
        P.BidOffer memory value = offer(1, 7);
        P.BidPermit memory permit = permitFor(value, owner);
        permit.expiry = 1100;
        permit.nonce = 7;
        vm.warp(1100);
        vm.expectRevert(errorBytes(P.SIGNATURE_EXPIRED));
        market.submitBid(
            value, P.OptionalBidPermit(true, permit), P.OptionalSignature(true, signPermit(permit))
        );
        assertFalse(market.isNonceUsed(owner, Signatures.BID_TYPEHASH, 7));
        permit.expiry++;
        market.seedNonce(owner, Signatures.VERDICT_TYPEHASH, 7);
        market.submitBid(
            value, P.OptionalBidPermit(true, permit), P.OptionalSignature(true, signPermit(permit))
        );
        assertTrue(market.isNonceUsed(owner, Signatures.BID_TYPEHASH, 7));
        assertTrue(market.isNonceUsed(owner, Signatures.VERDICT_TYPEHASH, 7));
    }

    function testUsedPermitNonceRejectsAndDirectBidConsumesNone() public {
        createRoot();
        P.BidOffer memory value = offer(1, 7);
        P.BidPermit memory permit = permitFor(value, owner);
        market.seedNonce(owner, Signatures.BID_TYPEHASH, 0);
        vm.expectRevert(errorBytes(P.NONCE_USED));
        market.submitBid(
            value, P.OptionalBidPermit(true, permit), P.OptionalSignature(true, signPermit(permit))
        );
        directBid(1, 7);
        assertFalse(market.isNonceUsed(owner, Signatures.BID_TYPEHASH, 1));
    }

    function testPermitTaskMismatchPrecedesSettledGuard() public {
        runTo(P.STATE_SETTLED);
        P.BidOffer memory value = offer(1, 7);
        P.BidPermit memory permit = permitFor(value, owner);
        permit.taskRef.taskId = 2;
        vm.expectRevert(errorBytes(P.UNKNOWN_TASK));
        market.submitBid(
            value, P.OptionalBidPermit(true, permit), P.OptionalSignature(true, signPermit(permit))
        );
    }

    function testMismatchedOptionalPermitAndSignature() public {
        createRoot();
        P.OptionalBidPermit memory permit;
        vm.expectRevert(errorBytes(P.INVALID_SIGNATURE));
        market.submitBid(offer(1, 7), permit, P.OptionalSignature(true, hex"01"));
        permit = P.OptionalBidPermit(true, permitFor(offer(1, 7), owner));
        P.OptionalSignature memory signature;
        vm.expectRevert(errorBytes(P.INVALID_SIGNATURE));
        market.submitBid(offer(1, 7), permit, signature);
    }
}

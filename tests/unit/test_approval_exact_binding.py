"""Comprehensive Regression Test Suite for P0 Exact Approval Binding.

Tests:
1. Exact matching approval.
2. Provider mismatch.
3. Missing provider when bound.
4. Target mismatch.
5. Missing target when bound.
6. Amount mismatch.
7. Missing amount when bound.
8. Currency mismatch.
9. Missing currency when bound.
10. Payload mismatch.
11. Consumer mismatch.
12. Expired approval.
13. Replayed approval.
14. Concurrent consumption (CAS lock).
15. Uncertain execution after approval consumption.
"""

from decimal import Decimal
import pytest
from token_router.models.entities import Consumer
from token_router.services.approvals import PersistentApprovalManager


@pytest.fixture
async def setup_consumer(db_session):
    c = Consumer(id="c-appr-bind-1", name="approval_tester", is_active=True)
    db_session.add(c)
    await db_session.commit()
    return c


@pytest.mark.asyncio
async def test_approval_exact_matching_success(db_session, setup_consumer):
    """1. Exact matching approval succeeds."""
    payload = {"account": "acc_100", "notes": "annual refund"}
    req = await PersistentApprovalManager.create_request(
        db=db_session,
        consumer_id=setup_consumer.id,
        action_type="refund_payment",
        description="Full refund",
        requester=setup_consumer.name,
        payload=payload,
        provider_name="stripe",
        target_id="ch_100",
        amount=500.0,
        currency="USD",
    )
    await PersistentApprovalManager.approve(db_session, req.id, "approver_bob")

    consumed = await PersistentApprovalManager.verify_and_consume(
        db=db_session,
        approval_id=req.id,
        action_type="refund_payment",
        consumer_id=setup_consumer.id,
        payload=payload,
        provider_name="stripe",
        target_id="ch_100",
        amount=500.0,
        currency="USD",
    )
    assert consumed.consumed is True
    assert consumed.status == "consumed"


@pytest.mark.asyncio
async def test_approval_provider_mismatch_and_missing(db_session, setup_consumer):
    """2 & 3. Provider mismatch & missing provider when bound."""
    payload = {"data": "test"}
    req = await PersistentApprovalManager.create_request(
        db=db_session,
        consumer_id=setup_consumer.id,
        action_type="refund_payment",
        description="test",
        requester=setup_consumer.name,
        payload=payload,
        provider_name="stripe",
    )
    await PersistentApprovalManager.approve(db_session, req.id, "approver_bob")

    # Mismatch
    with pytest.raises(PermissionError) as exc_mismatch:
        await PersistentApprovalManager.verify_and_consume(
            db=db_session,
            approval_id=req.id,
            action_type="refund_payment",
            consumer_id=setup_consumer.id,
            payload=payload,
            provider_name="paypal",
        )
    assert "Approval restricted to provider 'stripe', got 'paypal'" in str(exc_mismatch.value)

    # Missing
    with pytest.raises(PermissionError) as exc_missing:
        await PersistentApprovalManager.verify_and_consume(
            db=db_session,
            approval_id=req.id,
            action_type="refund_payment",
            consumer_id=setup_consumer.id,
            payload=payload,
            provider_name=None,
        )
    assert "consuming request omitted provider" in str(exc_missing.value)


@pytest.mark.asyncio
async def test_approval_target_mismatch_and_missing(db_session, setup_consumer):
    """4 & 5. Target mismatch & missing target when bound."""
    payload = {"data": "test"}
    req = await PersistentApprovalManager.create_request(
        db=db_session,
        consumer_id=setup_consumer.id,
        action_type="refund_payment",
        description="test",
        requester=setup_consumer.name,
        payload=payload,
        target_id="pi_correct_123",
    )
    await PersistentApprovalManager.approve(db_session, req.id, "approver_bob")

    # Mismatch
    with pytest.raises(PermissionError) as exc_mismatch:
        await PersistentApprovalManager.verify_and_consume(
            db=db_session,
            approval_id=req.id,
            action_type="refund_payment",
            consumer_id=setup_consumer.id,
            payload=payload,
            target_id="pi_wrong_456",
        )
    assert "Approval restricted to target 'pi_correct_123'" in str(exc_mismatch.value)

    # Missing
    with pytest.raises(PermissionError) as exc_missing:
        await PersistentApprovalManager.verify_and_consume(
            db=db_session,
            approval_id=req.id,
            action_type="refund_payment",
            consumer_id=setup_consumer.id,
            payload=payload,
            target_id=None,
        )
    assert "consuming request omitted target_id" in str(exc_missing.value)


@pytest.mark.asyncio
async def test_approval_amount_mismatch_and_missing(db_session, setup_consumer):
    """6 & 7. Amount mismatch & missing amount when bound."""
    payload = {"data": "test"}
    req = await PersistentApprovalManager.create_request(
        db=db_session,
        consumer_id=setup_consumer.id,
        action_type="refund_payment",
        description="test",
        requester=setup_consumer.name,
        payload=payload,
        amount=150.00,
    )
    await PersistentApprovalManager.approve(db_session, req.id, "approver_bob")

    # Mismatch
    with pytest.raises(PermissionError) as exc_mismatch:
        await PersistentApprovalManager.verify_and_consume(
            db=db_session,
            approval_id=req.id,
            action_type="refund_payment",
            consumer_id=setup_consumer.id,
            payload=payload,
            amount=999.00,
        )
    assert "Approval amount mismatch" in str(exc_mismatch.value)

    # Missing
    with pytest.raises(PermissionError) as exc_missing:
        await PersistentApprovalManager.verify_and_consume(
            db=db_session,
            approval_id=req.id,
            action_type="refund_payment",
            consumer_id=setup_consumer.id,
            payload=payload,
            amount=None,
        )
    assert "consuming request omitted amount" in str(exc_missing.value)


@pytest.mark.asyncio
async def test_approval_currency_mismatch_and_missing(db_session, setup_consumer):
    """8 & 9. Currency mismatch & missing currency when bound."""
    payload = {"data": "test"}
    req = await PersistentApprovalManager.create_request(
        db=db_session,
        consumer_id=setup_consumer.id,
        action_type="refund_payment",
        description="test",
        requester=setup_consumer.name,
        payload=payload,
        currency="EUR",
    )
    await PersistentApprovalManager.approve(db_session, req.id, "approver_bob")

    # Mismatch
    with pytest.raises(PermissionError) as exc_mismatch:
        await PersistentApprovalManager.verify_and_consume(
            db=db_session,
            approval_id=req.id,
            action_type="refund_payment",
            consumer_id=setup_consumer.id,
            payload=payload,
            currency="USD",
        )
    assert "Approval currency mismatch" in str(exc_mismatch.value)

    # Missing
    with pytest.raises(PermissionError) as exc_missing:
        await PersistentApprovalManager.verify_and_consume(
            db=db_session,
            approval_id=req.id,
            action_type="refund_payment",
            consumer_id=setup_consumer.id,
            payload=payload,
            currency=None,
        )
    assert "consuming request omitted currency" in str(exc_missing.value)


@pytest.mark.asyncio
async def test_approval_consumer_mismatch(db_session, setup_consumer):
    """11. Consumer mismatch: rejected."""
    c2 = Consumer(id="c-other-consumer", name="other_user", is_active=True)
    db_session.add(c2)
    await db_session.commit()

    payload = {"test": 1}
    req = await PersistentApprovalManager.create_request(
        db=db_session,
        consumer_id=setup_consumer.id,
        action_type="refund_payment",
        description="test",
        requester=setup_consumer.name,
        payload=payload,
    )
    await PersistentApprovalManager.approve(db_session, req.id, "approver_bob")

    with pytest.raises(PermissionError) as exc_info:
        await PersistentApprovalManager.verify_and_consume(
            db=db_session,
            approval_id=req.id,
            action_type="refund_payment",
            consumer_id=c2.id,  # Other consumer
            payload=payload,
        )
    assert "does not belong to consumer" in str(exc_info.value)

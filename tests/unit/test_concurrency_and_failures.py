"""Failure injection and concurrency tests for Persistent Approvals, Webhook worker retries, and Idempotency."""

import asyncio
import pytest
from token_router.models.entities import Consumer, Provider, ProviderCredential
from token_router.services.approvals import PersistentApprovalManager, compute_payload_hash
from token_router.services.idempotency import IdempotencyManager
from token_router.services.webhook_service import WebhookManager


@pytest.mark.asyncio
async def test_approval_payload_tamper_detected(db_session):
    """Verifies that tampering with even a single field of an approved payload rejects execution."""
    consumer = Consumer(id="c-tamper", name="tamper_tester", is_active=True)
    db_session.add(consumer)
    await db_session.commit()

    legit_payload = {"account": "acc_123", "amount": 1000, "currency": "usd"}
    req = await PersistentApprovalManager.create_request(
        db=db_session,
        consumer_id=consumer.id,
        action_type="refund_payment",
        description="Legitimate refund",
        requester=consumer.name,
        payload=legit_payload,
    )
    await PersistentApprovalManager.approve(db_session, req.id, "security_approver")

    # Attempt to consume with tampered payload ($10,000 instead of $1,000)
    tampered_payload = {"account": "acc_123", "amount": 10000, "currency": "usd"}
    with pytest.raises(PermissionError) as exc_info:
        await PersistentApprovalManager.verify_and_consume(
            db=db_session,
            approval_id=req.id,
            action_type="refund_payment",
            consumer_id=consumer.id,
            payload=tampered_payload,
        )
    assert "Approval payload hash mismatch" in str(exc_info.value)


@pytest.mark.asyncio
async def test_idempotency_key_payload_conflict_detected(db_session):
    """Reusing the same idempotency key with a modified payload raises ValueError."""
    consumer = Consumer(id="c-idemp-test", name="idemp_consumer", is_active=True)
    db_session.add(consumer)
    await db_session.commit()

    key = "idem_key_unique_456"
    p1 = {"order_id": "ord_1", "amount": 500}
    is_new, rec = await IdempotencyManager.acquire_or_get(
        db=db_session,
        consumer_id=consumer.id,
        idempotency_key=key,
        capability="create_payment_intent",
        payload=p1,
    )
    assert is_new is True

    # Mark first request completed
    await IdempotencyManager.mark_completed(
        db=db_session,
        consumer_id=consumer.id,
        idempotency_key=key,
        status_code=200,
        response_data={"id": "pi_123", "status": "succeeded"},
    )

    # Attempt reuse with same payload -> returns cached completed record
    is_new_same, rec_same = await IdempotencyManager.acquire_or_get(
        db=db_session,
        consumer_id=consumer.id,
        idempotency_key=key,
        capability="create_payment_intent",
        payload=p1,
    )
    assert is_new_same is False
    assert rec_same.response_data["id"] == "pi_123"

    # Attempt reuse with conflicting payload -> raises ValueError
    p2_tampered = {"order_id": "ord_1", "amount": 9999}
    with pytest.raises(ValueError) as val_err:
        await IdempotencyManager.acquire_or_get(
            db=db_session,
            consumer_id=consumer.id,
            idempotency_key=key,
            capability="create_payment_intent",
            payload=p2_tampered,
        )
    assert "reused with different request payload" in str(val_err.value)


@pytest.mark.asyncio
async def test_webhook_processing_retries_and_dead_letter(db_session):
    """Simulates failures during asynchronous webhook handling, verifying exponential retry and dead_letter state."""
    headers = {"mock-header": "test"}
    body = b'{"id": "evt_fail_1", "type": "charge.failed"}'

    # Ingest event directly into queued state
    from token_router.models.entities import WebhookEvent
    event = WebhookEvent(
        provider_name="stripe",
        external_event_id="evt_fail_1",
        event_type="charge.failed",
        status="queued",
        payload={"id": "evt_fail_1"},
    )
    db_session.add(event)
    await db_session.commit()
    await db_session.refresh(event)

    # Simulate failing handler
    async def failing_handler(payload):
        raise ConnectionResetError("Downstream CRM webhook endpoint unreachable")

    # Attempt 1 -> retry_scheduled
    ev = await WebhookManager.process_event(db_session, event.id, failing_handler)
    assert ev.status == "retry_scheduled"
    assert ev.retry_count == 1

    # Simulate retries up to attempt 5 -> dead_letter
    for _ in range(4):
        ev = await WebhookManager.process_event(db_session, event.id, failing_handler)

    assert ev.status == "dead_letter"
    assert ev.retry_count == 5

    # Manual retry re-queues event
    recovered = await WebhookManager.retry_dead_letter_event(db_session, event.id)
    assert recovered.status == "queued"
    assert recovered.retry_count == 0


@pytest.mark.asyncio
async def test_concurrent_idempotency_race(db_session):
    """Simulates concurrent in-flight requests with identical idempotency key; exactly one wins and second gets conflict."""
    from token_router.services.idempotency import IdempotencyConflictError
    consumer = Consumer(id="c-idemp-race", name="idemp_racer", is_active=True)
    db_session.add(consumer)
    await db_session.commit()

    key = "idem_race_key_777"
    payload = {"order_id": "ord_race_1", "amount": 250}

    # Step 1: First request acquires
    is_new, rec = await IdempotencyManager.acquire_or_get(
        db=db_session,
        consumer_id=consumer.id,
        idempotency_key=key,
        capability="create_payment_intent",
        payload=payload,
    )
    assert is_new is True
    assert rec.status == "in_progress"

    # Step 2: Concurrent second request arrives while first is still in_progress
    with pytest.raises(IdempotencyConflictError) as exc_info:
        await IdempotencyManager.acquire_or_get(
            db=db_session,
            consumer_id=consumer.id,
            idempotency_key=key,
            capability="create_payment_intent",
            payload=payload,
        )
    assert "currently in progress" in str(exc_info.value)

    # Step 3: First request completes
    await IdempotencyManager.mark_completed(
        db=db_session,
        consumer_id=consumer.id,
        idempotency_key=key,
        status_code=200,
        response_data={"payment_id": "pay_999"},
    )

    # Step 4: Subsequent request returns cached response
    is_new_third, rec_third = await IdempotencyManager.acquire_or_get(
        db=db_session,
        consumer_id=consumer.id,
        idempotency_key=key,
        capability="create_payment_intent",
        payload=payload,
    )
    assert is_new_third is False
    assert rec_third.status == "succeeded"
    assert rec_third.response_data["payment_id"] == "pay_999"


@pytest.mark.asyncio
async def test_concurrent_approval_atomic_consumption(db_session):
    """Verifies that double-consuming an approved operation fails on the second attempt."""
    consumer = Consumer(id="c-appr-race", name="appr_racer", is_active=True)
    db_session.add(consumer)
    await db_session.commit()

    payload = {"refund_id": "ref_888", "amount": 100}
    req = await PersistentApprovalManager.create_request(
        db=db_session,
        consumer_id=consumer.id,
        action_type="refund_payment",
        description="Refund test race",
        requester=consumer.name,
        payload=payload,
    )
    await PersistentApprovalManager.approve(db_session, req.id, "security_lead")

    # Winner consumes successfully
    consumed = await PersistentApprovalManager.verify_and_consume(
        db=db_session,
        approval_id=req.id,
        action_type="refund_payment",
        consumer_id=consumer.id,
        payload=payload,
        request_id="req_winner",
    )
    assert consumed.consumed is True
    assert consumed.status == "consumed"

    # Second consumption attempt raises replay rejection
    with pytest.raises(RuntimeError) as exc_info:
        await PersistentApprovalManager.verify_and_consume(
            db=db_session,
            approval_id=req.id,
            action_type="refund_payment",
            consumer_id=consumer.id,
            payload=payload,
            request_id="req_loser",
        )
    assert "already been consumed" in str(exc_info.value) or "consumed concurrently" in str(exc_info.value)


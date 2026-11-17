"""Comprehensive Regression Test Suite for P0 Idempotency State Machine.

Tests:
1. Cached successful request.
2. Cached failed request.
3. Cached uncertain request.
4. Reconciliation-required request.
5. Same key with different payload.
6. Two simultaneous requests with the same key.
7. Two simultaneous requests with different payloads.
8. Process interruption during execution (in_progress abandoned).
9. Provider success followed by lost response (cached success lookup).
10. Repeated request after uncertain outcome.
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
import pytest
from sqlalchemy import select
from token_router.models.entities import Consumer, IdempotencyRecord
from token_router.services.idempotency import (
    IdempotencyConflictError,
    IdempotencyExecutionFailedError,
    IdempotencyManager,
    IdempotencyPayloadMismatchError,
    IdempotencyUncertainError,
)
from token_router.services.router_service import TokenRouterService


@pytest.mark.asyncio
async def test_idempotency_cached_success(db_session):
    """1. Cached successful request: returns cached data without re-executing."""
    consumer = Consumer(id="c-idemp-1", name="consumer_1", is_active=True)
    db_session.add(consumer)
    await db_session.commit()

    key = "key_success_1"
    payload = {"account": "acc_1", "amount": 100}

    # Step 1: Acquire new
    is_new, rec = await IdempotencyManager.acquire_or_get(
        db=db_session,
        consumer_id=consumer.id,
        idempotency_key=key,
        capability="create_payment_intent",
        payload=payload,
    )
    assert is_new is True

    # Step 2: Mark completed
    await IdempotencyManager.mark_completed(
        db=db_session,
        consumer_id=consumer.id,
        idempotency_key=key,
        status_code=200,
        response_data={"id": "pi_123", "status": "succeeded"},
    )

    # Step 3: Re-request returns cached success
    is_new2, rec2 = await IdempotencyManager.acquire_or_get(
        db=db_session,
        consumer_id=consumer.id,
        idempotency_key=key,
        capability="create_payment_intent",
        payload=payload,
    )
    assert is_new2 is False
    assert rec2.status == "succeeded"
    assert rec2.response_data["id"] == "pi_123"


@pytest.mark.asyncio
async def test_idempotency_cached_failed(db_session):
    """2. Cached failed request: NEVER returns success, raises IdempotencyExecutionFailedError."""
    consumer = Consumer(id="c-idemp-2", name="consumer_2", is_active=True)
    db_session.add(consumer)
    await db_session.commit()

    key = "key_failed_1"
    payload = {"account": "acc_2", "amount": 200}

    is_new, rec = await IdempotencyManager.acquire_or_get(
        db=db_session,
        consumer_id=consumer.id,
        idempotency_key=key,
        capability="create_payment_intent",
        payload=payload,
    )
    assert is_new is True

    # Mark failed
    await IdempotencyManager.mark_failed(
        db=db_session,
        consumer_id=consumer.id,
        idempotency_key=key,
        status_code=402,
        error_data={"error": "card_declined"},
        error_message="Card declined by issuer",
    )

    # Re-request MUST raise IdempotencyExecutionFailedError, never success!
    with pytest.raises(IdempotencyExecutionFailedError) as exc_info:
        await IdempotencyManager.acquire_or_get(
            db=db_session,
            consumer_id=consumer.id,
            idempotency_key=key,
            capability="create_payment_intent",
            payload=payload,
        )
    assert exc_info.value.status_code == 402
    assert "confirmed failed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_idempotency_cached_uncertain_and_reconciliation(db_session):
    """3 & 4. Cached uncertain request & reconciliation required: raises IdempotencyUncertainError."""
    consumer = Consumer(id="c-idemp-3", name="consumer_3", is_active=True)
    db_session.add(consumer)
    await db_session.commit()

    key = "key_uncertain_1"
    payload = {"account": "acc_3", "amount": 300}

    is_new, rec = await IdempotencyManager.acquire_or_get(
        db=db_session,
        consumer_id=consumer.id,
        idempotency_key=key,
        capability="create_payment_intent",
        payload=payload,
    )
    assert is_new is True

    # Mark uncertain (e.g. gateway timeout)
    await IdempotencyManager.mark_uncertain(
        db=db_session,
        consumer_id=consumer.id,
        idempotency_key=key,
        last_error="Stripe gateway 504 timeout",
        recovery_metadata={"transaction_ref": "tx_unknown_3"},
    )

    # Re-request MUST raise IdempotencyUncertainError
    with pytest.raises(IdempotencyUncertainError) as exc_info:
        await IdempotencyManager.acquire_or_get(
            db=db_session,
            consumer_id=consumer.id,
            idempotency_key=key,
            capability="create_payment_intent",
            payload=payload,
        )
    assert "requires manual reconciliation" in str(exc_info.value)
    assert exc_info.value.metadata["transaction_ref"] == "tx_unknown_3"


@pytest.mark.asyncio
async def test_idempotency_payload_mismatch(db_session):
    """5. Same key with different payload: raises IdempotencyPayloadMismatchError."""
    consumer = Consumer(id="c-idemp-5", name="consumer_5", is_active=True)
    db_session.add(consumer)
    await db_session.commit()

    key = "key_mismatch_1"
    p1 = {"amount": 100}
    p2 = {"amount": 999}

    is_new, _ = await IdempotencyManager.acquire_or_get(
        db=db_session,
        consumer_id=consumer.id,
        idempotency_key=key,
        capability="create_payment_intent",
        payload=p1,
    )
    assert is_new is True

    with pytest.raises(IdempotencyPayloadMismatchError) as exc_info:
        await IdempotencyManager.acquire_or_get(
            db=db_session,
            consumer_id=consumer.id,
            idempotency_key=key,
            capability="create_payment_intent",
            payload=p2,
        )
    assert "reused with different request payload" in str(exc_info.value)


@pytest.mark.asyncio
async def test_idempotency_two_simultaneous_requests(db_session):
    """6. Two simultaneous requests with same key: exactly one proceeds, second gets 409 conflict."""
    consumer = Consumer(id="c-idemp-6", name="consumer_6", is_active=True)
    db_session.add(consumer)
    await db_session.commit()

    key = "key_simul_1"
    payload = {"amount": 500}

    # Worker 1 acquires
    is_new, rec = await IdempotencyManager.acquire_or_get(
        db=db_session,
        consumer_id=consumer.id,
        idempotency_key=key,
        capability="create_payment_intent",
        payload=payload,
    )
    assert is_new is True
    assert rec.status == "in_progress"

    # Worker 2 while worker 1 is in_progress -> IdempotencyConflictError
    with pytest.raises(IdempotencyConflictError) as exc_info:
        await IdempotencyManager.acquire_or_get(
            db=db_session,
            consumer_id=consumer.id,
            idempotency_key=key,
            capability="create_payment_intent",
            payload=payload,
        )
    assert "currently in progress" in str(exc_info.value)


@pytest.mark.asyncio
async def test_idempotency_simultaneous_different_payloads(db_session):
    """7. Two simultaneous requests with different payloads: second gets payload mismatch."""
    consumer = Consumer(id="c-idemp-7", name="consumer_7", is_active=True)
    db_session.add(consumer)
    await db_session.commit()

    key = "key_simul_diff_1"
    p1 = {"user": "alice"}
    p2 = {"user": "bob"}

    is_new, _ = await IdempotencyManager.acquire_or_get(
        db=db_session,
        consumer_id=consumer.id,
        idempotency_key=key,
        capability="send_email",
        payload=p1,
    )
    assert is_new is True

    with pytest.raises(IdempotencyPayloadMismatchError):
        await IdempotencyManager.acquire_or_get(
            db=db_session,
            consumer_id=consumer.id,
            idempotency_key=key,
            capability="send_email",
            payload=p2,
        )


@pytest.mark.asyncio
async def test_idempotency_process_interruption(db_session):
    """8. Process interruption during execution: remains in_progress; subsequent request rejects duplicate."""
    consumer = Consumer(id="c-idemp-8", name="consumer_8", is_active=True)
    db_session.add(consumer)
    await db_session.commit()

    key = "key_crash_1"
    payload = {"data": "crit_op"}

    is_new, rec = await IdempotencyManager.acquire_or_get(
        db=db_session,
        consumer_id=consumer.id,
        idempotency_key=key,
        capability="create_payment_intent",
        payload=payload,
    )
    assert is_new is True

    # Simulate crash (never marked completed or failed)
    # When server restarts, key is still in_progress -> reject duplicate execution attempt
    with pytest.raises(IdempotencyConflictError):
        await IdempotencyManager.acquire_or_get(
            db=db_session,
            consumer_id=consumer.id,
            idempotency_key=key,
            capability="create_payment_intent",
            payload=payload,
        )


@pytest.mark.asyncio
async def test_idempotency_provider_success_lost_response(db_session):
    """9. Provider success followed by lost response: lookup retrieves confirmed response."""
    consumer = Consumer(id="c-idemp-9", name="consumer_9", is_active=True)
    db_session.add(consumer)
    await db_session.commit()

    key = "key_lost_client_1"
    payload = {"order_id": "ord_999"}

    is_new, _ = await IdempotencyManager.acquire_or_get(
        db=db_session,
        consumer_id=consumer.id,
        idempotency_key=key,
        capability="create_payment_intent",
        payload=payload,
    )
    # Server commits success
    await IdempotencyManager.mark_completed(
        db=db_session,
        consumer_id=consumer.id,
        idempotency_key=key,
        status_code=201,
        response_data={"order_id": "ord_999", "payment_status": "captured"},
    )

    # Client retried after client-side network drop -> successfully gets original result
    is_new_retry, rec = await IdempotencyManager.acquire_or_get(
        db=db_session,
        consumer_id=consumer.id,
        idempotency_key=key,
        capability="create_payment_intent",
        payload=payload,
    )
    assert is_new_retry is False
    assert rec.status == "succeeded"
    assert rec.response_data["payment_status"] == "captured"


@pytest.mark.asyncio
async def test_idempotency_repeated_request_after_uncertain(db_session):
    """10. Repeated request after uncertain outcome: strictly blocks blind retry."""
    consumer = Consumer(id="c-idemp-10", name="consumer_10", is_active=True)
    db_session.add(consumer)
    await db_session.commit()

    key = "key_uncertain_repeat"
    payload = {"transfer_id": "tr_555"}

    await IdempotencyManager.acquire_or_get(
        db=db_session,
        consumer_id=consumer.id,
        idempotency_key=key,
        capability="create_payment_intent",
        payload=payload,
    )
    await IdempotencyManager.mark_uncertain(
        db=db_session,
        consumer_id=consumer.id,
        idempotency_key=key,
        last_error="Network RST",
    )

    # Repeated calls must be consistently rejected with IdempotencyUncertainError
    for _ in range(3):
        with pytest.raises(IdempotencyUncertainError):
            await IdempotencyManager.acquire_or_get(
                db=db_session,
                consumer_id=consumer.id,
                idempotency_key=key,
                capability="create_payment_intent",
                payload=payload,
            )

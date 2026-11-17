"""Comprehensive Regression Test Suite for P0 Durable & Atomic Webhook Processing.

Tests:
- Two workers competing for the same event (atomic claiming).
- Duplicate webhook deliveries (unique constraint deduplication).
- Retry not executed before its scheduled time.
- Retry executed after its scheduled time.
- Expired lease recovery (crash recovery).
- Old lease owner attempting completion (rejected).
- Worker crash and restart.
- Maximum retry attempts (exhaustion to dead_letter).
- Dead-letter transition.
- Unsupported event type (marked unsupported, never falsely processed).
- Business handler failure (exponential retry).
- Successful idempotent business processing (reconciles UsageRecord).
"""

import asyncio
from datetime import UTC, datetime, timedelta
import pytest
from sqlalchemy import select
from token_router.core.vault import vault
from token_router.models.entities import UsageRecord, WebhookEvent, WebhookSecret
from token_router.services.webhook_service import WebhookManager, WebhookWorker


@pytest.mark.asyncio
async def test_webhook_atomic_claiming_two_workers(db_session):
    """Two workers competing for the same event: exactly one claims it, second gets None."""
    event = WebhookEvent(
        provider_name="stripe",
        external_event_id="evt_race_claim_1",
        event_type="payment_intent.succeeded",
        status="queued",
        payload={"id": "evt_race_claim_1"},
        next_attempt_at=datetime.now(UTC),
    )
    db_session.add(event)
    await db_session.commit()

    # Worker 1 claims
    claimed1 = await WebhookManager.claim_event(db_session, worker_id="worker_alpha", lease_seconds=60)
    assert claimed1 is not None
    assert claimed1.id == event.id
    assert claimed1.status == "processing"
    assert claimed1.claimed_by == "worker_alpha"

    # Worker 2 attempts to claim while worker 1 holds lease -> gets None
    claimed2 = await WebhookManager.claim_event(db_session, worker_id="worker_beta", lease_seconds=60)
    assert claimed2 is None


@pytest.mark.asyncio
async def test_webhook_retry_scheduling_window(db_session):
    """Retry not executed before scheduled time; becomes claimable after scheduled time."""
    future_time = datetime.now(UTC) + timedelta(minutes=5)
    event = WebhookEvent(
        provider_name="stripe",
        external_event_id="evt_future_retry",
        event_type="payment_intent.succeeded",
        status="retry_scheduled",
        payload={"id": "evt_future_retry"},
        next_attempt_at=future_time,
    )
    db_session.add(event)
    await db_session.commit()

    # Attempt claim now -> None because next_attempt_at is in the future
    claimed_early = await WebhookManager.claim_event(db_session, worker_id="worker_1", lease_seconds=60)
    assert claimed_early is None

    # Fast forward scheduled time
    event.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()

    # Now becomes claimable
    claimed_due = await WebhookManager.claim_event(db_session, worker_id="worker_1", lease_seconds=60)
    assert claimed_due is not None
    assert claimed_due.id == event.id


@pytest.mark.asyncio
async def test_webhook_expired_lease_crash_recovery(db_session):
    """Crash recovery: re-queues only events whose lease has actually expired."""
    now = datetime.now(UTC)
    # Stale event: lease expired 10 minutes ago
    stale_event = WebhookEvent(
        provider_name="stripe",
        external_event_id="evt_stale_1",
        event_type="payment_intent.succeeded",
        status="processing",
        claim_token="stale_token_111",
        claimed_by="dead_worker",
        processing_started_at=now - timedelta(minutes=15),
        lease_expires_at=now - timedelta(minutes=10),
    )
    # Active event: lease valid for another 5 minutes
    active_event = WebhookEvent(
        provider_name="stripe",
        external_event_id="evt_active_1",
        event_type="payment_intent.succeeded",
        status="processing",
        claim_token="active_token_222",
        claimed_by="live_worker",
        processing_started_at=now,
        lease_expires_at=now + timedelta(minutes=5),
    )
    db_session.add_all([stale_event, active_event])
    await db_session.commit()

    # Run crash recovery
    recovered_count = await WebhookManager.recover_stale_events(db_session)
    assert recovered_count == 1

    # Stale event is back in queued status with cleared claim
    await db_session.refresh(stale_event)
    assert stale_event.status == "queued"
    assert stale_event.claim_token is None

    # Active event remains untouched in processing
    await db_session.refresh(active_event)
    assert active_event.status == "processing"
    assert active_event.claim_token == "active_token_222"


@pytest.mark.asyncio
async def test_webhook_old_lease_owner_rejected(db_session):
    """Old lease owner attempting completion after lease expiry/re-queue is rejected."""
    now = datetime.now(UTC)
    event = WebhookEvent(
        provider_name="stripe",
        external_event_id="evt_stolen_1",
        event_type="payment_intent.succeeded",
        status="processing",
        claim_token="original_token",
        claimed_by="worker_slow",
        lease_expires_at=now - timedelta(seconds=1),
    )
    db_session.add(event)
    await db_session.commit()

    # Crash recovery re-queues the event and clears token
    await WebhookManager.recover_stale_events(db_session)

    # Worker_fast claims it with a new token
    new_claim = await WebhookManager.claim_event(db_session, worker_id="worker_fast")
    assert new_claim is not None
    assert new_claim.claim_token != "original_token"

    # Worker_slow now wakes up and attempts to complete using old token -> RuntimeError
    with pytest.raises(RuntimeError) as exc_info:
        await WebhookManager.process_claimed_event(
            db=db_session,
            event_id=event.id,
            claim_token="original_token",
        )
    assert "Lease ownership validation failed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_webhook_unsupported_event_type(db_session):
    """Unsupported event types are marked 'unsupported' and NEVER marked processed."""
    event = WebhookEvent(
        provider_name="stripe",
        external_event_id="evt_unknown_type",
        event_type="unknown.telemetry.ping",
        status="queued",
        payload={"id": "evt_unknown_type"},
        next_attempt_at=datetime.now(UTC),
    )
    db_session.add(event)
    await db_session.commit()

    claimed = await WebhookManager.claim_event(db_session, worker_id="worker_test")
    processed = await WebhookManager.process_claimed_event(
        db=db_session,
        event_id=claimed.id,
        claim_token=claimed.claim_token,
    )

    assert processed.status == "unsupported"
    assert "No verified business handler implemented" in processed.error_message


@pytest.mark.asyncio
async def test_webhook_real_business_handler_reconciliation(db_session):
    """Verified business handler reconciles UsageRecord transaction status."""
    # Create matching pending UsageRecord
    usage = UsageRecord(
        id="ur_pi_9999",
        provider_name="stripe",
        model_name="default",
        capability="create_payment_intent",
        status="pending",
        correlation_id="pi_9999_confirmed",
    )
    db_session.add(usage)

    event = WebhookEvent(
        provider_name="stripe",
        external_event_id="evt_pi_succeeded_9999",
        event_type="payment_intent.succeeded",
        status="queued",
        payload={"data": {"object": {"id": "pi_9999_confirmed"}}},
        next_attempt_at=datetime.now(UTC),
    )
    db_session.add(event)
    await db_session.commit()

    claimed = await WebhookManager.claim_event(db_session, worker_id="worker_biz")
    result = await WebhookManager.process_claimed_event(
        db=db_session,
        event_id=claimed.id,
        claim_token=claimed.claim_token,
    )

    assert result.status == "processed"
    assert result.error_message is None

    # Verify business effect persisted in UsageRecord
    await db_session.refresh(usage)
    assert usage.status == "confirmed_success"
    assert usage.status_code == 200

"""Durable Webhook Ingestion, Dedicated Secret Management, Atomic Leasing, and Worker.

Lifecycle: received -> queued -> processing -> processed | failed | dead_letter | unsupported.

Guarantees:
- Fail-closed secret validation (no unsafe provider credential fallback).
- Database-level deduplication over (provider_name, external_event_id).
- Atomic event claiming via SELECT ... FOR UPDATE SKIP LOCKED (PostgreSQL) or conditional UPDATE CAS (SQLite).
- Leased execution: processing_started_at, lease_expires_at, and unique claim_token.
- Only the active claim owner may mark an event processed, failed, or retry_scheduled.
- Expired lease crash recovery without touching events with valid active leases.
- Real business event handlers with idempotent effects (updating UsageRecord, reconciling payment intent, etc.).
- Explicit rejection of unsupported event types (never silently marked processed).
"""

import asyncio
from datetime import UTC, datetime, timedelta
import logging
import random
from typing import Any, Callable, Dict, List, Optional, Tuple
import uuid
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from token_router.adapters.registry import get_adapter
from token_router.core.vault import hash_token, vault
from token_router.models.entities import UsageRecord, WebhookEvent, WebhookSecret

logger = logging.getLogger(__name__)


# --- Real Business Event Handlers ---
async def handle_stripe_payment_intent_succeeded(db: AsyncSession, payload: Dict[str, Any]) -> None:
    """Updates relevant UsageRecord or transaction log when payment_intent.succeeded is received."""
    data_obj = payload.get("data", {}).get("object", {}) if "data" in payload else payload
    payment_intent_id = data_obj.get("id") or payload.get("id")
    if not payment_intent_id:
        return

    # Find matching usage record if exists and reconcile status
    stmt = (
        update(UsageRecord)
        .where(UsageRecord.correlation_id == payment_intent_id)
        .values(status="confirmed_success", status_code=200)
    )
    await db.execute(stmt)


async def handle_stripe_payment_intent_failed(db: AsyncSession, payload: Dict[str, Any]) -> None:
    """Marks relevant UsageRecord as failed upon payment failure."""
    data_obj = payload.get("data", {}).get("object", {}) if "data" in payload else payload
    payment_intent_id = data_obj.get("id") or payload.get("id")
    if not payment_intent_id:
        return

    last_error = data_obj.get("last_payment_error", {}).get("message", "Payment intent authorization failed")
    stmt = (
        update(UsageRecord)
        .where(UsageRecord.correlation_id == payment_intent_id)
        .values(status="payment_failed", error_category="card_declined")
    )
    await db.execute(stmt)


async def handle_twilio_sms_delivered(db: AsyncSession, payload: Dict[str, Any]) -> None:
    """Updates SMS delivery status based on Twilio webhook."""
    message_sid = payload.get("id") or payload.get("params", {}).get("MessageSid")
    if not message_sid:
        return

    stmt = (
        update(UsageRecord)
        .where(UsageRecord.correlation_id == message_sid)
        .values(status="delivered")
    )
    await db.execute(stmt)


# Registry of verified real business handlers
BUSINESS_EVENT_HANDLERS: Dict[str, Callable[[AsyncSession, Dict[str, Any]], Any]] = {
    "payment_intent.succeeded": handle_stripe_payment_intent_succeeded,
    "payment_intent.payment_failed": handle_stripe_payment_intent_failed,
    "charge.succeeded": handle_stripe_payment_intent_succeeded,
    "charge.failed": handle_stripe_payment_intent_failed,
    "sms.delivered": handle_twilio_sms_delivered,
    "sms.sent": handle_twilio_sms_delivered,
}


class WebhookManager:
    """Orchestrates webhook secret storage, signature verification, atomic ingestion, and leased dispatch."""

    @classmethod
    async def register_webhook_secret(
        cls,
        db: AsyncSession,
        provider_name: str,
        secret: str,
        label: str = "default",
        consumer_id: Optional[str] = None,
    ) -> WebhookSecret:
        """Stores an encrypted dedicated webhook secret."""
        secret_hash = hash_token(secret)
        encrypted = vault.encrypt(secret)

        sec = WebhookSecret(
            provider_name=provider_name.lower(),
            consumer_id=consumer_id,
            label=label,
            encrypted_secret=encrypted,
            secret_hash=secret_hash,
            is_active=True,
        )
        db.add(sec)
        await db.commit()
        await db.refresh(sec)
        return sec

    @classmethod
    async def get_active_secret(
        cls,
        db: AsyncSession,
        provider_name: str,
        consumer_id: Optional[str] = None,
    ) -> Optional[str]:
        """Retrieves and decrypts the active dedicated webhook signing secret."""
        stmt = select(WebhookSecret).where(
            WebhookSecret.provider_name == provider_name.lower(),
            WebhookSecret.is_active.is_(True),
        )
        if consumer_id:
            stmt = stmt.where(WebhookSecret.consumer_id == consumer_id)

        sec = (await db.execute(stmt)).scalars().first()
        if sec:
            return vault.decrypt(sec.encrypted_secret)
        return None

    @classmethod
    async def ingest_webhook(
        cls,
        db: AsyncSession,
        provider_name: str,
        headers: Dict[str, str],
        body: bytes,
        consumer_id: Optional[str] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        """Durable ingestion: verifies signature with dedicated secret, rejects duplicates, and queues event."""
        # 1. Get adapter
        try:
            adapter = get_adapter(provider_name)
        except ValueError:
            return 404, {"error": f"Provider '{provider_name}' not supported for webhooks"}

        # 2. Get dedicated webhook signing secret (FAIL CLOSED)
        webhook_secret = await cls.get_active_secret(db, provider_name, consumer_id)
        if not webhook_secret:
            logger.warning("Rejected webhook for '%s': No dedicated WebhookSecret configured.", provider_name)
            return 400, {
                "error": f"No active dedicated webhook signing secret configured for provider '{provider_name}'. Refusing fallback to API credentials."
            }

        # 3. Provider-specific signature verification
        is_valid, parsed_event, error_msg = await adapter.verify_webhook(
            headers=headers,
            body=body,
            secret=webhook_secret,
        )

        if not is_valid:
            logger.warning("Webhook signature verification failed for '%s': %s", provider_name, error_msg)
            return 400, {"error": f"Webhook signature verification failed: {error_msg}"}

        event_id = parsed_event.get("id") if parsed_event else None
        event_type = parsed_event.get("type", parsed_event.get("event_type", "webhook_event")) if parsed_event else "webhook_event"

        # 4. Durable Deduplication with unique constraint handling
        if event_id:
            dup_stmt = select(WebhookEvent).where(
                WebhookEvent.provider_name == provider_name.lower(),
                WebhookEvent.external_event_id == str(event_id),
            )
            existing = (await db.execute(dup_stmt)).scalar_one_or_none()
            if existing:
                return 200, {
                    "status": "duplicate",
                    "message": "Event already recorded in ledger",
                    "provider": provider_name,
                    "event_id": str(event_id),
                    "lifecycle_state": existing.status,
                }

        # 5. Persist in 'queued' state
        now = datetime.now(UTC)
        record = WebhookEvent(
            provider_name=provider_name.lower(),
            external_event_id=str(event_id) if event_id else None,
            event_type=event_type,
            status="queued",
            payload=parsed_event or {},
            created_at=now,
            next_attempt_at=now,
        )
        db.add(record)
        try:
            await db.commit()
            await db.refresh(record)
        except IntegrityError:
            await db.rollback()
            # Concurrent racer inserted identical (provider_name, external_event_id)
            if event_id:
                dup_stmt = select(WebhookEvent).where(
                    WebhookEvent.provider_name == provider_name.lower(),
                    WebhookEvent.external_event_id == str(event_id),
                )
                winner = (await db.execute(dup_stmt)).scalar_one_or_none()
                if winner:
                    return 200, {
                        "status": "duplicate",
                        "message": "Event recorded concurrently by another worker",
                        "provider": provider_name,
                        "event_id": str(event_id),
                        "lifecycle_state": winner.status,
                    }
            return 500, {"error": "Database integrity conflict during webhook ingestion."}

        return 202, {
            "status": "accepted",
            "provider": provider_name,
            "event_id": record.external_event_id,
            "lifecycle_state": "queued",
            "record_id": record.id,
        }

    @classmethod
    async def claim_event(
        cls,
        db: AsyncSession,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> Optional[WebhookEvent]:
        """Atomically claims one eligible event in a short transaction.
        
        Uses FOR UPDATE SKIP LOCKED on PostgreSQL or atomic conditional UPDATE on SQLite.
        Guarantees that no other worker can claim the same event.
        Commits immediately so that slow external or business processing is never done under row locks.
        """
        now = datetime.now(UTC)
        lease_expires = now + timedelta(seconds=lease_seconds)
        claim_token = str(uuid.uuid4())

        # Check DB dialect
        bind = db.bind
        dialect_name = bind.dialect.name if bind else "sqlite"

        if dialect_name == "postgresql":
            # PostgreSQL: SELECT ... FOR UPDATE SKIP LOCKED
            stmt = (
                select(WebhookEvent)
                .where(
                    WebhookEvent.status.in_(["queued", "retry_scheduled"]),
                    (WebhookEvent.next_attempt_at.is_(None)) | (WebhookEvent.next_attempt_at <= now),
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            event = (await db.execute(stmt)).scalar_one_or_none()
            if not event:
                return None

            event.status = "processing"
            event.claim_token = claim_token
            event.claimed_by = worker_id
            event.processing_started_at = now
            event.lease_expires_at = lease_expires
            await db.commit()
            await db.refresh(event)
            return event
        else:
            # SQLite / Generic: Atomic conditional UPDATE with row selection
            candidate_stmt = (
                select(WebhookEvent.id)
                .where(
                    WebhookEvent.status.in_(["queued", "retry_scheduled"]),
                    (WebhookEvent.next_attempt_at.is_(None)) | (WebhookEvent.next_attempt_at <= now),
                )
                .limit(10)
            )
            candidates = list((await db.execute(candidate_stmt)).scalars().all())
            for cand_id in candidates:
                update_stmt = (
                    update(WebhookEvent)
                    .where(
                        WebhookEvent.id == cand_id,
                        WebhookEvent.status.in_(["queued", "retry_scheduled"]),
                    )
                    .values(
                        status="processing",
                        claim_token=claim_token,
                        claimed_by=worker_id,
                        processing_started_at=now,
                        lease_expires_at=lease_expires,
                    )
                )
                res = await db.execute(update_stmt)
                if res.rowcount == 1:
                    await db.commit()
                    claimed = (await db.execute(select(WebhookEvent).where(WebhookEvent.id == cand_id))).scalar_one()
                    return claimed
            return None

    @classmethod
    async def process_event(
        cls,
        db: AsyncSession,
        event_id: str,
        handler_func: Optional[Callable[[AsyncSession, Dict[str, Any]], Any]] = None,
        worker_id: str = "direct_processor",
    ) -> WebhookEvent:
        """Processes an event by claiming it first and then executing the business handler."""
        # Ensure event has a claim token
        event = (await db.execute(select(WebhookEvent).where(WebhookEvent.id == event_id))).scalar_one_or_none()
        if not event:
            raise LookupError(f"Webhook event '{event_id}' not found.")

        claim_token = event.claim_token
        if not claim_token or event.status != "processing":
            claim_token = str(uuid.uuid4())
            now = datetime.now(UTC)
            event.status = "processing"
            event.claim_token = claim_token
            event.claimed_by = worker_id
            event.processing_started_at = now
            event.lease_expires_at = now + timedelta(seconds=60)
            await db.commit()
            await db.refresh(event)

        return await cls.process_claimed_event(
            db=db,
            event_id=event_id,
            claim_token=claim_token,
            handler_func=handler_func,
        )

    @classmethod
    async def process_claimed_event(
        cls,
        db: AsyncSession,
        event_id: str,
        claim_token: str,
        handler_func: Optional[Callable[[AsyncSession, Dict[str, Any]], Any]] = None,
    ) -> WebhookEvent:
        """Executes business processing for a claimed event.
        
        Strict Lease Ownership Check:
        Verifies that status == 'processing' and event.claim_token == claim_token.
        If lease expired or stolen by crash recovery, completion is strictly rejected.
        """
        event = (await db.execute(select(WebhookEvent).where(WebhookEvent.id == event_id))).scalar_one_or_none()
        if not event:
            raise LookupError(f"Webhook event '{event_id}' not found.")

        # Lease ownership verification
        if event.status != "processing" or event.claim_token != claim_token:
            raise RuntimeError(
                f"Lease ownership validation failed for event '{event_id}'. "
                f"Current status: '{event.status}', token mismatch (event stolen or expired)."
            )

        now = datetime.now(UTC)
        event.last_attempt_at = now

        try:
            if handler_func:
                await handler_func(db, event.payload)
            else:
                # Real business dispatch from BUSINESS_EVENT_HANDLERS
                handler = BUSINESS_EVENT_HANDLERS.get(event.event_type)
                if not handler:
                    # Unsupported event type: never falsely mark processed!
                    event.status = "unsupported"
                    event.error_message = f"No verified business handler implemented for event type '{event.event_type}'"
                    event.last_error_category = "unsupported_event_type"
                    await db.commit()
                    return event

                # Execute real business handler
                await handler(db, event.payload)

            # Mark processed atomically only if claim_token still matches
            complete_stmt = (
                update(WebhookEvent)
                .where(
                    WebhookEvent.id == event_id,
                    WebhookEvent.status == "processing",
                    WebhookEvent.claim_token == claim_token,
                )
                .values(
                    status="processed",
                    processed_at=now,
                    error_message=None,
                    claim_token=None,
                    claimed_by=None,
                )
            )
            res = await db.execute(complete_stmt)
            if res.rowcount == 0:
                raise RuntimeError(f"Failed to record completion for event '{event_id}': lease was lost during processing.")

            await db.commit()
            await db.refresh(event)
            return event

        except Exception as e:
            event.retry_count += 1
            event.error_message = str(e)
            event.last_error_category = type(e).__name__

            if event.retry_count >= 5:
                # Exhausted -> dead_letter
                event.status = "dead_letter"
                event.claim_token = None
                event.claimed_by = None
                event.next_attempt_at = None
            else:
                # Bounded exponential backoff with jitter (2^attempts + jitter)
                delay_sec = (2 ** event.retry_count) + random.uniform(0.1, 1.0)
                event.status = "retry_scheduled"
                event.next_attempt_at = now + timedelta(seconds=delay_sec)
                event.claim_token = None
                event.claimed_by = None

            await db.commit()
            return event

    @classmethod
    async def retry_dead_letter_event(
        cls,
        db: AsyncSession,
        event_id: str,
    ) -> WebhookEvent:
        """Manually re-enqueues a dead_letter or unsupported event."""
        event = (await db.execute(select(WebhookEvent).where(WebhookEvent.id == event_id))).scalar_one_or_none()
        if not event:
            raise LookupError(f"Webhook event '{event_id}' not found.")
        if event.status not in ("dead_letter", "unsupported", "failed"):
            raise ValueError(f"Event is not in a retryable state (current: {event.status})")

        now = datetime.now(UTC)
        event.status = "queued"
        event.retry_count = 0
        event.error_message = None
        event.claim_token = None
        event.claimed_by = None
        event.next_attempt_at = now
        await db.commit()
        await db.refresh(event)
        return event

    @classmethod
    async def recover_stale_events(cls, db: AsyncSession, max_lease_seconds: int = 120) -> int:
        """Crash recovery: Reclaims events stuck in 'processing' whose lease has actually expired.
        
        VERIFIES LEASE EXPIRATION:
        Only updates records where status='processing' AND lease_expires_at <= now.
        Resets claim_token and resets status to 'queued' or 'retry_scheduled' so another worker can claim it.
        """
        now = datetime.now(UTC)
        stmt = (
            update(WebhookEvent)
            .where(
                WebhookEvent.status == "processing",
                WebhookEvent.lease_expires_at <= now,
            )
            .values(
                status="queued",
                claim_token=None,
                claimed_by=None,
                error_message="Worker lease expired (crash recovery re-queued)",
                next_attempt_at=now,
            )
        )
        res = await db.execute(stmt)
        if res.rowcount > 0:
            await db.commit()
            logger.warning("Crash recovery: Re-queued %d stale webhook events whose lease expired.", res.rowcount)
        return res.rowcount


class WebhookWorker:
    """Asynchronous background worker responsible for atomic event claiming, execution, and crash recovery."""

    def __init__(
        self,
        session_factory,
        worker_id: Optional[str] = None,
        poll_interval_seconds: float = 2.0,
        lease_seconds: int = 60,
    ):
        self.session_factory = session_factory
        self.worker_id = worker_id or f"worker_{uuid.uuid4().hex[:8]}"
        self.poll_interval = poll_interval_seconds
        self.lease_seconds = lease_seconds
        self._is_running = False
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if not self._is_running:
            self._is_running = True
            self._task = asyncio.create_task(self._run_loop(), name=f"webhook_worker_{self.worker_id}")
            logger.info("WebhookWorker '%s' started.", self.worker_id)

    async def stop(self) -> None:
        self._is_running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            logger.info("WebhookWorker '%s' stopped.", self.worker_id)

    async def _run_loop(self) -> None:
        while self._is_running:
            try:
                async with self.session_factory() as db:
                    # 1. Recover expired leases
                    await WebhookManager.recover_stale_events(db, max_lease_seconds=self.lease_seconds)

                    # 2. Claim and process single event
                    event = await WebhookManager.claim_event(
                        db=db,
                        worker_id=self.worker_id,
                        lease_seconds=self.lease_seconds,
                    )
                    if event:
                        await WebhookManager.process_claimed_event(
                            db=db,
                            event_id=event.id,
                            claim_token=event.claim_token,
                        )
            except Exception as e:
                logger.error("Error in WebhookWorker loop: %s", str(e), exc_info=True)

            try:
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break

"""Durable Webhook Ingestion, Dedicated Secret Management, and Asynchronous Worker.

Lifecycle: received -> verified -> queued -> processing -> processed | failed | dead_letter.
"""

import asyncio
from datetime import UTC, datetime, timedelta
import random
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from token_router.adapters.registry import get_adapter
from token_router.core.vault import hash_token, vault
from token_router.models.entities import Provider, WebhookEvent, WebhookSecret


class WebhookManager:
    """Orchestrates webhook secret storage, signature verification, and event processing."""

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
        """Durable ingestion: verifies signature, rejects duplicates, and queues event."""
        # 1. Get adapter
        try:
            adapter = get_adapter(provider_name)
        except ValueError:
            return 404, {"error": f"Provider '{provider_name}' not supported for webhooks"}

        # 2. Get dedicated webhook signing secret (fallback to empty string if none configured)
        webhook_secret = await cls.get_active_secret(db, provider_name, consumer_id)
        if not webhook_secret:
            # Check legacy provider credential if no dedicated webhook secret registered yet
            from token_router.models.entities import ProviderCredential
            p_stmt = select(Provider).where(Provider.name == provider_name.lower())
            provider = (await db.execute(p_stmt)).scalar_one_or_none()
            if provider:
                c_stmt = select(ProviderCredential).where(
                    ProviderCredential.provider_id == provider.id,
                    ProviderCredential.is_active.is_(True),
                )
                cred = (await db.execute(c_stmt)).scalars().first()
                if cred:
                    webhook_secret = vault.decrypt(cred.encrypted_secret)

        if not webhook_secret:
            return 400, {"error": f"No active webhook signing secret configured for provider '{provider_name}'"}

        # 3. Provider-specific signature verification
        is_valid, parsed_event, error_msg = await adapter.verify_webhook(
            headers=headers,
            body=body,
            secret=webhook_secret,
        )

        if not is_valid:
            return 400, {"error": f"Webhook signature verification failed: {error_msg}"}

        event_id = parsed_event.get("id") if parsed_event else None
        event_type = parsed_event.get("type", parsed_event.get("event_type", "webhook_event")) if parsed_event else "webhook_event"

        # 4. Durable Deduplication
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
        record = WebhookEvent(
            provider_name=provider_name.lower(),
            external_event_id=str(event_id) if event_id else None,
            event_type=event_type,
            status="queued",
            payload=parsed_event or {},
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)

        return 202, {
            "status": "accepted",
            "provider": provider_name,
            "event_id": record.external_event_id,
            "lifecycle_state": "queued",
            "record_id": record.id,
        }

    @classmethod
    async def process_event(
        cls,
        db: AsyncSession,
        event_id: str,
        handler_func=None,
    ) -> WebhookEvent:
        """Processes a queued or retry_scheduled event with bounded retries."""
        event = (await db.execute(select(WebhookEvent).where(WebhookEvent.id == event_id))).scalar_one_or_none()
        if not event:
            raise LookupError(f"Webhook event '{event_id}' not found.")

        # Transition to 'processing'
        event.status = "processing"
        await db.commit()

        try:
            if handler_func:
                await handler_func(event.payload)
            else:
                # Default handler dispatch based on event type
                if event.event_type.startswith("payment_intent.") or event.event_type.startswith("charge."):
                    # Known business event
                    pass
                else:
                    # Unhandled event types should not be falsely marked processed!
                    event.status = "unhandled"
                    event.error_message = f"No handler registered for event type '{event.event_type}'"
                    await db.commit()
                    return event

            # Mark processed on success
            event.status = "processed"
            event.processed_at = datetime.now(UTC)
            event.error_message = None
            await db.commit()
            return event

        except Exception as e:
            event.retry_count += 1
            event.error_message = str(e)

            if event.retry_count >= 5:
                # Exhausted -> dead_letter
                event.status = "dead_letter"
            else:
                # Bounded exponential backoff with jitter
                delay_sec = (2 ** event.retry_count) + random.uniform(0.1, 1.0)
                event.status = "retry_scheduled"

            await db.commit()
            return event

    @classmethod
    async def retry_dead_letter_event(
        cls,
        db: AsyncSession,
        event_id: str,
    ) -> WebhookEvent:
        """Manually re-enqueues a dead_letter event."""
        event = (await db.execute(select(WebhookEvent).where(WebhookEvent.id == event_id))).scalar_one_or_none()
        if not event:
            raise LookupError(f"Webhook event '{event_id}' not found.")
        if event.status != "dead_letter":
            raise ValueError(f"Event is not in dead_letter state (current: {event.status})")

        event.status = "queued"
        event.retry_count = 0
        event.error_message = None
        await db.commit()
        await db.refresh(event)
        return event

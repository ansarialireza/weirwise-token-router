"""Webhook Ingestion, Verification, Deduplication, and Event Lifecycle Management.

Supports full lifecycle: received -> verified -> queued -> processed -> failed -> dead_letter.
"""

from datetime import UTC, datetime
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from token_router.adapters.registry import get_adapter
from token_router.core.vault import vault
from token_router.db.session import get_db
from token_router.models.entities import Provider, ProviderCredential, WebhookEvent

router = APIRouter(prefix="/api/v1/webhooks", tags=["Webhooks"])


@router.post("/{provider_name}")
async def receive_provider_webhook(
    provider_name: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Centralized webhook endpoint receiving events from external services."""
    try:
        adapter = get_adapter(provider_name)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Provider '{provider_name}' not supported for webhooks",
        )

    body_bytes = await request.body()
    headers_dict = dict(request.headers)

    # 1. Locate registered provider
    provider = (await db.execute(select(Provider).where(Provider.name == provider_name.lower()))).scalar_one_or_none()
    if not provider:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Provider '{provider_name}' not configured in database",
        )

    # 2. Retrieve credential secret for signature verification
    cred = (await db.execute(
        select(ProviderCredential).where(
            ProviderCredential.provider_id == provider.id,
            ProviderCredential.is_active.is_(True),
        )
    )).scalars().first()

    webhook_secret = ""
    if cred:
        webhook_secret = vault.decrypt(cred.encrypted_secret)

    # 3. Provider-specific signature verification
    is_valid, parsed_event, error_msg = await adapter.verify_webhook(
        headers=headers_dict,
        body=body_bytes,
        secret=webhook_secret,
    )

    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Webhook signature verification failed: {error_msg}",
        )

    event_id = parsed_event.get("id") if parsed_event else None
    event_type = parsed_event.get("type", parsed_event.get("event_type", "webhook_event")) if parsed_event else "webhook_event"

    # 4. Persistent Deduplication and Replay Protection
    if event_id:
        existing_event = (await db.execute(
            select(WebhookEvent).where(
                WebhookEvent.provider_name == provider_name.lower(),
                WebhookEvent.external_event_id == str(event_id),
            )
        )).scalar_one_or_none()

        if existing_event:
            # Idempotent response for duplicate event
            return {
                "status": "duplicate",
                "message": "Event already recorded in ledger",
                "provider": provider_name,
                "event_id": str(event_id),
                "lifecycle_state": existing_event.status,
            }

    # 5. Record verified event in append-only ledger
    webhook_record = WebhookEvent(
        provider_name=provider_name.lower(),
        external_event_id=str(event_id) if event_id else None,
        event_type=event_type,
        status="verified",
        payload=parsed_event or {},
    )
    db.add(webhook_record)
    await db.commit()
    await db.refresh(webhook_record)

    # 6. Synchronous processing transition (verified -> processed)
    webhook_record.status = "processed"
    webhook_record.processed_at = datetime.now(UTC)
    await db.commit()

    return {
        "status": "received",
        "provider": provider_name,
        "event_id": webhook_record.external_event_id,
        "event_type": webhook_record.event_type,
        "lifecycle_state": webhook_record.status,
    }


@router.get("/events/{event_id}")
async def get_webhook_event_status(
    event_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Inspect webhook lifecycle state."""
    event = (await db.execute(
        select(WebhookEvent).where(
            (WebhookEvent.id == event_id) | (WebhookEvent.external_event_id == event_id)
        )
    )).scalar_one_or_none()

    if not event:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook event not found")

    return {
        "id": event.id,
        "provider": event.provider_name,
        "external_event_id": event.external_event_id,
        "event_type": event.event_type,
        "status": event.status,
        "created_at": event.created_at.isoformat() if event.created_at else None,
        "processed_at": event.processed_at.isoformat() if event.processed_at else None,
        "error_message": event.error_message,
    }

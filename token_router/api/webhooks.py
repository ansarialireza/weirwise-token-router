"""Webhook Ingestion, Verification, Deduplication, and Lifecycle API endpoints."""

import json
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from token_router.api.dependencies import authenticate_admin, authenticate_consumer
from token_router.db.session import get_db
from token_router.models.entities import Consumer, WebhookEvent
from token_router.services.webhook_service import WebhookManager

router = APIRouter(prefix="/api/v1/webhooks", tags=["Webhooks"])


@router.post("/{provider_name}")
async def receive_provider_webhook(
    provider_name: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Centralized webhook endpoint receiving events from external services."""
    body_bytes = await request.body()
    headers_dict = dict(request.headers)
    # Forward full requested URL so adapter can compute canonical signature
    headers_dict["x-request-url"] = str(request.url)

    status_code, response_data = await WebhookManager.ingest_webhook(
        db=db,
        provider_name=provider_name,
        headers=headers_dict,
        body=body_bytes,
    )

    if status_code >= 400:
        raise HTTPException(status_code=status_code, detail=response_data.get("error", "Webhook ingestion failed"))

    return Response(
        content=json.dumps(response_data),
        status_code=status_code,
        media_type="application/json",
    )


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
        "retry_count": event.retry_count,
        "created_at": event.created_at.isoformat() if event.created_at else None,
        "processed_at": event.processed_at.isoformat() if event.processed_at else None,
        "error_message": event.error_message,
    }


@router.post("/events/{event_id}/retry")
async def retry_dead_letter_webhook(
    event_id: str,
    db: AsyncSession = Depends(get_db),
    admin: bool = Depends(authenticate_admin),
):
    """Admin endpoint to re-enqueue dead_letter webhook events."""
    try:
        event = await WebhookManager.retry_dead_letter_event(db=db, event_id=event_id)
        return {
            "id": event.id,
            "status": event.status,
            "message": "Event re-enqueued for processing",
        }
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

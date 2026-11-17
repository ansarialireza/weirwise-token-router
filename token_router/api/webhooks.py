"""Webhook Ingestion, Verification, Deduplication, and Lifecycle API endpoints.

Handles reverse-proxy trust validation and canonical public URL reconstruction
for strict signature validation (e.g. Twilio, Stripe).
"""

import json
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from token_router.api.dependencies import authenticate_admin, authenticate_consumer
from token_router.core.config import settings
from token_router.db.session import get_db
from token_router.models.entities import Consumer, WebhookEvent
from token_router.services.webhook_service import WebhookManager

router = APIRouter(prefix="/api/v1/webhooks", tags=["Webhooks"])


def resolve_canonical_request_url(request: Request) -> str:
    """Safely reconstructs the canonical public URL for webhook signature verification.
    
    If settings.PUBLIC_WEBHOOK_URL_BASE is set, it overrides the scheme and authority.
    Only if the immediate client is in settings.TRUSTED_PROXIES will forwarded headers
    (X-Forwarded-Proto, X-Forwarded-Host, X-Forwarded-Port) be respected.
    Otherwise, the direct request URL is used.
    """
    if settings.PUBLIC_WEBHOOK_URL_BASE:
        base = settings.PUBLIC_WEBHOOK_URL_BASE.rstrip("/")
        path = request.url.path
        query = f"?{request.url.query}" if request.url.query else ""
        return f"{base}{path}{query}"

    client_host = request.client.host if request.client else "127.0.0.1"
    is_trusted_proxy = client_host in settings.TRUSTED_PROXIES

    if is_trusted_proxy:
        scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
        host = request.headers.get("x-forwarded-host") or request.url.netloc
        path = request.url.path
        query = f"?{request.url.query}" if request.url.query else ""
        return f"{scheme}://{host}{path}{query}"

    return str(request.url)


@router.post("/{provider_name}")
async def receive_provider_webhook(
    provider_name: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Centralized webhook endpoint receiving events from external services."""
    body_bytes = await request.body()
    headers_dict = dict(request.headers)

    # Establish exact canonical public URL using trusted proxy rules
    canonical_url = resolve_canonical_request_url(request)
    headers_dict["x-canonical-url"] = canonical_url
    headers_dict["x-request-url"] = canonical_url

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
    """Inspect webhook lifecycle state by database ID or external event ID."""
    event = (await db.execute(
        select(WebhookEvent).where(
            (WebhookEvent.id == event_id) | (WebhookEvent.external_event_id == event_id)
        )
    )).scalars().first()

    if not event:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Webhook event '{event_id}' not found")

    return {
        "event_id": event.id,
        "provider": event.provider_name,
        "external_event_id": event.external_event_id,
        "event_type": event.event_type,
        "status": event.status,
        "retry_count": event.retry_count,
        "claim_token": event.claim_token,
        "claimed_by": event.claimed_by,
        "next_attempt_at": event.next_attempt_at.isoformat() if event.next_attempt_at else None,
        "lease_expires_at": event.lease_expires_at.isoformat() if event.lease_expires_at else None,
        "created_at": event.created_at.isoformat(),
        "processed_at": event.processed_at.isoformat() if event.processed_at else None,
        "error_message": event.error_message,
    }


@router.post("/events/{event_id}/retry")
async def retry_dead_letter_webhook(
    event_id: str,
    consumer: Consumer = Depends(authenticate_admin),
    db: AsyncSession = Depends(get_db),
):
    """Admin-only endpoint to safely re-enqueue a dead-lettered webhook."""
    try:
        event = await WebhookManager.retry_dead_letter_event(db, event_id=event_id)
        return {
            "status": "re_queued",
            "event_id": event.id,
            "lifecycle_state": event.status,
            "message": "Event re-queued for processing.",
        }
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

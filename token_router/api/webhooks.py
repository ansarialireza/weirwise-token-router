"""Webhook Ingestion, Verification, and Event Normalization Gateway.

Provides centralized webhook receiver for third-party providers.
"""

from typing import Any, Dict, Optional, Tuple
from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from token_router.adapters.registry import get_adapter
from token_router.core.vault import vault
from token_router.db.session import get_db
from token_router.models.entities import Provider, ProviderCredential

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

    # Find registered provider and credential secret
    provider = (await db.execute(select(Provider).where(Provider.name == provider_name.lower()))).scalar_one_or_none()
    if not provider:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Provider '{provider_name}' not configured in database",
        )

    # In Stripe/Twilio, webhooks use signing secret stored in provider credential
    cred = (await db.execute(
        select(ProviderCredential).where(
            ProviderCredential.provider_id == provider.id,
            ProviderCredential.is_active.is_(True),
        )
    )).scalars().first()

    webhook_secret = ""
    if cred:
        webhook_secret = vault.decrypt(cred.encrypted_secret)

    is_valid, parsed_event, error_msg = await adapter.verify_webhook(
        headers=headers_dict,
        body=body_bytes,
        secret=webhook_secret,
    )

    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Webhook verification failed: {error_msg}",
        )

    return {
        "status": "received",
        "provider": provider_name,
        "event_id": parsed_event.get("id") if parsed_event else None,
        "event_type": parsed_event.get("type") if parsed_event else "webhook_event",
    }

import time
from decimal import Decimal
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from token_router.adapters.registry import get_adapter
from token_router.api.dependencies import authenticate_consumer
from token_router.core.capabilities import CapabilityRegistry
from token_router.core.config import settings
from token_router.core.vault import vault
from token_router.db.session import get_db
from token_router.models.entities import Consumer, Provider
from token_router.schemas.contracts import RouteRequest, RouteResponse
from token_router.services.router_service import TokenRouterService
from token_router.services.routing_engine import RoutingEngine

router = APIRouter(prefix="/api/v1", tags=["Routing & Capabilities"])


@router.get("/discover")
async def discover_system_capabilities(
    consumer: Optional[Consumer] = Depends(authenticate_consumer),
    db: AsyncSession = Depends(get_db),
):
    """Dynamic discovery endpoint for Agents to inspect available providers, services, models, and health."""
    return await TokenRouterService.discover_capabilities(db=db, consumer=consumer)


@router.get("/services")
async def list_supported_services():
    """List distinct AI services (chat, embeddings, text-to-speech, speech-to-text, email)."""
    return [s.model_dump() for s in CapabilityRegistry.list_services()]


@router.get("/diagnostics")
async def run_diagnostics(
    db: AsyncSession = Depends(get_db),
):
    """Automated self-diagnostics for Agent infrastructure troubleshooting."""
    return await TokenRouterService.run_diagnostics(db=db)


@router.post("/route", response_model=RouteResponse)
async def route_request(
    payload: RouteRequest,
    consumer: Consumer = Depends(authenticate_consumer),
    db: AsyncSession = Depends(get_db),
) -> RouteResponse:
    """Executes an intelligent, credential-routed request with bounded failover."""
    try:
        result = await TokenRouterService.execute_route(
            db=db,
            consumer=consumer,
            capability=payload.capability,
            model=payload.model,
            request_payload=payload.request,
            provider_name=payload.provider,
            strategy=payload.strategy,
            max_failover_attempts=payload.max_failover_attempts,
            timeout_seconds=payload.timeout_seconds,
        )
        return RouteResponse(**result)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/route/stream")
async def route_stream(
    payload: RouteRequest,
    consumer: Consumer = Depends(authenticate_consumer),
    db: AsyncSession = Depends(get_db),
):
    """Executes an intelligent, credential-routed streaming request."""
    if payload.capability not in consumer.allowed_capabilities:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Consumer '{consumer.name}' is not authorized for capability: {payload.capability}",
        )

    provider_stmt = select(Provider).where(Provider.is_enabled.is_(True))
    if payload.provider:
        provider_stmt = provider_stmt.where(Provider.name == payload.provider.lower())
    provider = (await db.execute(provider_stmt)).scalars().first()

    if not provider:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No enabled provider found matching '{payload.provider or 'any'}'",
        )

    cred = await RoutingEngine.select_credential(db=db, provider=provider, model=payload.model)
    if not cred:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No healthy active credentials available for this provider.",
        )

    adapter = get_adapter(provider.name)
    secret = vault.decrypt(cred.encrypted_secret)
    timeout = payload.timeout_seconds or settings.DEFAULT_TIMEOUT_SECONDS

    async def stream_generator():
        async for chunk in adapter.execute_stream(
            secret=secret,
            capability=payload.capability,
            model=payload.model,
            payload=payload.request,
            timeout=timeout,
        ):
            yield f"{chunk}\n"

    return StreamingResponse(stream_generator(), media_type="text/event-stream")

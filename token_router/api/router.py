import time
import uuid
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from token_router.adapters.registry import get_adapter
from token_router.api.dependencies import authenticate_consumer
from token_router.core.config import settings
from token_router.core.vault import vault
from token_router.db.session import get_db
from token_router.models.entities import Consumer, Provider, ProviderCredential, UsageRecord
from token_router.schemas.contracts import RouteRequest, RouteResponse
from token_router.services.routing_engine import RoutingEngine

router = APIRouter(prefix="/api/v1", tags=["Routing"])


@router.post("/route", response_model=RouteResponse)
async def route_request(
    payload: RouteRequest,
    consumer: Consumer = Depends(authenticate_consumer),
    db: AsyncSession = Depends(get_db),
) -> RouteResponse:
    """Executes an intelligent, credential-routed request with bounded failover."""
    # 1. Verify consumer permissions
    if payload.capability not in consumer.allowed_capabilities:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Consumer '{consumer.name}' is not authorized for capability: {payload.capability}",
        )

    # 2. Identify provider candidates
    provider_stmt = select(Provider).where(Provider.is_enabled.is_(True))
    if payload.provider:
        provider_stmt = provider_stmt.where(Provider.name == payload.provider.lower())
    providers = list((await db.execute(provider_stmt)).scalars().all())

    if not providers:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No enabled provider found matching '{payload.provider or 'any'}'",
        )

    timeout = payload.timeout_seconds or settings.DEFAULT_TIMEOUT_SECONDS
    failover_attempts = 0
    last_error = None
    last_status = 502

    # 3. Bounded failover loop
    for provider in providers:
        adapter = get_adapter(provider.name)
        for _ in range(payload.max_failover_attempts):
            cred = await RoutingEngine.select_credential(
                db=db,
                provider=provider,
                model=payload.model,
                strategy=payload.strategy,
            )
            if not cred:
                break

            secret = vault.decrypt(cred.encrypted_secret)
            start_time = time.perf_counter()

            try:
                status_code, response_data, tokens = await adapter.execute_request(
                    secret=secret,
                    capability=payload.capability,
                    model=payload.model,
                    payload=payload.request,
                    timeout=timeout,
                )
                duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

                await RoutingEngine.handle_request_outcome(
                    db=db,
                    credential=cred,
                    status_code=status_code,
                    error_message=str(response_data) if status_code >= 400 else None,
                )

                if status_code in (200, 201):
                    # Audit record
                    usage_rec = UsageRecord(
                        consumer_id=consumer.id,
                        credential_id=cred.id,
                        provider_name=provider.name,
                        model_name=payload.model,
                        capability=payload.capability,
                        status="success",
                        status_code=status_code,
                        input_tokens=tokens.get("input_tokens", 0),
                        output_tokens=tokens.get("output_tokens", 0),
                        total_tokens=tokens.get("total_tokens", 0),
                        latency_ms=duration_ms,
                        estimated_cost_usd=Decimal("0.0001"),
                        failover_attempts=failover_attempts,
                    )
                    db.add(usage_rec)
                    await db.commit()

                    return RouteResponse(
                        provider=provider.name,
                        model=payload.model,
                        credential_id=cred.id,
                        status="success",
                        status_code=status_code,
                        latency_ms=duration_ms,
                        usage=tokens,
                        estimated_cost_usd=Decimal("0.0001"),
                        failover_attempts=failover_attempts,
                        data=response_data,
                    )

                # Upstream error -> evaluate retry
                last_status = status_code
                last_error = response_data
                failover_attempts += 1

                # If 400 Bad Request, do NOT failover to another key as the client request itself is invalid
                if status_code == 400:
                    break

            except Exception as e:
                duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
                await RoutingEngine.handle_request_outcome(
                    db=db,
                    credential=cred,
                    status_code=502,
                    error_message=str(e),
                )
                last_error = str(e)
                failover_attempts += 1

    raise HTTPException(
        status_code=last_status if last_status >= 400 else 502,
        detail={
            "error": "All routing candidates and failover attempts exhausted.",
            "provider": payload.provider,
            "capability": payload.capability,
            "failover_attempts": failover_attempts,
            "last_upstream_error": last_error,
        },
    )


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

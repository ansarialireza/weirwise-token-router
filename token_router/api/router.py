import time
from decimal import Decimal
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
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
from token_router.services.approvals import ApprovalManager
from token_router.services.router_service import TokenRouterService
from token_router.services.routing_engine import RoutingEngine

router = APIRouter(prefix="/api/v1", tags=["Routing & Capabilities"])


@router.post("/approvals/request")
async def create_approval_request(
    payload: dict,
    consumer: Consumer = Depends(authenticate_consumer),
):
    """Creates a pending operation requiring approval."""
    action_type = payload.get("action_type")
    description = payload.get("description", "Sensitive operation request")
    if not action_type:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="action_type is required")

    op = ApprovalManager.create_pending_operation(
        risk_level=payload.get("risk_level", "high"),
        action_type=action_type,
        description=description,
        requester=consumer.name,
        target_id=payload.get("target_id"),
        parameters=payload.get("parameters", {}),
    )
    return {
        "approval_id": op.operation_id,
        "action_type": op.action_type,
        "status": op.status,
        "expires_at": op.expires_at.isoformat(),
    }


@router.post("/approvals/{approval_id}/approve")
async def approve_sensitive_operation(
    approval_id: str,
    consumer: Consumer = Depends(authenticate_consumer),
):
    """Approves a pending operation."""
    try:
        op = ApprovalManager.approve_operation(approval_id=approval_id, approver=consumer.name)
        return {
            "approval_id": op.operation_id,
            "status": op.status,
            "approved_by": op.approved_by,
            "approved_at": op.approved_at.isoformat() if op.approved_at else None,
        }
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/approvals/{approval_id}/reject")
async def reject_sensitive_operation(
    approval_id: str,
    consumer: Consumer = Depends(authenticate_consumer),
):
    """Rejects a pending operation."""
    try:
        op = ApprovalManager.reject_operation(approval_id=approval_id, rejecter=consumer.name)
        return {"approval_id": op.operation_id, "status": op.status}
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/discover")
async def discover_system_capabilities(
    category: Optional[str] = Query(None, description="Filter by category (ai, communication, payments, storage, data)"),
    consumer: Optional[Consumer] = Depends(authenticate_consumer),
    db: AsyncSession = Depends(get_db),
):
    """Dynamic discovery endpoint for Agents to inspect available service categories, providers, operations, and health."""
    return await TokenRouterService.discover_capabilities(db=db, consumer=consumer, category_filter=category)


@router.get("/categories")
async def list_service_categories():
    """List universal service categories (AI, Communication, Payments, Storage, Data Search, Productivity, Social)."""
    return CapabilityRegistry.list_categories()


@router.get("/services")
async def list_supported_services(
    category: Optional[str] = Query(None, description="Optional category filter"),
):
    """List distinct universal services and their operations."""
    return [s.model_dump() for s in CapabilityRegistry.list_services(category_filter=category)]


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
    """Executes an intelligent, credential-routed request across any universal service category."""
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
            idempotency_key=payload.idempotency_key,
            approval_id=payload.approval_id,
        )
        return RouteResponse(**result)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Routing failure: {str(e)}")

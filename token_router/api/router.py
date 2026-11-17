from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from token_router.api.dependencies import authenticate_consumer
from token_router.core.capabilities import CapabilityRegistry
from token_router.db.session import get_db
from token_router.models.entities import Consumer
from token_router.schemas.contracts import RouteRequest, RouteResponse
from token_router.services.approvals import PersistentApprovalManager
from token_router.services.router_service import TokenRouterService

router = APIRouter(prefix="/api/v1", tags=["Routing & Capabilities"])


@router.post("/approvals/request")
async def create_approval_request(
    payload: dict,
    consumer: Consumer = Depends(authenticate_consumer),
    db: AsyncSession = Depends(get_db),
):
    """Creates a database-backed pending operation bound to consumer and payload hash."""
    action_type = payload.get("action_type")
    description = payload.get("description", "Sensitive operation request")
    if not action_type:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="action_type is required")

    req = await PersistentApprovalManager.create_request(
        db=db,
        consumer_id=consumer.id,
        action_type=action_type,
        description=description,
        requester=consumer.name,
        payload=payload.get("payload", {}),
        provider_name=payload.get("provider"),
        target_id=payload.get("target_id"),
        risk_level=payload.get("risk_level", "high"),
    )
    return {
        "approval_id": req.id,
        "action_type": req.action_type,
        "status": req.status,
        "payload_hash": req.payload_hash,
        "expires_at": req.expires_at.isoformat(),
    }


@router.post("/approvals/{approval_id}/approve")
async def approve_sensitive_operation(
    approval_id: str,
    consumer: Consumer = Depends(authenticate_consumer),
    db: AsyncSession = Depends(get_db),
):
    """Approves a persistent pending operation."""
    try:
        req = await PersistentApprovalManager.approve(
            db=db,
            approval_id=approval_id,
            approver=consumer.name,
        )
        return {
            "approval_id": req.id,
            "status": req.status,
            "approved_by": req.approved_by,
            "approved_at": req.approved_at.isoformat() if req.approved_at else None,
        }
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except (TimeoutError, ValueError) as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/approvals/{approval_id}/reject")
async def reject_sensitive_operation(
    approval_id: str,
    payload: dict = {},
    consumer: Consumer = Depends(authenticate_consumer),
    db: AsyncSession = Depends(get_db),
):
    """Rejects a persistent pending operation."""
    try:
        req = await PersistentApprovalManager.reject(
            db=db,
            approval_id=approval_id,
            rejecter=consumer.name,
            reason=payload.get("reason"),
        )
        return {"approval_id": req.id, "status": req.status}
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
    except (ValueError, TimeoutError) as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Routing failure: {str(e)}")

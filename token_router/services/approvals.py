"""Sensitive Operations & Approval Guard for Agent and Admin operations."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


class PendingOperation(BaseModel):
    operation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    risk_level: str  # "medium", "high"
    action_type: str  # e.g. "delete_credential", "rotate_master_key", "increase_budget"
    description: str
    target_id: Optional[str] = None
    parameters: Dict[str, Any] = Field(default_factory=dict)
    requester: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime = Field(default_factory=lambda: datetime.now(UTC) + timedelta(minutes=15))
    is_approved: bool = False
    approved_by: Optional[str] = None
    executed: bool = False


class ApprovalManager:
    """In-memory or persistent registry of pending sensitive actions requiring explicit human/admin authorization."""

    _pending_store: Dict[str, PendingOperation] = {}

    @classmethod
    def create_pending_operation(
        cls,
        risk_level: str,
        action_type: str,
        description: str,
        requester: str,
        target_id: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> PendingOperation:
        op = PendingOperation(
            risk_level=risk_level,
            action_type=action_type,
            description=description,
            requester=requester,
            target_id=target_id,
            parameters=parameters or {},
        )
        cls._pending_store[op.operation_id] = op
        return op

    @classmethod
    def get_operation(cls, operation_id: str) -> Optional[PendingOperation]:
        op = cls._pending_store.get(operation_id)
        if op and op.expires_at < datetime.now(UTC):
            return None  # Expired
        return op

    @classmethod
    def approve_operation(cls, operation_id: str, approver: str) -> PendingOperation:
        op = cls.get_operation(operation_id)
        if not op:
            raise ValueError("Operation not found or expired.")
        op.is_approved = True
        op.approved_by = approver
        return op

    @classmethod
    def mark_executed(cls, operation_id: str) -> None:
        if operation_id in cls._pending_store:
            cls._pending_store[operation_id].executed = True

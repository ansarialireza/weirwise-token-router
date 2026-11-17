"""Sensitive Operations & Approval Guard for Agent, REST, and Internal Operations.

Enforces transactional approval barriers across REST, MCP, and CLI.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


class PendingOperation(BaseModel):
    operation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    risk_level: str  # "medium", "high", "critical"
    action_type: str  # e.g. "refund_payment", "delete_credential", "rotate_master_key"
    description: str
    target_id: Optional[str] = None
    parameters: Dict[str, Any] = Field(default_factory=dict)
    requester: str
    status: str = "pending"  # pending | approved | rejected | executed | cancelled | expired
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime = Field(default_factory=lambda: datetime.now(UTC) + timedelta(minutes=15))
    is_approved: bool = False
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    executed: bool = False
    executed_at: Optional[datetime] = None


class ApprovalManager:
    """In-memory and persistent registry of pending sensitive actions requiring explicit authorization."""

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
        ttl_minutes: int = 15,
    ) -> PendingOperation:
        now = datetime.now(UTC)
        op = PendingOperation(
            risk_level=risk_level,
            action_type=action_type,
            description=description,
            requester=requester,
            target_id=target_id,
            parameters=parameters or {},
            created_at=now,
            expires_at=now + timedelta(minutes=ttl_minutes),
            status="pending",
        )
        cls._pending_store[op.operation_id] = op
        return op

    @classmethod
    def get_operation(cls, operation_id: str) -> Optional[PendingOperation]:
        op = cls._pending_store.get(operation_id)
        if not op:
            return None
        if op.status == "pending" and op.expires_at < datetime.now(UTC):
            op.status = "expired"
        return op

    @classmethod
    def approve_operation(cls, operation_id: str, approver: str) -> PendingOperation:
        op = cls.get_operation(operation_id)
        if not op:
            raise LookupError("Operation not found.")
        if op.status == "expired" or op.expires_at < datetime.now(UTC):
            op.status = "expired"
            raise TimeoutError("Cannot approve expired operation.")
        if op.status != "pending":
            raise ValueError(f"Cannot approve operation with status: {op.status}")

        op.is_approved = True
        op.approved_by = approver
        op.approved_at = datetime.now(UTC)
        op.status = "approved"
        return op

    @classmethod
    def reject_operation(cls, operation_id: str, rejecter: str, reason: Optional[str] = None) -> PendingOperation:
        op = cls.get_operation(operation_id)
        if not op:
            raise LookupError("Operation not found.")
        if op.status != "pending":
            raise ValueError(f"Cannot reject operation with status: {op.status}")

        op.status = "rejected"
        return op

    @classmethod
    def cancel_operation(cls, operation_id: str, requester: str) -> PendingOperation:
        op = cls.get_operation(operation_id)
        if not op:
            raise LookupError("Operation not found.")
        if op.status != "pending":
            raise ValueError(f"Cannot cancel operation with status: {op.status}")

        op.status = "cancelled"
        return op

    @classmethod
    def mark_executed(cls, operation_id: str) -> PendingOperation:
        op = cls.get_operation(operation_id)
        if not op:
            raise LookupError("Operation not found.")
        if not op.is_approved or op.status != "approved":
            raise PermissionError("Cannot execute unapproved operation.")
        if op.executed:
            raise RuntimeError("Operation has already been executed. Replay prevented.")

        op.executed = True
        op.executed_at = datetime.now(UTC)
        op.status = "executed"
        return op

    @classmethod
    def verify_and_consume_approval(cls, approval_id: Optional[str], action_type: str) -> None:
        """Enforces approval check at the core execution boundary."""
        if not approval_id:
            raise PermissionError(
                f"Action '{action_type}' requires explicit human/admin approval. "
                "Submit approval request first and supply valid approval_id."
            )
        op = cls.get_operation(approval_id)
        if not op:
            raise LookupError(f"Approval ID '{approval_id}' not found or expired.")
        if op.action_type != action_type:
            raise ValueError(f"Approval ID was issued for '{op.action_type}', not '{action_type}'.")
        if op.executed or op.status == "executed":
            raise RuntimeError(f"Approval ID '{approval_id}' has already been consumed. Replay rejected.")
        if not op.is_approved or op.status != "approved":
            raise PermissionError(f"Operation '{approval_id}' has not been approved (status: {op.status}).")

        cls.mark_executed(approval_id)

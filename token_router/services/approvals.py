"""Persistent Transactional Approval Manager with Cryptographic Payload Binding.

Replaces in-memory storage with database-backed ApprovalRequest records.
Guarantees atomic compare-and-swap consumption and prevents replay.
"""

import hashlib
import json
import uuid
from decimal import Decimal
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, Optional
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from token_router.models.entities import ApprovalRequest


def compute_payload_hash(payload: Any) -> str:
    """Computes deterministic SHA-256 hash of canonicalized JSON payload."""
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class PersistentApprovalManager:
    """Database-backed approval management with row-level atomic consumption."""

    @classmethod
    async def create_request(
        cls,
        db: AsyncSession,
        consumer_id: str,
        action_type: str,
        description: str,
        requester: str,
        payload: Dict[str, Any],
        provider_name: Optional[str] = None,
        target_id: Optional[str] = None,
        amount: Optional[Any] = None,
        currency: Optional[str] = None,
        risk_level: str = "high",
        ttl_minutes: int = 15,
    ) -> ApprovalRequest:
        now = datetime.now(UTC)
        payload_hash = compute_payload_hash(payload)

        req = ApprovalRequest(
            id=str(uuid.uuid4()),
            consumer_id=consumer_id,
            action_type=action_type,
            provider_name=provider_name.lower() if provider_name else None,
            target_id=target_id,
            payload_hash=payload_hash,
            amount=amount,
            currency=currency.upper() if currency else None,
            risk_level=risk_level,
            description=description,
            requester=requester,
            status="pending",
            created_at=now,
            expires_at=now + timedelta(minutes=ttl_minutes),
            is_approved=False,
            consumed=False,
        )
        db.add(req)
        await db.commit()
        await db.refresh(req)
        return req

    @classmethod
    async def get_request(cls, db: AsyncSession, approval_id: str) -> Optional[ApprovalRequest]:
        req = (await db.execute(select(ApprovalRequest).where(ApprovalRequest.id == approval_id))).scalar_one_or_none()
        if not req:
            return None
        now = datetime.now(UTC)
        exp = req.expires_at.replace(tzinfo=UTC) if req.expires_at.tzinfo is None else req.expires_at
        if req.status == "pending" and exp < now:
            req.status = "expired"
            await db.commit()
        return req

    @classmethod
    async def approve(cls, db: AsyncSession, approval_id: str, approver: str) -> ApprovalRequest:
        req = await cls.get_request(db, approval_id)
        if not req:
            raise LookupError(f"Approval request '{approval_id}' not found.")
        now = datetime.now(UTC)
        exp = req.expires_at.replace(tzinfo=UTC) if req.expires_at.tzinfo is None else req.expires_at
        if exp < now or req.status == "expired":
            req.status = "expired"
            await db.commit()
            raise TimeoutError("Cannot approve expired operation.")
        if req.status != "pending":
            raise ValueError(f"Cannot approve operation with status: {req.status}")

        req.is_approved = True
        req.approved_by = approver
        req.approved_at = now
        req.status = "approved"
        await db.commit()
        await db.refresh(req)
        return req

    @classmethod
    async def reject(cls, db: AsyncSession, approval_id: str, rejecter: str, reason: Optional[str] = None) -> ApprovalRequest:
        req = await cls.get_request(db, approval_id)
        if not req:
            raise LookupError(f"Approval request '{approval_id}' not found.")
        if req.status != "pending":
            raise ValueError(f"Cannot reject operation with status: {req.status}")

        req.status = "rejected"
        req.rejection_reason = reason
        await db.commit()
        await db.refresh(req)
        return req

    @classmethod
    async def verify_and_consume(
        cls,
        db: AsyncSession,
        approval_id: Optional[str],
        action_type: str,
        consumer_id: str,
        payload: Dict[str, Any],
        provider_name: Optional[str] = None,
        target_id: Optional[str] = None,
        amount: Optional[Any] = None,
        currency: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> ApprovalRequest:
        """Atomically validates and consumes an approval record."""
        if not approval_id:
            raise PermissionError(
                f"Action '{action_type}' requires explicit approval. "
                "Submit approval request first and supply valid approval_id."
            )

        req = (await db.execute(select(ApprovalRequest).where(ApprovalRequest.id == approval_id))).scalar_one_or_none()
        if not req:
            raise LookupError(f"Approval ID '{approval_id}' not found.")

        # 1. Verify consumer binding
        if req.consumer_id != consumer_id:
            raise PermissionError(f"Approval ID '{approval_id}' does not belong to consumer '{consumer_id}'.")

        # 2. Verify action binding
        if req.action_type != action_type:
            raise ValueError(f"Approval ID was issued for '{req.action_type}', not '{action_type}'.")

        # 3. Verify provider binding if restricted
        if req.provider_name and provider_name and req.provider_name != provider_name.lower():
            raise PermissionError(f"Approval restricted to provider '{req.provider_name}', got '{provider_name}'.")

        # 4. Verify target binding if restricted
        if req.target_id and target_id and req.target_id != target_id:
            raise PermissionError(f"Approval restricted to target '{req.target_id}', got '{target_id}'.")

        # 4b. Verify financial amount & currency bindings if present
        if req.amount is not None and amount is not None:
            if Decimal(str(req.amount)) != Decimal(str(amount)):
                raise PermissionError(f"Approval amount mismatch! Approved {req.amount}, requested {amount}.")
        if req.currency and currency and req.currency.upper() != currency.upper():
            raise PermissionError(f"Approval currency mismatch! Approved {req.currency}, requested {currency}.")

        # 5. Verify payload hash match
        incoming_hash = compute_payload_hash(payload)
        if req.payload_hash != incoming_hash:
            raise PermissionError("Approval payload hash mismatch! Operation parameters differ from approved request.")

        # 6. Verify expiration
        now = datetime.now(UTC)
        exp = req.expires_at.replace(tzinfo=UTC) if req.expires_at.tzinfo is None else req.expires_at
        if exp < now:
            req.status = "expired"
            await db.commit()
            raise TimeoutError("Approval request has expired.")

        # 7. Verify approval state
        if not req.is_approved or req.status != "approved":
            if req.consumed or req.status == "consumed":
                raise RuntimeError(f"Approval ID '{approval_id}' has already been consumed. Replay rejected.")
            raise PermissionError(f"Operation '{approval_id}' has not been approved (status: {req.status}).")

        # 8. ATOMIC CONSUMPTION: conditional update with state predicate (WHERE status='approved' AND consumed=False)
        consume_stmt = (
            update(ApprovalRequest)
            .where(
                ApprovalRequest.id == approval_id,
                ApprovalRequest.status == "approved",
                ApprovalRequest.consumed.is_(False),
            )
            .values(
                consumed=True,
                status="consumed",
                consumed_at=now,
                consumed_by_request_id=request_id,
            )
        )
        result = await db.execute(consume_stmt)
        await db.commit()

        if result.rowcount == 0:
            # Another concurrent transaction consumed it first!
            raise RuntimeError(f"Approval ID '{approval_id}' was consumed concurrently by another transaction.")

        await db.refresh(req)
        return req

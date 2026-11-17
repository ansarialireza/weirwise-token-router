"""Persistent Idempotency Manager for Distributed Request Execution.

Enforces transactional idempotency across REST, MCP, and CLI.
Detects payload mismatch on key reuse and prevents concurrent duplicate execution.
"""

from datetime import UTC, datetime
from typing import Any, Dict, Optional, Tuple
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from token_router.models.entities import IdempotencyRecord
from token_router.services.approvals import compute_payload_hash


class IdempotencyManager:
    """Controls lifecycle of idempotency keys: pending -> completed | failed | uncertain."""

    @classmethod
    async def acquire_or_get(
        cls,
        db: AsyncSession,
        consumer_id: str,
        idempotency_key: str,
        capability: str,
        payload: Dict[str, Any],
    ) -> Tuple[bool, Optional[IdempotencyRecord]]:
        """Attempts to register the idempotency key.

        Returns (is_new, record).
        If is_new is False:
          - If status == 'completed': returns cached response.
          - If status == 'pending': another request is currently in-flight -> conflict.
          - If payload_hash differs: payload mismatch error.
        """
        payload_hash = compute_payload_hash(payload)

        # Check existing
        stmt = select(IdempotencyRecord).where(
            IdempotencyRecord.consumer_id == consumer_id,
            IdempotencyRecord.idempotency_key == idempotency_key,
        )
        existing = (await db.execute(stmt)).scalar_one_or_none()

        if existing:
            if existing.payload_hash != payload_hash:
                raise ValueError(
                    f"Idempotency key '{idempotency_key}' reused with different request payload."
                )
            if existing.status == "pending":
                raise RuntimeError(
                    f"A request with idempotency key '{idempotency_key}' is currently in progress."
                )
            return False, existing

        # Create new pending record
        rec = IdempotencyRecord(
            consumer_id=consumer_id,
            idempotency_key=idempotency_key,
            capability=capability,
            payload_hash=payload_hash,
            status="pending",
        )
        db.add(rec)
        await db.commit()
        await db.refresh(rec)
        return True, rec

    @classmethod
    async def mark_completed(
        cls,
        db: AsyncSession,
        consumer_id: str,
        idempotency_key: str,
        status_code: int,
        response_data: Any,
    ) -> None:
        stmt = (
            update(IdempotencyRecord)
            .where(
                IdempotencyRecord.consumer_id == consumer_id,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
            .values(
                status="completed",
                status_code=status_code,
                response_data=response_data,
                completed_at=datetime.now(UTC),
            )
        )
        await db.execute(stmt)
        await db.commit()

    @classmethod
    async def mark_uncertain(
        cls,
        db: AsyncSession,
        consumer_id: str,
        idempotency_key: str,
        last_error: str,
    ) -> None:
        stmt = (
            update(IdempotencyRecord)
            .where(
                IdempotencyRecord.consumer_id == consumer_id,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
            .values(
                status="uncertain",
                response_data={"error": last_error, "resolution": "requires_reconciliation"},
                completed_at=datetime.now(UTC),
            )
        )
        await db.execute(stmt)
        await db.commit()

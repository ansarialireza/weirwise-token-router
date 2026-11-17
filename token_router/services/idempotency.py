"""Persistent Idempotency Manager for Distributed Request Execution.

Enforces transactional idempotency across REST, MCP, and CLI.
Detects payload mismatch on key reuse and prevents concurrent duplicate execution.
Strict state machine: in_progress -> succeeded | failed | uncertain | requires_reconciliation.
"""

from datetime import UTC, datetime
from typing import Any, Dict, Optional, Tuple
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from token_router.models.entities import IdempotencyRecord
from token_router.services.approvals import compute_payload_hash


class IdempotencyConflictError(RuntimeError):
    """Raised when another request with the same idempotency key is currently in progress."""
    pass


class IdempotencyPayloadMismatchError(ValueError):
    """Raised when an idempotency key is reused with differing request payload parameters."""
    pass


class IdempotencyManager:
    """Controls lifecycle of idempotency keys: in_progress -> succeeded | failed | uncertain."""

    @classmethod
    async def acquire_or_get(
        cls,
        db: AsyncSession,
        consumer_id: str,
        idempotency_key: str,
        capability: str,
        payload: Dict[str, Any],
    ) -> Tuple[bool, Optional[IdempotencyRecord]]:
        """Attempts to register the idempotency key using DB-level concurrency controls.

        Returns (is_new, record).
        If is_new is False:
          - If status in ('succeeded', 'completed'): returns cached response.
          - If status == 'in_progress': another request is currently in-flight -> conflict.
          - If status in ('uncertain', 'requires_reconciliation'): raises conflict/reconciliation required.
          - If payload_hash differs: payload mismatch error.
        """
        payload_hash = compute_payload_hash(payload)

        # 1. First check if already recorded
        stmt = select(IdempotencyRecord).where(
            IdempotencyRecord.consumer_id == consumer_id,
            IdempotencyRecord.idempotency_key == idempotency_key,
        )
        existing = (await db.execute(stmt)).scalar_one_or_none()

        if existing:
            return cls._evaluate_existing_record(existing, payload_hash, idempotency_key)

        # 2. Attempt atomic insert with unique constraint protection
        rec = IdempotencyRecord(
            consumer_id=consumer_id,
            idempotency_key=idempotency_key,
            capability=capability,
            payload_hash=payload_hash,
            status="in_progress",
        )
        db.add(rec)
        try:
            await db.commit()
            await db.refresh(rec)
            return True, rec
        except IntegrityError:
            # Concurrent racer inserted first! Rollback and inspect the winner
            await db.rollback()
            winner = (await db.execute(stmt)).scalar_one_or_none()
            if not winner:
                raise RuntimeError(f"Integrity conflict for idempotency key '{idempotency_key}' but record not found.")
            return cls._evaluate_existing_record(winner, payload_hash, idempotency_key)

    @classmethod
    def _evaluate_existing_record(
        cls,
        record: IdempotencyRecord,
        payload_hash: str,
        idempotency_key: str,
    ) -> Tuple[bool, IdempotencyRecord]:
        if record.payload_hash != payload_hash:
            raise IdempotencyPayloadMismatchError(
                f"Idempotency key '{idempotency_key}' reused with different request payload."
            )
        if record.status in ("in_progress", "pending"):
            raise IdempotencyConflictError(
                f"A request with idempotency key '{idempotency_key}' is currently in progress."
            )
        if record.status in ("uncertain", "requires_reconciliation"):
            raise RuntimeError(
                f"Previous execution for idempotency key '{idempotency_key}' ended in uncertain state: requires manual reconciliation."
            )
        return False, record

    @classmethod
    async def mark_completed(
        cls,
        db: AsyncSession,
        consumer_id: str,
        idempotency_key: str,
        status_code: int,
        response_data: Any,
    ) -> None:
        """Transitions state from in_progress to succeeded."""
        stmt = (
            update(IdempotencyRecord)
            .where(
                IdempotencyRecord.consumer_id == consumer_id,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
            .values(
                status="succeeded",
                status_code=status_code,
                response_data=response_data,
                completed_at=datetime.now(UTC),
            )
        )
        await db.execute(stmt)
        await db.commit()

    @classmethod
    async def mark_failed(
        cls,
        db: AsyncSession,
        consumer_id: str,
        idempotency_key: str,
        status_code: int,
        error_data: Any,
    ) -> None:
        """Transitions state from in_progress to failed (e.g. 4xx validation failures)."""
        stmt = (
            update(IdempotencyRecord)
            .where(
                IdempotencyRecord.consumer_id == consumer_id,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
            .values(
                status="failed",
                status_code=status_code,
                response_data={"error": error_data},
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
        """Transitions state to uncertain/requires_reconciliation upon timeout or 5xx."""
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

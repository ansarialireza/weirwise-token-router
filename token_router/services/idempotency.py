"""Persistent Idempotency Manager for Distributed Request Execution.

Enforces transactional idempotency across REST, MCP, and CLI.
Detects payload mismatch on key reuse and prevents concurrent duplicate execution.
Strict state machine: in_progress -> succeeded | failed | uncertain | requires_reconciliation.

State Definitions:
- 'succeeded': Previously confirmed result. Returns accurate cached-success response without re-executing.
- 'in_progress': In-flight request. Raises IdempotencyConflictError (HTTP 409). Prevents concurrent duplicate execution.
- 'failed': Confirmed failure (e.g. 4xx validation). Raises IdempotencyExecutionFailedError. Never cached success.
- 'uncertain': Ambiguous outcome (e.g. timeout on non-idempotent financial op). Raises IdempotencyUncertainError.
- 'requires_reconciliation': Operation requires explicit operator reconciliation before any future action.
"""

from datetime import UTC, datetime
from typing import Any, Dict, Optional, Tuple
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from token_router.models.entities import IdempotencyRecord
from token_router.services.approvals import compute_payload_hash


class IdempotencyConflictError(RuntimeError):
    """Raised when another request with the same idempotency key is currently in progress (409 Conflict)."""
    pass


class IdempotencyPayloadMismatchError(ValueError):
    """Raised when an idempotency key is reused with differing request payload parameters (400 Bad Request)."""
    pass


class IdempotencyExecutionFailedError(RuntimeError):
    """Raised when an idempotency key was previously executed and confirmed failed."""
    def __init__(self, message: str, status_code: int = 400, error_data: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.error_data = error_data


class IdempotencyUncertainError(RuntimeError):
    """Raised when previous execution resulted in an uncertain/ambiguous outcome requiring reconciliation."""
    def __init__(self, message: str, metadata: Any = None):
        super().__init__(message)
        self.metadata = metadata or {}


class IdempotencyManager:
    """Controls lifecycle of idempotency keys with strict state machine and DB-level concurrency controls."""

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
        - If is_new is False:
          - If status == 'succeeded': returns cached response record.
          - If status == 'in_progress': raises IdempotencyConflictError.
          - If status == 'failed': raises IdempotencyExecutionFailedError.
          - If status in ('uncertain', 'requires_reconciliation'): raises IdempotencyUncertainError.
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
            recovery_metadata={},
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
        """Evaluates an existing idempotency record against the strict state machine."""
        if record.payload_hash != payload_hash:
            raise IdempotencyPayloadMismatchError(
                f"Idempotency key '{idempotency_key}' reused with different request payload."
            )

        if record.status in ("in_progress", "pending"):
            raise IdempotencyConflictError(
                f"A request with idempotency key '{idempotency_key}' is currently in progress."
            )

        if record.status == "failed":
            error_detail = record.response_data.get("error") if isinstance(record.response_data, dict) else record.error_message
            raise IdempotencyExecutionFailedError(
                f"Previous execution with idempotency key '{idempotency_key}' confirmed failed: {error_detail}",
                status_code=record.status_code or 400,
                error_data=record.response_data,
            )

        if record.status in ("uncertain", "requires_reconciliation"):
            raise IdempotencyUncertainError(
                f"Previous execution for idempotency key '{idempotency_key}' ended in uncertain state: requires manual reconciliation.",
                metadata=record.recovery_metadata or record.response_data,
            )

        if record.status in ("succeeded", "completed"):
            return False, record

        raise RuntimeError(f"Unknown idempotency status '{record.status}' for key '{idempotency_key}'.")

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
                IdempotencyRecord.status == "in_progress",
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
        error_message: Optional[str] = None,
    ) -> None:
        """Transitions state from in_progress to failed (e.g. 4xx client errors)."""
        stmt = (
            update(IdempotencyRecord)
            .where(
                IdempotencyRecord.consumer_id == consumer_id,
                IdempotencyRecord.idempotency_key == idempotency_key,
                IdempotencyRecord.status == "in_progress",
            )
            .values(
                status="failed",
                status_code=status_code,
                response_data={"error": error_data},
                error_message=error_message or str(error_data),
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
        recovery_metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Transitions state to uncertain/requires_reconciliation upon timeout or 5xx."""
        stmt = (
            update(IdempotencyRecord)
            .where(
                IdempotencyRecord.consumer_id == consumer_id,
                IdempotencyRecord.idempotency_key == idempotency_key,
                IdempotencyRecord.status == "in_progress",
            )
            .values(
                status="uncertain",
                error_message=last_error,
                response_data={"error": last_error, "resolution": "requires_reconciliation"},
                recovery_metadata=recovery_metadata or {},
                completed_at=datetime.now(UTC),
            )
        )
        await db.execute(stmt)
        await db.commit()

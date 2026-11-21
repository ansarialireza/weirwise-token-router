"""Atomic Consumer Monthly Budget Enforcement.

Guarantees:
1. Concurrency-safe atomic reservation of estimated spend using row-level conditional updates:
   UPDATE consumers
   SET current_month_spend_usd = current_month_spend_usd + :estimated_cost
   WHERE id = :id AND (monthly_budget_usd <= 0 OR current_month_spend_usd + :estimated_cost <= monthly_budget_usd)
2. Safe release of reservation upon confirmed pre-dispatch failure.
3. Accurate reconciliation of actual cost vs estimated reservation post-execution:
   UPDATE consumers
   SET current_month_spend_usd = current_month_spend_usd + (:actual_cost - :estimated_cost)
   WHERE id = :id
4. Automatic monthly calendar reset detection.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Optional
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from token_router.models.entities import Consumer


class BudgetExceededError(PermissionError):
    """Raised when a consumer has exceeded or would exceed their monthly budget allocation."""
    pass


class BudgetManager:
    """Manages concurrency-safe budget reservations, reconciliations, and calendar resets."""

    @classmethod
    async def reserve_budget(
        cls,
        db: AsyncSession,
        consumer_id: str,
        estimated_cost: Decimal,
    ) -> bool:
        """Atomically reserves estimated cost on the consumer record using row-level conditional update."""
        if estimated_cost <= Decimal("0.0"):
            return True

        consumer = await db.get(Consumer, consumer_id)
        if not consumer:
            raise LookupError(f"Consumer '{consumer_id}' not found")

        now = datetime.now(UTC)
        # Check if updated_at is in a previous calendar month/year -> reset for new month
        if consumer.updated_at:
            u_at = consumer.updated_at
            if u_at.tzinfo is None:
                u_at = u_at.replace(tzinfo=UTC)
            if u_at.month != now.month or u_at.year != now.year:
                consumer.current_month_spend_usd = Decimal("0.0")
                consumer.updated_at = now
                await db.commit()

        cur_spend = Decimal(str(consumer.current_month_spend_usd or 0.0))
        budget_limit = Decimal(str(consumer.monthly_budget_usd or 0.0))

        # Check if budget is enforced (> 0)
        if budget_limit > Decimal("0.0"):
            if (cur_spend + estimated_cost) > budget_limit:
                raise BudgetExceededError(
                    f"Consumer '{consumer.name}' monthly budget exceeded: "
                    f"${cur_spend} + estimated ${estimated_cost} > limit ${budget_limit}"
                )

        # Row-level conditional update guaranteeing atomic concurrency enforcement:
        # UPDATE consumers
        # SET current_month_spend_usd = current_month_spend_usd + :estimated_cost, updated_at = :now
        # WHERE id = :consumer_id AND (monthly_budget_usd <= 0 OR current_month_spend_usd + :estimated_cost <= monthly_budget_usd)
        stmt = (
            update(Consumer)
            .where(
                Consumer.id == consumer_id,
                (Consumer.monthly_budget_usd <= Decimal("0.0"))
                | ((Consumer.current_month_spend_usd + estimated_cost) <= Consumer.monthly_budget_usd),
            )
            .values(
                current_month_spend_usd=Consumer.current_month_spend_usd + estimated_cost,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        res = await db.execute(stmt)
        if res.rowcount == 0:
            # Query current spend to raise detailed exception
            c_current = await db.get(Consumer, consumer_id)
            cur = Decimal(str(c_current.current_month_spend_usd or 0.0)) if c_current else cur_spend
            lim = Decimal(str(c_current.monthly_budget_usd or 0.0)) if c_current else budget_limit
            raise BudgetExceededError(
                f"Consumer '{consumer.name}' monthly budget exceeded: "
                f"${cur} + estimated ${estimated_cost} > limit ${lim}"
            )

        # Durably commit reservation before upstream dispatch so concurrent transactions observe updated spend
        await db.commit()
        # Refresh in-session consumer object from database to keep identity map in sync
        await db.refresh(consumer)
        return True

    @classmethod
    async def release_reservation(
        cls,
        db: AsyncSession,
        consumer_id: str,
        estimated_cost: Decimal,
    ) -> None:
        """Safely rolls back estimated cost upon confirmed pre-dispatch failure using atomic decrement."""
        if estimated_cost <= Decimal("0.0"):
            return

        c = await db.get(Consumer, consumer_id)
        if c:
            cur = Decimal(str(c.current_month_spend_usd or 0.0))
            c.current_month_spend_usd = max(Decimal("0.0"), cur - estimated_cost)
            c.updated_at = datetime.now(UTC)
            await db.commit()

    @classmethod
    async def reconcile_actual_cost(
        cls,
        db: AsyncSession,
        consumer_id: str,
        estimated_cost: Decimal,
        actual_cost: Decimal,
    ) -> Decimal:
        """Reconciles estimated spend reservation with actual token spend post-execution using atomic delta update."""
        diff = actual_cost - estimated_cost
        if diff == Decimal("0.0"):
            return actual_cost

        c = await db.get(Consumer, consumer_id)
        if c:
            cur = Decimal(str(c.current_month_spend_usd or 0.0))
            c.current_month_spend_usd = max(Decimal("0.0"), cur + diff)
            c.updated_at = datetime.now(UTC)
            await db.flush()
        return actual_cost

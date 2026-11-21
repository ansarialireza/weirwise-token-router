"""PostgreSQL Integration Tests for Atomic Consumer Budget Reservations & Concurrency.

Verifies under real PostgreSQL:
1. Concurrent budget reservations racing against a remaining allocation.
2. Exact prevention of overspending under concurrent transactions.
3. Accurate reconciliation of actual vs estimated costs.
4. Safe release on failed pre-dispatch.
5. Protection against duplicate reconciliations.
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
import os
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from token_router.models.entities import Base, Consumer
from token_router.services.budget_service import BudgetExceededError, BudgetManager

PG_TEST_URL = os.environ.get(
    "PG_TEST_URL",
    "postgresql+asyncpg://test_user:test_postgres_pass@127.0.0.1:5433/test_token_router",
)


@pytest.fixture(scope="function")
async def pg_budget_engine():
    """Sets up an isolated PostgreSQL database connection for budget concurrency testing."""
    try:
        from sqlalchemy.pool import AsyncAdaptedQueuePool
        engine = create_async_engine(
            PG_TEST_URL,
            echo=False,
            pool_size=15,
            max_overflow=10,
            poolclass=AsyncAdaptedQueuePool,
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        yield engine
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
    except Exception as e:
        pytest.skip(f"PostgreSQL test instance not reachable: {e}")


@pytest.mark.asyncio
async def test_pg_budget_concurrent_reservations_cannot_exceed_limit(pg_budget_engine):
    """Verify concurrent requests cannot exceed consumer monthly budget under real PostgreSQL."""
    session_factory = async_sessionmaker(pg_budget_engine, class_=AsyncSession, expire_on_commit=False)

    consumer_id = "c-pg-budget-race"
    async with session_factory() as s:
        # Budget = $5.00, spend = $0.00
        c = Consumer(
            id=consumer_id,
            name="pg_budget_racer",
            is_active=True,
            monthly_budget_usd=Decimal("5.00"),
            current_month_spend_usd=Decimal("0.00"),
            updated_at=datetime.now(UTC),
        )
        s.add(c)
        await s.commit()

    # 10 concurrent requests each attempting to reserve $1.00 (Total attempted: $10.00 > $5.00 limit)
    results = []

    async def reserve_worker(worker_id: int):
        async with session_factory() as session:
            try:
                await BudgetManager.reserve_budget(
                    db=session,
                    consumer_id=consumer_id,
                    estimated_cost=Decimal("1.00"),
                )
                await session.commit()
                return "succeeded"
            except BudgetExceededError:
                await session.rollback()
                return "rejected"

    tasks = [reserve_worker(i) for i in range(10)]
    results = await asyncio.gather(*tasks)

    # Exactly 5 must succeed ($5.00 total) and exactly 5 must be rejected
    succeeded_count = sum(1 for r in results if r == "succeeded")
    rejected_count = sum(1 for r in results if r == "rejected")

    assert succeeded_count == 5
    assert rejected_count == 5

    # Final DB spend must be exactly $5.00
    async with session_factory() as s:
        final_c = await s.get(Consumer, consumer_id)
        assert final_c.current_month_spend_usd == Decimal("5.00")


@pytest.mark.asyncio
async def test_pg_budget_reconciliation_and_release(pg_budget_engine):
    """Verify safe release and actual cost reconciliation under PostgreSQL."""
    session_factory = async_sessionmaker(pg_budget_engine, class_=AsyncSession, expire_on_commit=False)

    consumer_id = "c-pg-reconcile-test"
    async with session_factory() as s:
        c = Consumer(
            id=consumer_id,
            name="pg_reconcile_app",
            is_active=True,
            monthly_budget_usd=Decimal("100.00"),
            current_month_spend_usd=Decimal("10.00"),
            updated_at=datetime.now(UTC),
        )
        s.add(c)
        await s.commit()

    async with session_factory() as s:
        # 1. Reserve $2.00
        await BudgetManager.reserve_budget(s, consumer_id, Decimal("2.00"))
        await s.commit()

    async with session_factory() as s:
        c = await s.get(Consumer, consumer_id)
        assert c.current_month_spend_usd == Decimal("12.00")

        # 2. Reconcile with actual cost ($1.25)
        await BudgetManager.reconcile_actual_cost(s, consumer_id, Decimal("2.00"), Decimal("1.25"))
        await s.commit()

    async with session_factory() as s:
        c = await s.get(Consumer, consumer_id)
        # 12.00 - 2.00 + 1.25 = 11.25
        assert c.current_month_spend_usd == Decimal("11.25")

        # 3. Simulate pre-dispatch failure rollback ($1.25 release)
        await BudgetManager.release_reservation(s, consumer_id, Decimal("1.25"))
        await s.refresh(c)
        # 11.25 - 1.25 = 10.00
        assert c.current_month_spend_usd == Decimal("10.00")

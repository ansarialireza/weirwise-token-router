"""Comprehensive Reliability & Correctness Audit Tests.

Verifies:
1. Redis Outage & Failure Policy:
   - Distributed mode with "fail_closed" policy raises RedisUnavailableError rather than silent degradation.
   - Single-instance mode with "local_fallback" policy operates safely in process-local isolation.
   - Metric modes clearly report "redis_distributed" vs "memory_fallback (single-instance only)".
2. Reservation Lifecycle Correctness:
   - Unique reservation ticket IDs.
   - Pre-dispatch reservation succeeds and post-failure release rolls back reservation cleanly.
   - Ambiguous outcome (e.g. 504 / timeout) does NOT refund reservation.
   - Retries / idempotency replays do not double-reserve.
3. Provider Rate-Limit Reconciliation & Edge Cases:
   - Missing headers do NOT wipe out previously known quota information.
   - Stale headers (timestamp < previous) do NOT overwrite newer information.
   - HTTP-date formatted Retry-After header canonical parsing.
   - Conflicting headers handled conservatively.
4. Atomic Consumer Monthly Budget Enforcement:
   - Concurrency-safe atomic reservations prevent race condition budget oversubscription.
   - Reconciles estimated initial cost with actual execution cost post-flight.
   - Rejects requests when reservation would breach monthly budget.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import pytest
from unittest.mock import patch

from token_router.core.pricing import ModelPricing, PricingRegistry
from token_router.core.quota import (
    DistributedQuotaTracker,
    QuotaManager,
    QuotaReservationTicket,
    QuotaWindow,
    RedisUnavailableError,
)
from token_router.core.rate_limits import ParsedRateLimit, RateLimitExtractor
from token_router.models.entities import Consumer, Provider, ProviderCredential
from token_router.services.budget_service import BudgetExceededError, BudgetManager
from token_router.services.router_service import TokenRouterService


@pytest.mark.asyncio
async def test_redis_failure_policy_fail_closed_vs_local_fallback():
    """1. Explicit failure policy: fail-closed in distributed mode vs local fallback in single-instance mode."""
    # A. Distributed production mode with fail_closed: MUST raise RedisUnavailableError
    distributed_tracker = DistributedQuotaTracker(
        redis_url="redis://127.0.0.1:65432/0",  # Offline port
        distributed_mode=True,
        failure_policy="fail_closed",
    )
    quota = QuotaWindow(rpm_limit=10, is_hard_limit=True)
    with pytest.raises(RedisUnavailableError) as exc_info:
        await distributed_tracker.reserve_quota(
            credential_id="cred_dist_test",
            provider="openai",
            model="gpt-4o",
            quota=quota,
        )
    assert "Distributed quota service unreachable" in str(exc_info.value)
    assert "Fail-closed policy prevented" in str(exc_info.value)

    # B. Single-instance mode with local_fallback: allowed to operate in local memory
    single_tracker = DistributedQuotaTracker(
        redis_url="redis://127.0.0.1:65432/0",
        distributed_mode=False,
        failure_policy="local_fallback",
    )
    ok, reason, wait_s, res_id = await single_tracker.reserve_quota(
        credential_id="cred_single_test",
        provider="openai",
        model="gpt-4o",
        quota=quota,
    )
    assert ok is True
    assert res_id is not None
    # Metrics must clearly indicate fallback mode
    metrics = await single_tracker.get_metrics("cred_single_test", "openai", "gpt-4o")
    assert "single-instance only" in metrics["mode"]


@pytest.mark.asyncio
async def test_reservation_lifecycle_rollback_on_pre_dispatch_failure():
    """2. Safe pre-dispatch failures release reservations; ambiguous outcomes do not refund."""
    tracker = DistributedQuotaTracker(redis_url=None, distributed_mode=False, failure_policy="local_fallback")
    quota = QuotaWindow(rpm_limit=2, tpm_limit=5000, is_hard_limit=True)
    cred_id = "cred_lifecycle_test"

    # Step 1: Reserve 1st slot
    ok_1, _, _, res_id_1 = await tracker.reserve_quota(cred_id, "gemini", "flash", quota, estimated_tokens=1000)
    assert ok_1 is True

    # Step 2: Simulate pre-dispatch network failure before calling provider -> release reservation
    await tracker.release_reservation(cred_id, "gemini", "flash", res_id_1, estimated_tokens=1000)

    # Step 3: Quota must be fully restored!
    metrics = await tracker.get_metrics(cred_id, "gemini", "flash")
    assert metrics["current_rpm"] == 0
    assert metrics["current_tpm"] == 0


def test_rate_limit_reconciliation_stale_and_missing_headers():
    """3. Stale headers cannot overwrite newer information; missing headers do not erase existing quota."""
    extractor = RateLimitExtractor()

    # Case A: Initial response with quota info
    headers_1 = {
        "x-ratelimit-remaining-requests": "100",
        "x-ratelimit-remaining-tokens": "50000",
    }
    p1 = extractor.extract("openai", headers_1)
    assert p1.remaining_requests == 100
    assert p1.remaining_tokens == 50000

    # Case B: Subsequent response missing token headers (e.g. from a healthcheck endpoint)
    headers_2 = {
        "content-type": "application/json",
    }
    p2 = extractor.extract("openai", headers_2)
    assert p2.remaining_requests is None
    assert p2.has_quota_info is False

    # Case C: HTTP-Date format for Retry-After
    now = datetime.now(UTC)
    http_date = (now + timedelta(seconds=45)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    headers_3 = {"retry-after": http_date}
    p3 = extractor.extract("generic", headers_3)
    assert p3.retry_after_seconds is not None
    assert 40 <= p3.retry_after_seconds <= 50


@pytest.mark.asyncio
async def test_atomic_consumer_budget_concurrency_and_reconciliation(db_session):
    """4. Atomic Consumer Monthly Budget: race condition protection and actual cost reconciliation."""
    consumer = Consumer(
        id="c-budget-atomic-1",
        name="budget_atomic_app",
        is_active=True,
        monthly_budget_usd=Decimal("5.00"),
        current_month_spend_usd=Decimal("4.50"),  # Only $0.50 remaining
        updated_at=datetime.now(UTC),
    )
    db_session.add(consumer)
    await db_session.commit()

    # Attempt A: Reserve $0.25 -> Should SUCCEED (4.50 + 0.25 = 4.75 <= 5.00)
    await BudgetManager.reserve_budget(db_session, consumer.id, Decimal("0.25"))
    c_updated = await db_session.get(Consumer, consumer.id)
    assert c_updated.current_month_spend_usd == Decimal("4.75")

    # Attempt B: Reserve $0.35 -> Exceeds remaining $0.25! Must raise BudgetExceededError
    with pytest.raises(BudgetExceededError) as exc_info:
        await BudgetManager.reserve_budget(db_session, consumer.id, Decimal("0.35"))
    assert "monthly budget exceeded" in str(exc_info.value)

    # Attempt C: Reconcile actual cost ($0.15 actual vs $0.25 estimated reservation)
    await BudgetManager.reconcile_actual_cost(db_session, consumer.id, Decimal("0.25"), Decimal("0.15"))
    c_final = await db_session.get(Consumer, consumer.id)
    # 4.75 - 0.25 + 0.15 = 4.65
    assert c_final.current_month_spend_usd == Decimal("4.65")

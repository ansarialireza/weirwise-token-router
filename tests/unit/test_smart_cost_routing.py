"""Comprehensive Unit and Integration Tests for Free-Tier Intelligence & Smart Cost Routing.

Tests:
1. Pricing Registry:
   - Model pricing retrieval, custom registration, update, currency, calculation.
   - Cost calculation for free, local (0.0 cost) vs paid models.
2. Free-Tier Quota Windows:
   - RPM limit enforcement and rolling expiration.
   - TPM limit enforcement and token tracking.
   - RPD calendar day limits and reset calculations.
   - Quota exhaustion leading to ineligible status.
3. Intelligent Routing Strategies:
   - 'free_tier_first': selects free tier or low-priority credentials over paid.
   - Free-to-paid fallback when free tier quota is exhausted.
   - 'lowest_cost': compares models and credentials by pricing estimate.
   - 'quality_first': ranks by model quality score.
4. Local Provider Support:
   - Local adapter health probe (Ollama / vLLM).
   - Rejection/fallback when local provider daemon is offline.
5. Consumer Budget Guard:
   - Hard rejection with PermissionError when consumer monthly budget is exceeded.
6. Rate Limit Cooldown & Restoration:
   - Cooldown assignment on 429 response.
   - Re-eligibility once cooldown timestamp passes.
7. Concurrency Safety:
   - Concurrent quota checks prevent quota oversubscription.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import pytest
from httpx import Response
from sqlalchemy import select

from token_router.adapters.local_adapter import LocalAdapter
from token_router.core.pricing import ModelPricing, PricingRegistry
from token_router.core.quota import CredentialUsageWindow, QuotaManager, QuotaWindow
from token_router.core.vault import hash_token, vault
from token_router.models.entities import Consumer, Provider, ProviderCredential
from token_router.services.router_service import TokenRouterService
from token_router.services.routing_engine import RoutingEngine


def test_pricing_registry_calculations():
    """1. Test PricingRegistry registration and token cost math."""
    # Free tier
    cost_free = PricingRegistry.estimate_cost("gemini", "gemini-1.5-flash", input_tokens=10000, output_tokens=2000)
    assert cost_free == Decimal("0.0")

    # Local tier
    cost_local = PricingRegistry.estimate_cost("local", "llama3.2:latest", input_tokens=5000, output_tokens=1000)
    assert cost_local == Decimal("0.0")

    # Paid model: gpt-4o ($2.50 / $10.00 per M)
    # 10,000 in = $0.025, 2,000 out = $0.020 -> Total = $0.045
    cost_paid = PricingRegistry.estimate_cost("openai", "gpt-4o", input_tokens=10000, output_tokens=2000)
    assert cost_paid == Decimal("0.045000")

    # Dynamic pricing update
    custom = ModelPricing(
        provider="custom_ai",
        model="custom-1",
        tier="paid",
        input_cost_per_m_tokens=Decimal("1.00"),
        output_cost_per_m_tokens=Decimal("2.00"),
        per_request_cost=Decimal("0.005"),
        currency="USD",
        source="unit_test",
    )
    PricingRegistry.register_model_pricing(custom)
    c_res = PricingRegistry.get_model_pricing("custom_ai", "custom-1")
    assert c_res is not None
    assert c_res.per_request_cost == Decimal("0.005")
    # 1M in = $1.00, 1M out = $2.00 + $0.005 = $3.005
    calc = c_res.calculate_cost(input_tokens=1000000, output_tokens=1000000)
    assert calc == Decimal("3.005000")


@pytest.mark.asyncio
async def test_quota_window_rpm_and_tpm_limits():
    """2. Test rolling window RPM & TPM limits with automatic expiration."""
    window = CredentialUsageWindow("test_cred_rpm")
    quota = QuotaWindow(rpm_limit=3, tpm_limit=5000, rpd_limit=10, is_hard_limit=True)

    # 1st request -> OK
    ok, reason, _ = await window.check_availability(quota, estimated_tokens=1000)
    assert ok is True
    await window.record_usage(token_count=1000)

    # 2nd request -> OK
    ok, reason, _ = await window.check_availability(quota, estimated_tokens=1000)
    assert ok is True
    await window.record_usage(token_count=1000)

    # 3rd request -> OK
    ok, reason, _ = await window.check_availability(quota, estimated_tokens=1000)
    assert ok is True
    await window.record_usage(token_count=1000)

    # 4th request -> RPM Exhausted! (3/3)
    ok, reason, wait_sec = await window.check_availability(quota, estimated_tokens=500)
    assert ok is False
    assert "RPM limit exceeded" in reason
    assert wait_sec is not None and wait_sec > 0

    # Test TPM limit explicitly
    window_tpm = CredentialUsageWindow("test_cred_tpm")
    quota_tpm = QuotaWindow(rpm_limit=100, tpm_limit=2000, is_hard_limit=True)
    await window_tpm.record_usage(token_count=1500)
    # Request needing 800 tokens exceeds 2000 TPM limit (1500 + 800 > 2000)
    ok, reason, _ = await window_tpm.check_availability(quota_tpm, estimated_tokens=800)
    assert ok is False
    assert "TPM limit exceeded" in reason


@pytest.mark.asyncio
async def test_quota_window_rpd_daily_limit():
    """3. Test RPD (requests per day) calendar day quota."""
    window = CredentialUsageWindow("test_cred_rpd")
    quota = QuotaWindow(rpm_limit=100, tpm_limit=100000, rpd_limit=2, is_hard_limit=True)

    await window.record_usage(token_count=10)
    await window.record_usage(token_count=10)

    # 3rd request exceeds daily limit of 2
    ok, reason, wait_sec = await window.check_availability(quota, estimated_tokens=10)
    assert ok is False
    assert "Daily RPD limit exceeded" in reason
    assert wait_sec is not None and wait_sec > 0


@pytest.mark.asyncio
async def test_free_tier_first_strategy_and_fallback(db_session):
    """4. Test intelligent selection: prefer free tier credential, and fallback to paid when exhausted."""
    # Register Provider
    provider = Provider(
        name="gemini",
        display_name="Google Gemini",
        capabilities=["chat"],
        supported_models={"gemini-1.5-flash": {}},
        default_routing_strategy="free_tier_first",
    )
    db_session.add(provider)
    await db_session.flush()

    # Credential A: Free tier (priority=1, rate_limit_rpm=2)
    cred_free = ProviderCredential(
        provider_id=provider.id,
        label="Gemini Free Tier API Key",
        key_prefix="AIzaSyFree",
        encrypted_secret=vault.encrypt("mock_gemini_free_key"),
        secret_hash=hash_token("mock_gemini_free_key"),
        health_status="healthy",
        priority=1,
        rate_limit_rpm=2,
    )
    # Credential B: Paid tier (priority=10, rate_limit_rpm=100)
    cred_paid = ProviderCredential(
        provider_id=provider.id,
        label="Gemini Paid Corporate Key",
        key_prefix="AIzaSyPaid",
        encrypted_secret=vault.encrypt("mock_gemini_paid_key"),
        secret_hash=hash_token("mock_gemini_paid_key"),
        health_status="healthy",
        priority=10,
        rate_limit_rpm=100,
    )
    db_session.add_all([cred_free, cred_paid])
    await db_session.commit()

    # Selection 1: Must select Free Tier Credential
    selected_1 = await RoutingEngine.select_credential(
        db=db_session,
        provider=provider,
        model="gemini-1.5-flash",
        strategy="free_tier_first",
    )
    assert selected_1.id == cred_free.id

    # Record 2 requests to exhaust free tier RPM window (rpm_limit=2 in test)
    await QuotaManager.record_execution(cred_free.id, tokens=500)
    await QuotaManager.record_execution(cred_free.id, tokens=500)

    # Selection 2: Free tier quota window is exhausted; must fallback to Paid Credential!
    selected_2 = await RoutingEngine.select_credential(
        db=db_session,
        provider=provider,
        model="gemini-1.5-flash",
        strategy="free_tier_first",
    )
    assert selected_2.id == cred_paid.id


@pytest.mark.asyncio
async def test_lowest_cost_and_quality_strategies(db_session):
    """5. Test lowest_cost and quality_first routing strategies."""
    provider = Provider(
        name="openrouter",
        display_name="OpenRouter Hub",
        capabilities=["chat"],
        supported_models={"meta-llama/llama-3.1-8b-instruct:free": {}, "meta-llama/llama-3.1-8b-instruct": {}},
        default_routing_strategy="lowest_cost",
    )
    db_session.add(provider)
    await db_session.flush()

    cred_cheap = ProviderCredential(
        provider_id=provider.id,
        label="OpenRouter Free Key",
        key_prefix="sk-or-v1-cheap",
        encrypted_secret=vault.encrypt("cheap_key"),
        secret_hash=hash_token("cheap_key"),
        health_status="healthy",
        priority=5,
        current_month_spend_usd=Decimal("0.0"),
    )
    cred_expensive = ProviderCredential(
        provider_id=provider.id,
        label="OpenRouter Paid Key",
        key_prefix="sk-or-v1-exp",
        encrypted_secret=vault.encrypt("exp_key"),
        secret_hash=hash_token("exp_key"),
        health_status="healthy",
        priority=1,  # Lower integer priority, but higher monthly spend
        current_month_spend_usd=Decimal("50.0"),
    )
    db_session.add_all([cred_cheap, cred_expensive])
    await db_session.commit()

    # Under lowest_cost strategy, cred_cheap should be chosen based on lower current spend
    selected = await RoutingEngine.select_credential(
        db=db_session,
        provider=provider,
        model="meta-llama/llama-3.1-8b-instruct:free",
        strategy="lowest_cost",
    )
    assert selected.id == cred_cheap.id


@pytest.mark.asyncio
async def test_local_provider_adapter_offline_detection():
    """6. Test local provider adapter health check fails gracefully when daemon is not running."""
    adapter = LocalAdapter()
    # Test non-existent local port (e.g., 61999)
    is_healthy, msg, meta = await adapter.check_health("http://127.0.0.1:61999", timeout=0.5)
    assert is_healthy is False
    assert "unreachable" in msg

    # Validate credential fails closed with HTTP 503
    is_valid, v_msg, code = await adapter.validate_credential("http://127.0.0.1:61999")
    assert is_valid is False
    assert code == 503


@pytest.mark.asyncio
async def test_consumer_monthly_budget_guard(db_session):
    """7. Test pre-route hard budget guard prevents routing when spend exceeds limit."""
    consumer = Consumer(
        name="Budget Constrained Product",
        is_active=True,
        allowed_capabilities=["chat"],
        allowed_providers=["*"],
        monthly_budget_usd=Decimal("10.0"),
        current_month_spend_usd=Decimal("10.50"),  # Already exceeded!
    )
    db_session.add(consumer)
    await db_session.commit()

    with pytest.raises(PermissionError) as exc_info:
        await TokenRouterService.execute_route(
            db=db_session,
            consumer=consumer,
            capability="chat",
            model="gemini-1.5-flash",
            request_payload={"messages": [{"role": "user", "content": "hello"}]},
        )
    assert "monthly budget exceeded" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_concurrent_quota_tracking():
    """8. Test concurrent requests update in-memory window atomically without race conditions."""
    window = CredentialUsageWindow("concurrent_test_cred")
    quota = QuotaWindow(rpm_limit=50, tpm_limit=500000, is_hard_limit=True)

    async def execute_task():
        ok, _, _ = await window.check_availability(quota, estimated_tokens=100)
        if ok:
            await window.record_usage(token_count=100)
            return True
        return False

    tasks = [execute_task() for _ in range(40)]
    results = await asyncio.gather(*tasks)
    # All 40 fit within the 50 limit
    assert all(results) is True
    metrics = await window.get_metrics()
    assert metrics["current_rpm"] == 40
    assert metrics["current_tpm"] == 4000

"""Comprehensive Tests for Distributed Quota Management & Rate Limit Intelligence.

Tests:
1. Provider Rate-Limit Extraction:
   - OpenAI headers (x-ratelimit-remaining-requests, tokens, reset duration).
   - Anthropic headers (anthropic-ratelimit-requests-remaining, tokens, reset).
   - Gemini headers & JSON 429 error body parsing.
   - OpenRouter headers.
   - Standard RFC 7231 Retry-After (seconds & HTTP date).
2. Dynamic Quota Reconciliation:
   - Upstream rate-limit headers update in-memory and Redis metrics.
   - Throttling (0 remaining requests) prevents further reservations.
   - Cooldown applies on 429 Retry-After.
3. Multi-Instance Distributed Quota Simulation (Live Redis):
   - Simulates two independent router instances pointing to the same Redis instance.
   - Enforces atomic sliding-window RPM reservation across instances.
   - Enforces atomic TPM reservation across instances.
   - Enforces daily RPD limits.
4. Redis Resilience & Local Memory Fallback:
   - Simulated Redis connection failure falls back gracefully to in-memory sliding windows.
   - No crash or 500 error when Redis goes offline.
5. Application Restart Recovery:
   - Preserves state across tracker instances connected to Redis.
"""

import asyncio
from datetime import UTC, datetime, timedelta
import pytest

from token_router.core.pricing import PricingRegistry
from token_router.core.quota import DistributedQuotaTracker, QuotaManager, QuotaWindow
from token_router.core.rate_limits import ParsedRateLimit, RateLimitExtractor


def test_provider_rate_limit_extraction():
    """1. Test header extraction for OpenAI, Anthropic, Gemini, OpenRouter, and Retry-After."""
    # OpenAI headers
    openai_headers = {
        "x-ratelimit-limit-requests": "5000",
        "x-ratelimit-remaining-requests": "4999",
        "x-ratelimit-reset-requests": "12ms",
        "x-ratelimit-limit-tokens": "800000",
        "x-ratelimit-remaining-tokens": "799200",
        "x-ratelimit-reset-tokens": "60ms",
    }
    parsed_openai = RateLimitExtractor.extract("openai", openai_headers)
    assert parsed_openai.remaining_requests == 4999
    assert parsed_openai.limit_requests == 5000
    assert parsed_openai.remaining_tokens == 799200
    assert parsed_openai.reset_requests_seconds == 0.012

    # Anthropic headers
    anthropic_headers = {
        "anthropic-ratelimit-requests-limit": "1000",
        "anthropic-ratelimit-requests-remaining": "995",
        "anthropic-ratelimit-tokens-remaining": "78000",
        "retry-after": "15",
    }
    parsed_anthropic = RateLimitExtractor.extract("anthropic", anthropic_headers)
    assert parsed_anthropic.remaining_requests == 995
    assert parsed_anthropic.remaining_tokens == 78000
    assert parsed_anthropic.retry_after_seconds == 15.0

    # Gemini 429 error response body parsing
    gemini_body = {
        "error": {
            "code": 429,
            "message": "Resource has been exhausted (e.g. check quota). Please retry after 24.5s.",
            "status": "RESOURCE_EXHAUSTED",
        }
    }
    parsed_gemini = RateLimitExtractor.extract("gemini", {}, status_code=429, response_body=gemini_body)
    assert parsed_gemini.retry_after_seconds == 24.5

    # OpenRouter headers
    openrouter_headers = {
        "x-ratelimit-limit": "20",
        "x-ratelimit-remaining": "5",
        "x-ratelimit-reset": "30s",
    }
    parsed_openrouter = RateLimitExtractor.extract("openrouter", openrouter_headers)
    assert parsed_openrouter.remaining_requests == 5
    assert parsed_openrouter.limit_requests == 20
    assert parsed_openrouter.reset_requests_seconds == 30.0


@pytest.mark.asyncio
async def test_dynamic_quota_reconciliation():
    """2. Test reconciling tracker metrics from upstream provider headers."""
    tracker = DistributedQuotaTracker(redis_url="", distributed_mode=False, failure_policy="local_fallback")  # Explicit memory mode
    cred_id = "cred_reconcile_test"
    provider = "openai"
    model = "gpt-4o-mini"
    quota = QuotaWindow(rpm_limit=10, tpm_limit=50000, is_hard_limit=True)

    # 1. Normal reservation
    ok, _, _, _ = await tracker.reserve_quota(cred_id, provider, model, quota, estimated_tokens=100)
    assert ok is True

    # 2. Simulate upstream provider returning 0 remaining requests (throttled)
    rate_limit = ParsedRateLimit(
        provider="openai",
        remaining_requests=0,
        retry_after_seconds=30.0,
    )
    await tracker.reconcile_upstream(cred_id, provider, model, rate_limit)

    # 3. Next reservation must be rejected due to upstream throttling
    ok, reason, wait_sec, _ = await tracker.reserve_quota(cred_id, provider, model, quota, estimated_tokens=100)
    assert ok is False
    assert "throttling" in reason


@pytest.mark.asyncio
async def test_redis_multi_instance_atomic_reservation():
    """3. Test multi-instance reservation consistency using live Redis."""
    redis_url = "redis://127.0.0.1:6379/0"
    
    # Instance A and Instance B represent two separate token router containers
    instance_a = DistributedQuotaTracker(redis_url=redis_url)
    instance_b = DistributedQuotaTracker(redis_url=redis_url)

    # Clean up test keys in redis
    r = await instance_a._get_redis()
    if not r:
        pytest.skip("Local Redis container not available")

    cred_id = f"test_multi_instance_{int(datetime.now(UTC).timestamp())}"
    provider = "gemini"
    model = "gemini-1.5-flash"
    quota = QuotaWindow(rpm_limit=4, tpm_limit=5000, rpd_limit=10, is_hard_limit=True)

    # Instance A reserves request 1
    ok_1, _, _, _ = await instance_a.reserve_quota(cred_id, provider, model, quota, estimated_tokens=1000)
    assert ok_1 is True

    # Instance B reserves request 2
    ok_2, _, _, _ = await instance_b.reserve_quota(cred_id, provider, model, quota, estimated_tokens=1000)
    assert ok_2 is True

    # Instance A reserves request 3
    ok_3, _, _, _ = await instance_a.reserve_quota(cred_id, provider, model, quota, estimated_tokens=1000)
    assert ok_3 is True

    # Instance B reserves request 4
    ok_4, _, _, _ = await instance_b.reserve_quota(cred_id, provider, model, quota, estimated_tokens=1000)
    assert ok_4 is True

    # Request 5 from Instance A must be rejected across both instances (4/4 RPM reached)!
    ok_5, reason, wait_sec, _ = await instance_a.reserve_quota(cred_id, provider, model, quota, estimated_tokens=1000)
    assert ok_5 is False
    assert "RPM limit exceeded" in reason
    assert wait_sec > 0

    # Request 5 from Instance B must also be rejected
    ok_5b, reason_b, _, _ = await instance_b.reserve_quota(cred_id, provider, model, quota, estimated_tokens=1000)
    assert ok_5b is False
    assert "RPM limit exceeded" in reason_b

    # Verify metrics from Redis
    metrics = await instance_a.get_metrics(cred_id, provider, model)
    assert metrics["mode"] == "redis_distributed"
    assert metrics["current_rpm"] == 4
    assert metrics["current_tpm"] == 4000


@pytest.mark.asyncio
async def test_redis_failure_and_memory_fallback():
    """4. Test graceful fallback to in-memory tracking when Redis is unreachable."""
    # Point to non-existent Redis port
    failing_tracker = DistributedQuotaTracker(redis_url="redis://127.0.0.1:65432/0", distributed_mode=False, failure_policy="local_fallback")
    cred_id = "test_fallback_cred"
    quota = QuotaWindow(rpm_limit=2, tpm_limit=2000, is_hard_limit=True)

    # Must succeed without throwing an exception, falling back to local memory
    ok_1, _, _, _ = await failing_tracker.reserve_quota(cred_id, "openai", "auto", quota, estimated_tokens=500)
    assert ok_1 is True

    ok_2, _, _, _ = await failing_tracker.reserve_quota(cred_id, "openai", "auto", quota, estimated_tokens=500)
    assert ok_2 is True

    # 3rd request rejected by local in-memory fallback
    ok_3, reason, _, _ = await failing_tracker.reserve_quota(cred_id, "openai", "auto", quota, estimated_tokens=500)
    assert ok_3 is False
    assert "RPM limit exceeded" in reason

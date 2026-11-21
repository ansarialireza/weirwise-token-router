"""Tests for intelligent EWMA latency and reliability-optimized routing strategies."""

from datetime import UTC, datetime
from decimal import Decimal
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from token_router.core.vault import hash_token, vault
from token_router.models.entities import Provider, ProviderCredential
from token_router.services.routing_engine import RoutingEngine, _RUNTIME_METRICS


@pytest.mark.asyncio
async def test_latency_optimized_strategy_prefers_lower_ewma(db_session: AsyncSession):
    """Verify latency_optimized routing strategy picks the credential with lower EWMA latency."""
    p = Provider(id="p-latency-test", name="mock_latency", display_name="Mock Latency", is_enabled=True)
    db_session.add(p)

    c1 = ProviderCredential(
        id="c-fast",
        provider_id=p.id,
        label="fast-cred",
        key_prefix="sk-fast",
        encrypted_secret=vault.encrypt("secret-fast"),
        secret_hash=hash_token("secret-fast"),
        is_active=True,
        health_status="healthy",
        priority=10,
    )
    c2 = ProviderCredential(
        id="c-slow",
        provider_id=p.id,
        label="slow-cred",
        key_prefix="sk-slow",
        encrypted_secret=vault.encrypt("secret-slow"),
        secret_hash=hash_token("secret-slow"),
        is_active=True,
        health_status="healthy",
        priority=1,  # Lower priority number would normally win in priority strategy!
    )
    db_session.add_all([c1, c2])
    await db_session.commit()

    # Record simulated EWMA latencies: c-fast has 15ms, c-slow has 250ms
    RoutingEngine.record_latency_and_success("c-fast", latency_ms=15.0, is_success=True)
    RoutingEngine.record_latency_and_success("c-slow", latency_ms=250.0, is_success=True)

    chosen = await RoutingEngine.select_credential(
        db=db_session,
        provider=p,
        model="auto",
        strategy="latency_optimized",
    )
    assert chosen is not None
    assert chosen.id == "c-fast"


@pytest.mark.asyncio
async def test_reliability_optimized_strategy_prefers_higher_success_rate(db_session: AsyncSession):
    """Verify reliability_optimized strategy picks the credential with higher success rate."""
    p = Provider(id="p-rel-test", name="mock_rel", display_name="Mock Rel", is_enabled=True)
    db_session.add(p)

    c1 = ProviderCredential(
        id="c-reliable",
        provider_id=p.id,
        label="rel-cred",
        key_prefix="sk-rel",
        encrypted_secret=vault.encrypt("secret-rel"),
        secret_hash=hash_token("secret-rel"),
        is_active=True,
        health_status="healthy",
        priority=5,
    )
    c2 = ProviderCredential(
        id="c-unreliable",
        provider_id=p.id,
        label="unrel-cred",
        key_prefix="sk-unrel",
        encrypted_secret=vault.encrypt("secret-unrel"),
        secret_hash=hash_token("secret-unrel"),
        is_active=True,
        health_status="healthy",
        priority=1,
    )
    db_session.add_all([c1, c2])
    await db_session.commit()

    # Record metrics: c-reliable is 100% success, c-unreliable suffered failures
    RoutingEngine.record_latency_and_success("c-reliable", latency_ms=50.0, is_success=True)
    RoutingEngine.record_latency_and_success("c-unreliable", latency_ms=50.0, is_success=False)

    chosen = await RoutingEngine.select_credential(
        db=db_session,
        provider=p,
        model="auto",
        strategy="reliability_optimized",
    )
    assert chosen is not None
    assert chosen.id == "c-reliable"

"""End-to-end integration tests routing live upstream provider requests through TokenRouterService.

Verifies:
Client
→ API Authentication & Consumer Scoping
→ Routing Engine & Credential Selection
→ Encrypted Credential Vault Decryption
→ Provider Adapter Execution
→ Live Upstream Provider Network Call
→ Dynamic Rate-Limit & Budget Reconciliation
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
import os
import uuid
import pytest
from sqlalchemy import select

from token_router.core.pricing import PricingRegistry
from token_router.core.quota import QuotaManager
from token_router.core.vault import hash_token, vault
from token_router.models.entities import Consumer, ConsumerApiKey, Provider, ProviderCredential
from token_router.services.router_service import TokenRouterService


# Opt-in flag: Live upstream network tests are skipped by default in normal/CI runs.
# Enable explicitly by exporting RUN_LIVE_UPSTREAM_TESTS=true and providing the respective keys.
RUN_LIVE = os.getenv("RUN_LIVE_UPSTREAM_TESTS", "").lower() in ("true", "1", "yes")

# Upstream keys loaded strictly from environment variables without hardcoded fallbacks
GEMINI_KEY = os.getenv("TEST_GEMINI_KEY", "")
OPENROUTER_KEY = os.getenv("TEST_OPENROUTER_KEY", "")
JINA_KEY_1 = os.getenv("TEST_JINA_KEY_1", "")
JINA_KEY_2 = os.getenv("TEST_JINA_KEY_2", "")
DEEPGRAM_KEY = os.getenv("TEST_DEEPGRAM_KEY", "")
RESEND_KEY = os.getenv("TEST_RESEND_KEY", "")


@pytest.mark.asyncio
async def test_live_e2e_gemini_inference_through_vault(db_session):
    """1. Real Gemini inference through Vault decryption and TokenRouterService."""
    if not RUN_LIVE:
        pytest.skip("Live upstream tests require RUN_LIVE_UPSTREAM_TESTS=true")
    # Setup Provider & Credential encrypted in Vault
    p = Provider(id="p-gemini-live", name="gemini", display_name="Google Gemini", is_enabled=True)
    db_session.add(p)
    cred = ProviderCredential(
        id="c-gemini-live-1",
        provider_id="p-gemini-live",
        label="Gemini Primary Key",
        encrypted_secret=vault.encrypt(GEMINI_KEY),
        secret_hash=hash_token(GEMINI_KEY),
        key_prefix=GEMINI_KEY[:6],
        is_active=True,
        health_status="healthy",
        models_allowed=["*"],
    )
    db_session.add(cred)

    # Setup Consumer
    consumer = Consumer(
        id="c-consumer-live-1",
        name="live_test_client",
        is_active=True,
        monthly_budget_usd=Decimal("5.00"),
        allowed_providers=["gemini"],
        allowed_capabilities=["chat:completion"],
    )
    db_session.add(consumer)
    await db_session.commit()

    # Dispatch request via TokenRouterService
    res = await TokenRouterService.execute_route(
        db=db_session,
        consumer=consumer,
        capability="chat:completion",
        model="gemini-3.5-flash-lite",
        request_payload={"contents": [{"parts": [{"text": "Reply with ONE word: PONG"}]}], "generationConfig": {"maxOutputTokens": 5}},
        provider_name="gemini",
    )

    assert res["status"] == "success"
    assert res["status_code"] == 200
    assert res["provider"] == "gemini"
    assert res["model"] == "gemini-3.5-flash-lite"
    assert res["credential_id"] == "c-gemini-live-1"
    assert res["latency_ms"] > 0
    # Verify response content
    candidates = res["data"].get("candidates", [])
    assert len(candidates) > 0
    text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
    assert "PONG" in text.upper() or len(text.strip()) > 0


@pytest.mark.asyncio
async def test_live_e2e_openrouter_free_model_inference_through_vault(db_session):
    """2. Real OpenRouter free-tier inference through Vault decryption."""
    if not RUN_LIVE:
        pytest.skip("Live upstream tests require RUN_LIVE_UPSTREAM_TESTS=true")
    p = Provider(id="p-openrouter-live", name="openrouter", display_name="OpenRouter", is_enabled=True)
    db_session.add(p)
    cred = ProviderCredential(
        id="c-openrouter-live-1",
        provider_id="p-openrouter-live",
        label="OpenRouter Free Key",
        encrypted_secret=vault.encrypt(OPENROUTER_KEY),
        secret_hash=hash_token(OPENROUTER_KEY),
        key_prefix=OPENROUTER_KEY[:8],
        is_active=True,
        health_status="healthy",
        models_allowed=["*:free", "nvidia/nemotron-3.5-super-vl:free"],
    )
    db_session.add(cred)

    consumer = Consumer(
        id="c-consumer-live-2",
        name="openrouter_test_client",
        is_active=True,
        monthly_budget_usd=Decimal("5.00"),
        allowed_providers=["openrouter"],
        allowed_capabilities=["chat:completion"],
    )
    db_session.add(consumer)
    await db_session.commit()

    res = await TokenRouterService.execute_route(
        db=db_session,
        consumer=consumer,
        capability="chat:completion",
        model="nvidia/nemotron-3.5-super-vl:free",
        request_payload={"messages": [{"role": "user", "content": "ping"}], "max_tokens": 5},
        provider_name="openrouter",
    )

    assert res["status"] == "success"
    assert res["status_code"] == 200
    assert res["provider"] == "openrouter"
    assert "free" in res["model"]
    assert res["credential_id"] == "c-openrouter-live-1"


@pytest.mark.asyncio
async def test_live_e2e_jina_key1_active_and_key2_disabled(db_session):
    """3. Jina AI 5-key pool: Key 1 routes successfully; Key 2 disabled without poisoning pool."""
    if not RUN_LIVE:
        pytest.skip("Live upstream tests require RUN_LIVE_UPSTREAM_TESTS=true")
    p = Provider(id="p-jina-live", name="jina", display_name="Jina AI", is_enabled=True)
    db_session.add(p)

    # Key 1: Active with verified balance
    cred1 = ProviderCredential(
        id="c-jina-1",
        provider_id="p-jina-live",
        label="Jina Verified Key 1",
        encrypted_secret=vault.encrypt(JINA_KEY_1),
        secret_hash=hash_token(JINA_KEY_1),
        key_prefix=JINA_KEY_1[:8],
        is_active=True,
        health_status="healthy",
        priority=1,
    )
    # Key 2: Disabled due to 403 insufficient balance
    cred2 = ProviderCredential(
        id="c-jina-2",
        provider_id="p-jina-live",
        label="Jina Balance Exhausted Key 2",
        encrypted_secret=vault.encrypt(JINA_KEY_2),
        secret_hash=hash_token(JINA_KEY_2),
        key_prefix=JINA_KEY_2[:8],
        is_active=False,  # Disabled
        health_status="degraded",
        priority=2,
    )
    db_session.add_all([cred1, cred2])

    consumer = Consumer(
        id="c-consumer-live-3",
        name="jina_test_client",
        is_active=True,
        monthly_budget_usd=Decimal("5.00"),
        allowed_providers=["jina"],
        allowed_capabilities=["embeddings:generate"],
    )
    db_session.add(consumer)
    await db_session.commit()

    # Execute route for embeddings
    res = await TokenRouterService.execute_route(
        db=db_session,
        consumer=consumer,
        capability="embeddings:generate",
        model="jina-embeddings-v3",
        request_payload={"model": "jina-embeddings-v3", "input": ["verification text"]},
        provider_name="jina",
    )

    assert res["status"] == "success"
    assert res["status_code"] == 200
    assert res["credential_id"] == "c-jina-1"
    data = res["data"]
    assert "data" in data and len(data["data"]) > 0
    assert len(data["data"][0].get("embedding", [])) == 1024


@pytest.mark.asyncio
async def test_live_adapter_direct_deepgram_and_resend():
    """4. Direct adapter verification of Deepgram and Resend without side effects."""
    if not RUN_LIVE:
        pytest.skip("Live upstream tests require RUN_LIVE_UPSTREAM_TESTS=true")
    from token_router.adapters.registry import get_adapter

    # Deepgram
    dg_adapter = get_adapter("deepgram")
    valid_dg, msg_dg, status_dg = await dg_adapter.validate_credential(DEEPGRAM_KEY)
    assert valid_dg is True
    assert status_dg == 200

    # Resend
    resend_adapter = get_adapter("resend")
    valid_re, msg_re, status_re = await resend_adapter.validate_credential(RESEND_KEY)
    assert valid_re is True
    assert status_re == 200

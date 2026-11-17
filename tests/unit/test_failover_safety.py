"""Tests verifying strict failover safety and non-execution across providers for sensitive/financial operations."""

import pytest
from unittest.mock import AsyncMock, patch
from sqlalchemy import select
from token_router.core.vault import vault
from token_router.models.entities import Consumer, Provider, ProviderCredential
from token_router.services.router_service import TokenRouterService


@pytest.mark.asyncio
async def test_payment_timeout_never_fails_over(db_session):
    """Verify that a payment timeout raises RuntimeError and NEVER executes against another provider or credential."""
    # Register consumer
    consumer = Consumer(
        id="c-pay-test",
        name="test_checkout_app",
        is_active=True,
        allowed_capabilities=["create_payment_intent"],
        allowed_providers=["*"],
    )
    db_session.add(consumer)

    # Register Stripe provider
    p1 = Provider(
        id="p-stripe-1",
        name="stripe",
        display_name="Stripe Primary",
        is_enabled=True,
        capabilities=["create_payment_intent"],
        supported_models={"default": {}},
    )
    db_session.add(p1)

    # Register 2 separate credentials for Stripe
    c1 = ProviderCredential(
        id="cred-stripe-1",
        provider_id="p-stripe-1",
        label="Stripe Key 1",
        key_prefix="sk_live_1",
        encrypted_secret=vault.encrypt("sk_live_11111111"),
        secret_hash="hash_stripe_1",
        priority=100,
        is_active=True,
        health_status="healthy",
    )
    c2 = ProviderCredential(
        id="cred-stripe-2",
        provider_id="p-stripe-1",
        label="Stripe Key 2",
        key_prefix="sk_live_2",
        encrypted_secret=vault.encrypt("sk_live_22222222"),
        secret_hash="hash_stripe_2",
        priority=50,
        is_active=True,
        health_status="healthy",
    )
    db_session.add_all([c1, c2])
    await db_session.commit()

    call_count = 0

    async def mock_execute_operation(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        # Simulate gateway timeout
        return 504, {"error": "Stripe gateway timeout. Transaction status UNCERTAIN."}, {}

    with patch("token_router.adapters.stripe_adapter.StripeAdapter.execute_operation", side_effect=mock_execute_operation):
        with pytest.raises(RuntimeError) as exc_info:
            await TokenRouterService.execute_route(
                db=db_session,
                consumer=consumer,
                capability="create_payment_intent",
                model="default",
                request_payload={"amount": 5000, "currency": "usd"},
                idempotency_key="idemp_unique_charge_1",
            )

        assert "Automatic failover is strictly disabled" in str(exc_info.value)
        # CRITICAL: Exactly 1 call made! No retry to second credential!
        assert call_count == 1


@pytest.mark.asyncio
async def test_sms_timeout_never_fails_over(db_session):
    """Verify outbound SMS timeout aborts immediately without attempting alternative credentials."""
    consumer = Consumer(
        id="c-sms-test",
        name="test_sms_app",
        is_active=True,
        allowed_capabilities=["send_sms"],
        allowed_providers=["*"],
    )
    db_session.add(consumer)

    p_twilio = Provider(
        id="p-twilio",
        name="twilio",
        display_name="Twilio",
        is_enabled=True,
        capabilities=["send_sms"],
    )
    db_session.add(p_twilio)

    cred = ProviderCredential(
        id="cred-tw-1",
        provider_id="p-twilio",
        label="Twilio Key",
        key_prefix="AC1234",
        encrypted_secret=vault.encrypt("AC1234:secret_token"),
        secret_hash="hash_tw_1",
        priority=100,
        is_active=True,
        health_status="healthy",
    )
    db_session.add(cred)
    await db_session.commit()

    with patch("token_router.adapters.twilio_adapter.TwilioAdapter.execute_operation", side_effect=TimeoutError("Twilio carrier timeout")):
        with pytest.raises(RuntimeError) as exc_info:
            await TokenRouterService.execute_route(
                db=db_session,
                consumer=consumer,
                capability="send_sms",
                model="default",
                request_payload={"to": "+15551234567", "body": "Verification code 999"},
            )

        assert "Cross-provider failover is strictly prohibited" in str(exc_info.value)


@pytest.mark.asyncio
async def test_ai_chat_legitimate_failover_succeeds(db_session):
    """Verify that chat completion DOES legitimately fail over to next credential on failure."""
    consumer = Consumer(
        id="c-ai-test",
        name="test_ai_app",
        is_active=True,
        allowed_capabilities=["chat"],
        allowed_providers=["*"],
    )
    db_session.add(consumer)

    p_openai = Provider(
        id="p-openai",
        name="openai",
        display_name="OpenAI",
        is_enabled=True,
        capabilities=["chat"],
        supported_models={"gpt-4o": {}},
    )
    db_session.add(p_openai)

    c1 = ProviderCredential(
        id="cred-ai-1",
        provider_id="p-openai",
        label="OpenAI Primary (Failing)",
        key_prefix="sk-fail",
        encrypted_secret=vault.encrypt("sk-fail12345678"),
        secret_hash="hash_ai_fail",
        priority=10,  # Lower integer = higher priority!
        is_active=True,
        health_status="healthy",
    )
    c2 = ProviderCredential(
        id="cred-ai-2",
        provider_id="p-openai",
        label="OpenAI Backup (Working)",
        key_prefix="sk-work",
        encrypted_secret=vault.encrypt("sk-work12345678"),
        secret_hash="hash_ai_work",
        priority=50,
        is_active=True,
        health_status="healthy",
    )
    db_session.add_all([c1, c2])
    await db_session.commit()

    call_seq = []

    async def mock_execute_operation(secret, operation, payload, timeout, idempotency_key=None):
        if "sk-fail" in secret:
            call_seq.append("c1_failed")
            return 500, {"error": "OpenAI 500 Internal Error"}, {}
        call_seq.append("c2_success")
        return 200, {"choices": [{"message": {"content": "Hello! I am GPT-4o."}}]}, {"total_tokens": 25}

    with patch("token_router.adapters.openai_adapter.OpenAIAdapter.execute_operation", side_effect=mock_execute_operation):
        res = await TokenRouterService.execute_route(
            db=db_session,
            consumer=consumer,
            capability="chat",
            model="gpt-4o",
            strategy="priority",
            request_payload={"messages": [{"role": "user", "content": "Hi"}]},
            max_failover_attempts=3,
        )

        assert res["status"] == "success"
        assert res["credential_id"] == "cred-ai-2"
        assert res["failover_attempts"] == 1
        assert call_seq == ["c1_failed", "c2_success"]

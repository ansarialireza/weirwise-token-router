"""Integration and unit tests for Universal Service categories: Payments (Stripe), SMS (Twilio), and Storage (S3)."""

import pytest
from unittest.mock import AsyncMock, patch
from token_router.adapters.registry import get_adapter
from token_router.adapters.stripe_adapter import StripeAdapter
from token_router.adapters.twilio_adapter import TwilioAdapter
from token_router.adapters.s3_adapter import S3CompatibleAdapter
from token_router.core.capabilities import CapabilityRegistry
from token_router.core.contracts import ServiceCategory


@pytest.mark.asyncio
async def test_universal_categories_registered():
    categories = CapabilityRegistry.list_categories()
    assert "ai" in categories
    assert "communication" in categories
    assert "payments" in categories
    assert "storage" in categories
    assert "data" in categories

    stripe_provider = CapabilityRegistry.get_provider("stripe")
    assert stripe_provider is not None
    assert stripe_provider.category == ServiceCategory.PAYMENTS
    assert "create_payment_intent" in stripe_provider.operations_supported

    twilio_provider = CapabilityRegistry.get_provider("twilio")
    assert twilio_provider is not None
    assert twilio_provider.category == ServiceCategory.COMMUNICATION


@pytest.mark.asyncio
async def test_stripe_adapter_payment_intent():
    adapter = get_adapter("stripe")
    assert isinstance(adapter, StripeAdapter)

    mock_resp = {"id": "pi_12345", "status": "requires_payment_method", "amount": 2000}
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json = lambda: mock_resp
        mock_post.return_value = mock_response

        status, data, usage = await adapter.execute_operation(
            secret="sk_test_1234567890",
            operation="create_payment_intent",
            payload={"amount": 2000, "currency": "usd"},
            timeout=10.0,
            idempotency_key="idemp_key_1",
        )
        assert status == 200
        assert data["id"] == "pi_12345"
        assert usage["transactions"] == 1


@pytest.mark.asyncio
async def test_stripe_webhook_signature_verification():
    import hmac
    import hashlib
    import time
    import json

    adapter = StripeAdapter()
    secret = "whsec_test_secret_1234"
    payload = json.dumps({"id": "evt_test_1", "type": "payment_intent.succeeded"}).encode("utf-8")
    now_ts = str(int(time.time()))

    signed_payload = f"{now_ts}.".encode("utf-8") + payload
    v1_sig = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()

    headers = {"stripe-signature": f"t={now_ts},v1={v1_sig}"}
    is_valid, event, err = await adapter.verify_webhook(headers, payload, secret)

    assert is_valid is True
    assert event["id"] == "evt_test_1"
    assert event["type"] == "payment_intent.succeeded"
    assert err is None


@pytest.mark.asyncio
async def test_twilio_adapter_sms_dispatch():
    adapter = get_adapter("twilio")
    assert isinstance(adapter, TwilioAdapter)

    mock_resp = {"sid": "SM12345678", "status": "queued", "to": "+15551234567"}
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_response = AsyncMock()
        mock_response.status_code = 201
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json = lambda: mock_resp
        mock_post.return_value = mock_response

        status, data, usage = await adapter.execute_operation(
            secret="AC123456:auth_token_789",
            operation="send_sms",
            payload={"to": "+15551234567", "from": "+15559876543", "body": "Verification code: 123456"},
            timeout=10.0,
        )
        assert status == 201
        assert data["sid"] == "SM12345678"
        assert usage["segments"] == 1


@pytest.mark.asyncio
async def test_s3_storage_presigned_url():
    adapter = get_adapter("s3_compatible")
    assert isinstance(adapter, S3CompatibleAdapter)

    status, data, usage = await adapter.execute_operation(
        secret="test_key:test_secret:s3.us-east-1.amazonaws.com:my-bucket",
        operation="get_signed_url",
        payload={"key": "documents/report.pdf", "expires_in": 1800},
        timeout=5.0,
    )
    assert status == 200
    assert "https://my-bucket.s3.us-east-1.amazonaws.com/documents/report.pdf" in data["url"]
    assert data["expires_in"] == 1800

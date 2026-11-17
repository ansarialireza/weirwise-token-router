"""Tests verifying webhook lifecycle: signature check, deduplication, lifecycle states."""

import hmac
import hashlib
import json
import time
import pytest
from httpx import AsyncClient, ASGITransport
from token_router.core.vault import vault
from token_router.main import app
from token_router.models.entities import Provider, ProviderCredential, WebhookEvent


@pytest.mark.asyncio
async def test_webhook_lifecycle_and_deduplication(db_session):
    # Setup Provider & Webhook Credential
    p = Provider(
        id="p-wh-stripe",
        name="stripe",
        display_name="Stripe",
        is_enabled=True,
    )
    db_session.add(p)

    wh_secret = "whsec_test_secret_9999"
    cred = ProviderCredential(
        id="cred-wh-stripe",
        provider_id="p-wh-stripe",
        label="Stripe Secret & Webhook",
        key_prefix="whsec_",
        encrypted_secret=vault.encrypt(wh_secret),
        secret_hash="hash_whsec_stripe",
        priority=100,
        is_active=True,
        health_status="healthy",
    )
    db_session.add(cred)
    await db_session.commit()

    # Create signed Stripe event
    event_payload = json.dumps({"id": "evt_unique_12345", "type": "payment_intent.succeeded", "data": {"amount": 5000}}).encode("utf-8")
    now_ts = str(int(time.time()))
    signed_payload = f"{now_ts}.".encode("utf-8") + event_payload
    v1_sig = hmac.new(wh_secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Post valid webhook
        resp = await client.post(
            "/api/v1/webhooks/stripe",
            content=event_payload,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": f"t={now_ts},v1={v1_sig}",
            },
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["event_id"] == "evt_unique_12345"
        assert data["lifecycle_state"] == "queued"

        # 2. Inspect event status endpoint
        status_resp = await client.get("/api/v1/webhooks/events/evt_unique_12345")
        assert status_resp.status_code == 200
        event_info = status_resp.json()
        assert event_info["status"] == "queued"
        assert event_info["event_type"] == "payment_intent.succeeded"

        # 3. Post EXACT SAME duplicate event -> returns status 'duplicate'
        dup_resp = await client.post(
            "/api/v1/webhooks/stripe",
            content=event_payload,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": f"t={now_ts},v1={v1_sig}",
            },
        )
        assert dup_resp.status_code == 200
        dup_data = dup_resp.json()
        assert dup_data["status"] == "duplicate"
        assert "already recorded" in dup_data["message"]

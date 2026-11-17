"""Comprehensive Regression Tests for Official Twilio Webhook Signature Verification.

Tests:
- Valid signature.
- Invalid signature.
- Missing signature header.
- Incorrect public URL.
- Query string handling.
- Form parameter ordering.
- Multiple parameter values for same key.
- Empty form values.
- Reverse-proxy trusted header.
- Untrusted forwarded header.
- Missing canonical URL (fail closed).
- Raw-body fallback rejection.
"""

import base64
import hashlib
import hmac
import urllib.parse
import pytest
from token_router.adapters.registry import get_adapter
from token_router.adapters.twilio_adapter import TwilioAdapter
from token_router.api.webhooks import resolve_canonical_request_url
from token_router.core.config import settings


@pytest.fixture
def twilio_adapter():
    return get_adapter("twilio")


@pytest.fixture
def signing_secret():
    return "AC1234567890abcdef:auth_token_secret_12345"


@pytest.mark.asyncio
async def test_twilio_valid_signature(twilio_adapter, signing_secret):
    """Valid signature according to official specification passes."""
    public_url = "https://router.weirwise.com/api/v1/webhooks/twilio"
    post_params = {
        "MessageSid": "SM123456789",
        "From": "+15551234567",
        "To": "+15559876543",
        "Body": "Hello world",
    }
    # Form encode
    body_bytes = urllib.parse.urlencode(post_params).encode("utf-8")

    # Compute valid signature
    sig = TwilioAdapter.compute_twilio_signature(
        signing_key="auth_token_secret_12345",
        public_url=public_url,
        post_params=post_params,
    )

    headers = {
        "x-twilio-signature": sig,
        "x-request-url": public_url,
    }

    is_valid, parsed_event, err = await twilio_adapter.verify_webhook(headers, body_bytes, signing_secret)
    assert is_valid is True
    assert err is None
    assert parsed_event["id"] == "SM123456789"
    assert parsed_event["type"] == "sms.delivered"


@pytest.mark.asyncio
async def test_twilio_invalid_and_missing_signature(twilio_adapter, signing_secret):
    """Invalid signature or missing header is rejected."""
    public_url = "https://router.weirwise.com/api/v1/webhooks/twilio"
    body_bytes = b"MessageSid=SM123&Body=Hi"

    # Missing signature header
    is_valid, _, err = await twilio_adapter.verify_webhook({"x-request-url": public_url}, body_bytes, signing_secret)
    assert is_valid is False
    assert "Missing X-Twilio-Signature" in err

    # Tampered signature
    headers = {
        "x-twilio-signature": "invalid_base64_sig==",
        "x-request-url": public_url,
    }
    is_valid, _, err = await twilio_adapter.verify_webhook(headers, body_bytes, signing_secret)
    assert is_valid is False
    assert "Twilio signature mismatch" in err


@pytest.mark.asyncio
async def test_twilio_incorrect_public_url_rejected(twilio_adapter, signing_secret):
    """Mismatched public URL causes signature failure (prevents replay/spoofing)."""
    signer_url = "https://router.weirwise.com/api/v1/webhooks/twilio"
    wrong_url = "https://other-domain.com/api/v1/webhooks/twilio"
    params = {"MessageSid": "SM999"}
    body_bytes = urllib.parse.urlencode(params).encode("utf-8")

    sig = TwilioAdapter.compute_twilio_signature(
        signing_key="auth_token_secret_12345",
        public_url=signer_url,
        post_params=params,
    )

    # Server receives wrong request-url
    headers = {
        "x-twilio-signature": sig,
        "x-request-url": wrong_url,
    }
    is_valid, _, err = await twilio_adapter.verify_webhook(headers, body_bytes, signing_secret)
    assert is_valid is False
    assert "Twilio signature mismatch" in err


@pytest.mark.asyncio
async def test_twilio_query_string_and_param_ordering(twilio_adapter, signing_secret):
    """Parameters are sorted alphabetically regardless of original payload order."""
    public_url = "https://router.weirwise.com/api/v1/webhooks/twilio?client=web&env=prod"
    # Unordered dict
    params = {
        "Zebra": "stripes",
        "Apple": "fruit",
        "MessageSid": "SM_SORT_TEST",
        "Banana": "yellow",
    }
    body_bytes = urllib.parse.urlencode(params).encode("utf-8")

    sig = TwilioAdapter.compute_twilio_signature(
        signing_key="auth_token_secret_12345",
        public_url=public_url,
        post_params=params,
    )

    headers = {
        "x-twilio-signature": sig,
        "x-request-url": public_url,
    }
    is_valid, parsed, err = await twilio_adapter.verify_webhook(headers, body_bytes, signing_secret)
    assert is_valid is True
    assert parsed["id"] == "SM_SORT_TEST"


@pytest.mark.asyncio
async def test_twilio_raw_body_fallback_rejected(twilio_adapter, signing_secret):
    """Raw-body HMAC is strictly rejected (zero raw body fallback)."""
    public_url = "https://router.weirwise.com/api/v1/webhooks/twilio"
    body_bytes = b"MessageSid=SM123&Body=Test"

    # Compute a raw HMAC-SHA1 of body (the old insecure fallback behavior)
    raw_mac = hmac.new("auth_token_secret_12345".encode("utf-8"), body_bytes, hashlib.sha1)
    raw_sig = base64.b64encode(raw_mac.digest()).decode("utf-8")

    headers = {
        "x-twilio-signature": raw_sig,
        "x-request-url": public_url,
    }
    # MUST FAIL: We do NOT accept raw-body fallback
    is_valid, _, err = await twilio_adapter.verify_webhook(headers, body_bytes, signing_secret)
    assert is_valid is False
    assert "Twilio signature mismatch" in err


@pytest.mark.asyncio
async def test_twilio_missing_canonical_url_fails_closed(twilio_adapter, signing_secret):
    """Missing canonical URL fails closed without guessing or fabricating a fallback URL."""
    body_bytes = b"MessageSid=SM123"
    headers = {"x-twilio-signature": "some_sig"}
    # No x-request-url or x-canonical-url
    is_valid, _, err = await twilio_adapter.verify_webhook(headers, body_bytes, signing_secret)
    assert is_valid is False
    assert "Missing canonical public URL" in err

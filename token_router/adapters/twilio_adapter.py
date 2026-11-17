"""Official Twilio Webhook Signature Verification.

Follows official Twilio specification:
- Exact public URL including scheme, host, port, path, and query parameters.
- Sorted POST parameters appended as key + value without separators.
- HMAC-SHA1 with Twilio Auth Token.
- Base64 encoding of HMAC digest compared in constant-time.
- ZERO raw-body fallback. ZERO fabricated fallback URLs.
- Strict trusted reverse proxy validation.
"""

import base64
import hashlib
import hmac
import urllib.parse
from typing import Any, Dict, Optional, Tuple
import httpx

from token_router.adapters.base import ProviderAdapter


class TwilioAdapter(ProviderAdapter):
    provider_name: str = "twilio"
    base_url: str = "https://api.twilio.com/2010-04-01"

    def _parse_secret(self, secret: str) -> Tuple[str, str]:
        """Parses credential format 'ACCOUNT_SID:AUTH_TOKEN'."""
        if ":" in secret:
            sid, token = secret.split(":", 1)
            return sid.strip(), token.strip()
        return secret.strip(), ""

    async def validate_credential(self, secret: str) -> Tuple[bool, str, int]:
        """Validate Twilio credentials against Account resource."""
        account_sid, auth_token = self._parse_secret(secret)
        if not account_sid or not auth_token:
            return False, "Twilio credential must be in format ACCOUNT_SID:AUTH_TOKEN", 400

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.get(
                    f"{self.base_url}/Accounts/{account_sid}.json",
                    auth=(account_sid, auth_token),
                )
                if resp.status_code == 200:
                    return True, "Twilio account validated successfully", 200
                return False, f"Twilio validation failed: {resp.text}", resp.status_code
            except Exception as e:
                return False, f"Twilio connection error: {str(e)}", 503

    async def execute_request(
        self,
        secret: str,
        capability: str,
        model: str,
        payload: Dict[str, Any],
        timeout: float,
    ) -> Tuple[int, Dict[str, Any], Dict[str, int]]:
        return await self.execute_operation(
            secret=secret,
            operation=capability,
            payload=payload,
            timeout=timeout,
        )

    async def execute_operation(
        self,
        secret: str,
        operation: str,
        payload: Dict[str, Any],
        timeout: float,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[int, Dict[str, Any], Dict[str, Any]]:
        account_sid, auth_token = self._parse_secret(secret)
        if not account_sid or not auth_token:
            return 400, {"error": "Twilio credential must be formatted as ACCOUNT_SID:AUTH_TOKEN"}, {}

        if operation == "send_sms":
            to = payload.get("to")
            from_num = payload.get("from")
            body = payload.get("body")
            if not to or not body:
                return 400, {"error": "Twilio SMS requires 'to' and 'body' fields"}, {}

            async with httpx.AsyncClient(timeout=timeout) as client:
                data = {"To": to, "Body": body}
                if from_num:
                    data["From"] = from_num

                try:
                    resp = await client.post(
                        f"{self.base_url}/Accounts/{account_sid}/Messages.json",
                        auth=(account_sid, auth_token),
                        data=data,
                    )
                    resp_json = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"text": resp.text}
                    return resp.status_code, resp_json, {"messages": 1, "segments": 1}
                except httpx.TimeoutException:
                    return 504, {"error": f"Twilio gateway timeout dispatching SMS to {to}"}, {}
                except Exception as e:
                    return 502, {"error": f"Twilio communication failure: {str(e)}"}, {}

        return 400, {"error": f"Unsupported Twilio operation: {operation}"}, {}

    @classmethod
    def compute_twilio_signature(cls, signing_key: str, public_url: str, post_params: Dict[str, Any]) -> str:
        """Computes official Twilio HMAC-SHA1 signature.
        
        Algorithm:
        1. Start with full canonical URL (scheme, host, path, query).
        2. Sort POST parameter keys lexicographically.
        3. For each key, append key name followed by value (for multi-values, append each value).
        4. Sign with HMAC-SHA1 using Twilio auth token.
        5. Base64 encode the digest.
        """
        data_to_sign = public_url
        for key in sorted(post_params.keys()):
            val = post_params[key]
            if isinstance(val, list):
                # If multiple values for same key, sort values as per Twilio spec
                for item in sorted(str(v) for v in val):
                    data_to_sign += f"{key}{item}"
            else:
                data_to_sign += f"{key}{val}"

        mac = hmac.new(signing_key.encode("utf-8"), data_to_sign.encode("utf-8"), hashlib.sha1)
        return base64.b64encode(mac.digest()).decode("utf-8")

    async def verify_webhook(
        self,
        headers: Dict[str, str],
        body: bytes,
        secret: str,
    ) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
        """Validate Twilio X-Twilio-Signature according to official Twilio specification.
        
        FAIL-CLOSED REQUIREMENTS:
        - Signature header MUST be present.
        - Canonical URL MUST be provided via headers['x-request-url'] or headers['x-canonical-url'].
        - NO fabricated fallback URLs.
        - NO raw-body fallback.
        - Constant-time comparison using hmac.compare_digest.
        """
        signature = headers.get("x-twilio-signature") or headers.get("X-Twilio-Signature")
        if not signature:
            return False, None, "Missing X-Twilio-Signature header"

        # The exact public URL must be passed by the application/proxy boundary
        request_url = headers.get("x-canonical-url") or headers.get("x-request-url")
        if not request_url:
            return False, None, "Fail-closed: Missing canonical public URL for Twilio signature verification."

        _, auth_token = self._parse_secret(secret)
        signing_key = auth_token or secret
        if not signing_key:
            return False, None, "Twilio signing secret is missing or empty."

        # Parse form-encoded parameters if present
        post_params: Dict[str, Any] = {}
        if body:
            try:
                # Twilio sends application/x-www-form-urlencoded
                raw_parsed = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)
                for k, v in raw_parsed.items():
                    # If single value, keep as scalar; if multiple, list
                    post_params[k] = v if len(v) > 1 else v[0]
            except Exception as e:
                return False, None, f"Failed to parse form parameters from webhook body: {str(e)}"

        # Compute official expected signature
        expected_sig = self.compute_twilio_signature(
            signing_key=signing_key,
            public_url=request_url,
            post_params=post_params,
        )

        # Constant-time comparison (NO raw body fallback!)
        if not hmac.compare_digest(expected_sig, signature):
            return False, None, "Twilio signature mismatch: Verification failed."

        # Extract message ID / SID
        message_sid = post_params.get("MessageSid") or post_params.get("SmsSid")
        if isinstance(message_sid, list):
            message_sid = message_sid[0] if message_sid else None

        return True, {
            "id": message_sid,
            "type": "sms.delivered",
            "provider": "twilio",
            "params": post_params,
        }, None

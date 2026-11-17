"""Twilio SMS and Communications Provider Adapter.

Handles outbound SMS dispatch and Twilio signature verification.
"""

import base64
import hashlib
import hmac
from typing import Any, Dict, Tuple, Optional
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
            return 400, {"error": "Invalid Twilio credentials format (must be SID:TOKEN)"}, {}

        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                if operation in ("send_sms", "sms"):
                    form_data = {
                        "To": payload.get("to"),
                        "From": payload.get("from"),
                        "Body": payload.get("body", payload.get("text", "")),
                    }
                    if "media_url" in payload:
                        form_data["MediaUrl"] = payload["media_url"]

                    resp = await client.post(
                        f"{self.base_url}/Accounts/{account_sid}/Messages.json",
                        data=form_data,
                        auth=(account_sid, auth_token),
                    )
                    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"text": resp.text}
                    return resp.status_code, data, {"segments": 1}
                else:
                    return 400, {"error": f"Unsupported Twilio operation: {operation}"}, {}
            except Exception as e:
                return 502, {"error": f"Twilio gateway error: {str(e)}"}, {}

    async def verify_webhook(
        self,
        headers: Dict[str, str],
        body: bytes,
        secret: str,
    ) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
        """Validate Twilio X-Twilio-Signature according to Twilio standard specification:

        HMAC-SHA1 of full URL + sorted POST parameter key-values, compared in constant time.
        """
        signature = headers.get("x-twilio-signature") or headers.get("X-Twilio-Signature")
        if not signature:
            return False, None, "Missing X-Twilio-Signature header"

        # Obtain auth token from secret (ACCOUNT_SID:AUTH_TOKEN or raw token)
        _, auth_token = self._parse_secret(secret)
        signing_key = auth_token or secret

        try:
            # Twilio canonical signature input: full URL + sorted POST param key-values
            # When receiving via reverse proxy, X-Forwarded-Url or URL header may be present
            request_url = headers.get("x-forwarded-url") or headers.get("x-original-url") or "https://token-router/api/v1/webhooks/twilio"

            # Parse POST parameters if form-encoded
            import urllib.parse
            data_to_sign = request_url
            try:
                parsed_params = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)
                for key in sorted(parsed_params.keys()):
                    # parse_qs returns list of values; Twilio takes the first/single value
                    val = parsed_params[key][0] if parsed_params[key] else ""
                    data_to_sign += f"{key}{val}"
            except Exception:
                pass

            mac = hmac.new(signing_key.encode("utf-8"), data_to_sign.encode("utf-8"), hashlib.sha1)
            expected_sig = base64.b64encode(mac.digest()).decode("utf-8")

            if not hmac.compare_digest(expected_sig, signature):
                # Also allow raw-body fallback check if parameters parsing differs
                mac_raw = hmac.new(signing_key.encode("utf-8"), body, hashlib.sha1)
                expected_raw = base64.b64encode(mac_raw.digest()).decode("utf-8")
                if not hmac.compare_digest(expected_raw, signature):
                    return False, None, "Invalid Twilio signature"

            # Parse message SID and body
            event_id = headers.get("message-sid") or headers.get("MessageSid")
            if not event_id:
                try:
                    p = urllib.parse.parse_qs(body.decode("utf-8"))
                    event_id = p.get("MessageSid", [None])[0] or p.get("SmsSid", [None])[0]
                except Exception:
                    pass

            return True, {
                "id": event_id,
                "type": "sms.delivered",
                "provider": "twilio",
            }, None
        except Exception as e:
            return False, None, f"Twilio signature verification error: {str(e)}"

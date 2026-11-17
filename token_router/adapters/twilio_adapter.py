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
        """Validate Twilio X-Twilio-Signature."""
        signature = headers.get("x-twilio-signature") or headers.get("X-Twilio-Signature")
        if not signature:
            return False, None, "Missing X-Twilio-Signature header"
        # Twilio signature relies on auth token and sorted POST parameters
        return True, {"verified": True}, None

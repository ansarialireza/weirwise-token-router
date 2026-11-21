"""Stripe Payment Provider Adapter.

Handles payment intents, refunds, and webhook verification with strict idempotency.
Zero automatic failover on network timeouts to prevent duplicate charges.
"""

import hmac
import hashlib
import json
import time
from typing import Any, Dict, Tuple, Optional
import httpx

from token_router.adapters.base import ProviderAdapter
from token_router.core.http import get_http_client


class StripeAdapter(ProviderAdapter):
    provider_name: str = "stripe"
    base_url: str = "https://api.stripe.com/v1"

    async def validate_credential(self, secret: str) -> Tuple[bool, str, int]:
        """Validate Stripe Secret Key against /v1/balance endpoint."""
        client = await get_http_client(timeout=10.0)
        try:
            resp = await client.get(
                f"{self.base_url}/balance",
                headers={"Authorization": f"Bearer {secret}"},
            )
            if resp.status_code == 200:
                return True, "Stripe secret key valid and balance accessible", 200
            return False, f"Stripe validation failed: {resp.text}", resp.status_code
        except Exception as e:
            return False, f"Stripe validation connection error: {str(e)}", 503

    async def execute_request(
        self,
        secret: str,
        capability: str,
        model: str,
        payload: Dict[str, Any],
        timeout: float,
    ) -> Tuple[int, Dict[str, Any], Dict[str, int]]:
        """Executes payment requests."""
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
        """Executes Stripe operations (create_payment_intent, refund_payment, get_balance)."""
        headers = {
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        client = await get_http_client(timeout=timeout)
        try:
            if operation == "create_payment_intent":
                # Form-encoded Stripe payload
                form_data = {
                    "amount": payload.get("amount"),
                    "currency": payload.get("currency", "usd"),
                }
                if "customer" in payload:
                    form_data["customer"] = payload["customer"]
                if "payment_method" in payload:
                    form_data["payment_method"] = payload["payment_method"]
                if payload.get("confirm"):
                    form_data["confirm"] = "true"

                resp = await client.post(
                    f"{self.base_url}/payment_intents",
                    data=form_data,
                    headers=headers,
                    timeout=timeout,
                )
                data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"text": resp.text}
                return resp.status_code, data, {"transactions": 1}

            elif operation == "refund_payment":
                form_data = {"payment_intent": payload.get("payment_intent")}
                if "amount" in payload:
                    form_data["amount"] = payload["amount"]
                resp = await client.post(
                    f"{self.base_url}/refunds",
                    data=form_data,
                    headers=headers,
                    timeout=timeout,
                )
                data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"text": resp.text}
                return resp.status_code, data, {"transactions": 1}

            elif operation == "get_balance":
                resp = await client.get(
                    f"{self.base_url}/balance",
                    headers=headers,
                    timeout=timeout,
                )
                data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"text": resp.text}
                return resp.status_code, data, {"queries": 1}

            else:
                return 400, {"error": f"Unsupported Stripe operation: {operation}"}, {}

        except httpx.TimeoutException:
            # CRITICAL: Note that transaction status is unknown!
            return 504, {
                "error": "Stripe gateway timeout. Transaction status UNCERTAIN. DO NOT retry automatically without balance/intent verification."
            }, {}
        except Exception as e:
            return 502, {"error": f"Stripe gateway communication error: {str(e)}"}, {}

    async def verify_webhook(
        self,
        headers: Dict[str, str],
        body: bytes,
        secret: str,
    ) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
        """Verify Stripe webhook signature (Stripe-Signature header with timestamp t and signature v1)."""
        sig_header = headers.get("stripe-signature") or headers.get("Stripe-Signature")
        if not sig_header:
            return False, None, "Missing Stripe-Signature header"

        try:
            elements = dict(item.strip().split("=", 1) for item in sig_header.split(","))
            timestamp = elements.get("t")
            v1_sig = elements.get("v1")

            if not timestamp or not v1_sig:
                return False, None, "Invalid Stripe-Signature format"

            # Check timestamp tolerance (e.g. 5 minutes)
            if abs(time.time() - int(timestamp)) > 300:
                return False, None, "Webhook timestamp exceeds 300s tolerance"

            # Compute HMAC-SHA256
            signed_payload = f"{timestamp}.".encode("utf-8") + body
            computed_sig = hmac.new(
                secret.encode("utf-8"),
                signed_payload,
                hashlib.sha256,
            ).hexdigest()

            if not hmac.compare_digest(computed_sig, v1_sig):
                return False, None, "Webhook signature mismatch"

            parsed_event = json.loads(body.decode("utf-8"))
            return True, parsed_event, None
        except Exception as e:
            return False, None, f"Webhook signature verification error: {str(e)}"

import httpx
from typing import Any, AsyncGenerator, Dict, Tuple
from token_router.adapters.base import ProviderAdapter


class ResendAdapter(ProviderAdapter):
    provider_name = "resend"
    base_url = "https://api.resend.com"

    async def validate_credential(self, secret: str) -> Tuple[bool, str, int]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.get(
                    f"{self.base_url}/api-keys",
                    headers={"Authorization": f"Bearer {secret}"},
                )
                if resp.status_code == 200:
                    return True, "Credential valid and active", 200
                elif resp.status_code in (401, 403):
                    return False, "Authentication failed: invalid Resend API key", resp.status_code
                return False, f"Provider returned status {resp.status_code}", resp.status_code
            except Exception as e:
                return False, f"Connection error: {str(e)}", 502

    async def execute_request(
        self,
        secret: str,
        capability: str,
        model: str,
        payload: Dict[str, Any],
        timeout: float,
    ) -> Tuple[int, Dict[str, Any], Dict[str, int]]:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{self.base_url}/emails",
                headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
                json=payload,
            )
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"raw": resp.text}
            return resp.status_code, data, {"total_tokens": 1}

    async def execute_stream(
        self,
        secret: str,
        capability: str,
        model: str,
        payload: Dict[str, Any],
        timeout: float,
    ) -> AsyncGenerator[str, None]:
        yield "Resend transactional email does not stream tokens."

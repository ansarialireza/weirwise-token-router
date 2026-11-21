import httpx
from typing import Any, AsyncGenerator, Dict, Tuple
from token_router.adapters.base import ProviderAdapter


class AnthropicAdapter(ProviderAdapter):
    provider_name = "anthropic"
    base_url = "https://api.anthropic.com/v1"

    async def validate_credential(self, secret: str) -> Tuple[bool, str, int]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.get(
                    f"{self.base_url}/models",
                    headers={"x-api-key": secret, "anthropic-version": "2023-06-01"},
                )
                if resp.status_code == 200:
                    return True, "Credential valid and active", 200
                elif resp.status_code in (401, 403):
                    return False, "Authentication failed: invalid x-api-key", resp.status_code
                elif resp.status_code == 429:
                    return False, "Rate limited or credit exhausted", 429
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
        code, data, tokens, _ = await self.execute_request_with_headers(
            secret=secret,
            capability=capability,
            model=model,
            payload=payload,
            timeout=timeout,
        )
        return code, data, tokens

    async def execute_request_with_headers(
        self,
        secret: str,
        capability: str,
        model: str,
        payload: Dict[str, Any],
        timeout: float,
    ) -> Tuple[int, Dict[str, Any], Dict[str, int], Dict[str, str]]:
        req_payload = dict(payload)
        if model != "auto":
            req_payload["model"] = model
        elif "model" not in req_payload:
            req_payload["model"] = "claude-3-5-haiku-20241022"

        if "max_tokens" not in req_payload:
            req_payload["max_tokens"] = 1024

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{self.base_url}/messages",
                headers={
                    "x-api-key": secret,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json=req_payload,
            )
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"raw": resp.text}
            usage = data.get("usage", {})
            tokens = {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            }
            return resp.status_code, data, tokens, dict(resp.headers)

    async def execute_stream(
        self,
        secret: str,
        capability: str,
        model: str,
        payload: Dict[str, Any],
        timeout: float,
    ) -> AsyncGenerator[str, None]:
        req_payload = dict(payload)
        req_payload["stream"] = True
        if model != "auto":
            req_payload["model"] = model
        elif "model" not in req_payload:
            req_payload["model"] = "claude-3-5-haiku-20241022"

        if "max_tokens" not in req_payload:
            req_payload["max_tokens"] = 1024

        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/messages",
                headers={
                    "x-api-key": secret,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json=req_payload,
            ) as resp:
                async for chunk in resp.aiter_lines():
                    if chunk:
                        yield chunk

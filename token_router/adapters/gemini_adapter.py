import httpx
from typing import Any, AsyncGenerator, Dict, Tuple
from token_router.adapters.base import ProviderAdapter


class GeminiAdapter(ProviderAdapter):
    provider_name = "gemini"
    base_url = "https://generativelanguage.googleapis.com/v1beta"

    async def validate_credential(self, secret: str) -> Tuple[bool, str, int]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.get(
                    f"{self.base_url}/models",
                    headers={"x-goog-api-key": secret},
                )
                if resp.status_code == 200:
                    return True, "Credential valid and active", 200
                elif resp.status_code in (400, 401, 403):
                    return False, "Authentication failed: invalid Gemini API key", resp.status_code
                elif resp.status_code == 429:
                    return False, "Rate limited or quota exceeded", 429
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
        actual_model = model if model != "auto" else "gemini-1.5-flash"
        endpoint = f"{self.base_url}/models/{actual_model}:generateContent"

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                endpoint,
                headers={"x-goog-api-key": secret, "Content-Type": "application/json"},
                json=payload,
            )
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"raw": resp.text}
            usage = data.get("usageMetadata", {})
            tokens = {
                "input_tokens": usage.get("promptTokenCount", 0),
                "output_tokens": usage.get("candidatesTokenCount", 0),
                "total_tokens": usage.get("totalTokenCount", 0),
            }
            return resp.status_code, data, tokens

    async def execute_stream(
        self,
        secret: str,
        capability: str,
        model: str,
        payload: Dict[str, Any],
        timeout: float,
    ) -> AsyncGenerator[str, None]:
        actual_model = model if model != "auto" else "gemini-1.5-flash"
        endpoint = f"{self.base_url}/models/{actual_model}:streamGenerateContent?alt=sse"

        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                endpoint,
                headers={"x-goog-api-key": secret, "Content-Type": "application/json"},
                json=payload,
            ) as resp:
                async for chunk in resp.aiter_lines():
                    if chunk:
                        yield chunk

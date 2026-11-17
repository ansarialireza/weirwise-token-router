import httpx
from typing import Any, AsyncGenerator, Dict, Tuple
from token_router.adapters.base import ProviderAdapter


class OpenAIAdapter(ProviderAdapter):
    provider_name = "openai"
    base_url = "https://api.openai.com/v1"

    async def validate_credential(self, secret: str) -> Tuple[bool, str, int]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.get(
                    f"{self.base_url}/models",
                    headers={"Authorization": f"Bearer {secret}"},
                )
                if resp.status_code == 200:
                    return True, "Credential valid and active", 200
                elif resp.status_code in (401, 403):
                    return False, "Authentication failed: invalid API key", resp.status_code
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
        req_payload = dict(payload)
        if model != "auto":
            req_payload["model"] = model
        elif "model" not in req_payload:
            req_payload["model"] = "gpt-4o-mini"

        endpoint = f"{self.base_url}/chat/completions" if capability == "chat" else f"{self.base_url}/embeddings"
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                endpoint,
                headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
                json=req_payload,
            )
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"raw": resp.text}
            usage = data.get("usage", {})
            tokens = {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
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
        req_payload = dict(payload)
        req_payload["stream"] = True
        if model != "auto":
            req_payload["model"] = model
        elif "model" not in req_payload:
            req_payload["model"] = "gpt-4o-mini"

        endpoint = f"{self.base_url}/chat/completions"
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                endpoint,
                headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
                json=req_payload,
            ) as resp:
                async for chunk in resp.aiter_lines():
                    if chunk:
                        yield chunk

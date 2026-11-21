import httpx
from typing import Any, AsyncGenerator, Dict, Tuple
from token_router.adapters.base import ProviderAdapter
from token_router.core.http import get_http_client


class JinaAdapter(ProviderAdapter):
    provider_name = "jina"
    base_url = "https://api.jina.ai/v1"

    async def validate_credential(self, secret: str) -> Tuple[bool, str, int]:
        client = await get_http_client(timeout=10.0)
        try:
            resp = await client.get(
                f"{self.base_url}/models",
                headers={"Authorization": f"Bearer {secret}"},
            )
            if resp.status_code == 200:
                return True, "Credential valid and active", 200
            elif resp.status_code in (401, 403):
                return False, "Authentication failed: invalid Jina Bearer token", resp.status_code
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
        actual_model = model if model != "auto" else "jina-embeddings-v3"
        req_payload = dict(payload)
        req_payload["model"] = actual_model

        client = await get_http_client(timeout=timeout)
        resp = await client.post(
            f"{self.base_url}/embeddings",
            headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
            json=req_payload,
            timeout=timeout,
        )
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"raw": resp.text}
        usage = data.get("usage", {})
        tokens = {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": 0,
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
        yield "Jina embeddings does not support token streaming."

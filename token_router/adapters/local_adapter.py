"""Local Inference Provider Adapter.

Supports local inference engines (Ollama, LocalAI, vLLM, LM Studio)
exposing an OpenAI-compatible /v1/chat/completions endpoint or native Ollama /api.
Includes configurable health-check probes and strictly validates availability
before routing requests, guaranteeing that offline local engines fail over cleanly.
"""

import json
from typing import Any, AsyncGenerator, Dict, Optional, Tuple
import httpx
from token_router.adapters.base import ProviderAdapter
from token_router.core.http import get_http_client


class LocalAdapter(ProviderAdapter):
    provider_name = "local"
    default_base_url = "http://127.0.0.1:11434"  # Default Ollama / local endpoint

    def _get_url(self, secret: str) -> str:
        """Secret can be empty or can specify a custom endpoint URL e.g. http://192.168.1.50:11434."""
        if secret and (secret.startswith("http://") or secret.startswith("https://")):
            return secret.rstrip("/")
        return self.default_base_url

    async def check_health(self, base_url: str = default_base_url, timeout: float = 2.0) -> Tuple[bool, str, Dict[str, Any]]:
        """Probes the local endpoint to verify whether the daemon is actively running and responsive."""
        client = await get_http_client(timeout=timeout)
        try:
            # 1. Try standard OpenAI-compatible /v1/models
            resp = await client.get(f"{base_url}/v1/models", timeout=timeout)
            if resp.status_code == 200:
                models = [m.get("id") for m in resp.json().get("data", [])]
                return True, "Local OpenAI-compatible engine responsive", {"models": models, "type": "v1"}
        except Exception:
            pass

        try:
            # 2. Try native Ollama /api/tags
            resp = await client.get(f"{base_url}/api/tags", timeout=timeout)
            if resp.status_code == 200:
                models = [m.get("name") for m in resp.json().get("models", [])]
                return True, "Local Ollama daemon responsive", {"models": models, "type": "ollama"}
        except Exception:
            pass

        return False, f"Local inference engine unreachable at {base_url}", {}

    async def validate_credential(self, secret: str) -> Tuple[bool, str, int]:
        url = self._get_url(secret)
        is_healthy, msg, _ = await self.check_health(url, timeout=3.0)
        if is_healthy:
            return True, f"Local engine verified at {url}: {msg}", 200
        return False, f"Local engine validation failed: {msg}", 503

    async def execute_request(
        self,
        secret: str,
        capability: str,
        model: str,
        payload: Dict[str, Any],
        timeout: float,
    ) -> Tuple[int, Dict[str, Any], Dict[str, int]]:
        url = self._get_url(secret)
        actual_model = model if model != "auto" else "llama3.2:latest"

        # Check if native Ollama or OpenAI-compatible
        endpoint = f"{url}/v1/chat/completions"
        req_payload = dict(payload)
        req_payload["model"] = actual_model

        client = await get_http_client(timeout=timeout)
        try:
            resp = await client.post(
                endpoint,
                headers={"Content-Type": "application/json"},
                json=req_payload,
                timeout=timeout,
            )
            if resp.status_code == 404:
                # Fallback to Ollama native /api/chat
                resp = await client.post(
                    f"{url}/api/chat",
                    headers={"Content-Type": "application/json"},
                    json={
                        "model": actual_model,
                        "messages": req_payload.get("messages", []),
                        "stream": False,
                    },
                    timeout=timeout,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    tokens = {
                        "input_tokens": data.get("prompt_eval_count", 0),
                        "output_tokens": data.get("eval_count", 0),
                        "total_tokens": data.get("prompt_eval_count", 0) + data.get("eval_count", 0),
                    }
                    return 200, data, tokens

            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"raw": resp.text}
            usage = data.get("usage", {})
            tokens = {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            }
            return resp.status_code, data, tokens
        except httpx.ConnectError as e:
            return 503, {"error": f"Local provider connection refused: {str(e)}"}, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        except Exception as e:
            return 500, {"error": f"Local provider execution error: {str(e)}"}, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    async def execute_stream(
        self,
        secret: str,
        capability: str,
        model: str,
        payload: Dict[str, Any],
        timeout: float,
    ) -> AsyncGenerator[str, None]:
        url = self._get_url(secret)
        actual_model = model if model != "auto" else "llama3.2:latest"
        endpoint = f"{url}/v1/chat/completions"
        req_payload = dict(payload)
        req_payload["model"] = actual_model
        req_payload["stream"] = True

        client = await get_http_client(timeout=timeout)
        async with client.stream("POST", endpoint, json=req_payload, timeout=timeout) as resp:
            async for chunk in resp.aiter_lines():
                if chunk:
                    yield chunk

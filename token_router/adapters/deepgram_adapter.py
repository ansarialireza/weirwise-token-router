import httpx
from typing import Any, AsyncGenerator, Dict, Tuple
from token_router.adapters.base import ProviderAdapter
from token_router.core.http import get_http_client


class DeepgramAdapter(ProviderAdapter):
    provider_name = "deepgram"
    base_url = "https://api.deepgram.com/v1"

    async def validate_credential(self, secret: str) -> Tuple[bool, str, int]:
        client = await get_http_client(timeout=10.0)
        try:
            resp = await client.get(
                f"{self.base_url}/projects",
                headers={"Authorization": f"Token {secret}"},
            )
            if resp.status_code == 200:
                return True, "Credential valid and active", 200
            elif resp.status_code in (401, 403):
                return False, "Authentication failed: invalid Deepgram Token", resp.status_code
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
        # Text to Speech endpoint
        if capability == "text-to-speech":
            text_data = payload.get("text", "")
            voice_model = model if model != "auto" else "aura-asteria-en"
            client = await get_http_client(timeout=timeout)
            resp = await client.post(
                f"{self.base_url}/speak?model={voice_model}",
                headers={"Authorization": f"Token {secret}", "Content-Type": "application/json"},
                json={"text": text_data},
                timeout=timeout,
            )
            return resp.status_code, {"audio_bytes": len(resp.content)}, {"total_tokens": len(text_data.split())}
        else:
            return 400, {"error": "Unsupported capability for Deepgram HTTP REST (Use WS for stream)"}, {}

    async def execute_stream(
        self,
        secret: str,
        capability: str,
        model: str,
        payload: Dict[str, Any],
        timeout: float,
    ) -> AsyncGenerator[str, None]:
        yield "Deepgram streaming is implemented over WebSocket protocol"

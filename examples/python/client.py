"""Python Client SDK for Weirwise Token Router."""

from typing import Any, Dict, Optional
import httpx


class TokenRouterClient:
    """Client for routing AI/LLM/infra requests through the independent Token Router."""

    def __init__(
        self,
        base_url: str,
        consumer_api_key: str,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.consumer_api_key = consumer_api_key
        self.timeout = timeout

    async def route_chat(
        self,
        messages: list[Dict[str, str]],
        provider: Optional[str] = None,
        model: str = "auto",
        strategy: Optional[str] = None,
        max_failover_attempts: int = 3,
        **extra_params,
    ) -> Dict[str, Any]:
        """Routes a chat completion request to an active healthy credential."""
        payload = {
            "provider": provider,
            "capability": "chat",
            "model": model,
            "strategy": strategy,
            "max_failover_attempts": max_failover_attempts,
            "request": {"messages": messages, **extra_params},
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/api/v1/route",
                headers={"X-Consumer-Key": self.consumer_api_key, "Content-Type": "application/json"},
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()

    async def route_embeddings(
        self,
        input_texts: list[str],
        provider: Optional[str] = None,
        model: str = "auto",
    ) -> Dict[str, Any]:
        """Routes an embedding generation request."""
        payload = {
            "provider": provider,
            "capability": "embeddings",
            "model": model,
            "request": {"input": input_texts},
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/api/v1/route",
                headers={"X-Consumer-Key": self.consumer_api_key, "Content-Type": "application/json"},
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()

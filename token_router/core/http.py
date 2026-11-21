"""Centralized Lifecycle-Managed HTTP Transport & Connection Pooling.

Provides:
1. Singleton HTTP connection pool (httpx.AsyncClient) with configurable limits,
   keep-alive, connection timeouts, and connection lifecycle management.
2. Per-provider connection limits and timeouts.
3. Event-loop aware lifecycle management integrated with FastAPI lifespan.
4. Clean shutdown and prevention of connection leaks.
"""

import asyncio
import logging
from typing import Dict, Optional
import httpx

from token_router.core.config import settings

logger = logging.getLogger(__name__)


class HTTPTransportManager:
    """Singleton connection pool and transport manager for upstream provider communication."""

    _instance: Optional["HTTPTransportManager"] = None
    _lock = asyncio.Lock()

    def __init__(
        self,
        max_connections: int = 200,
        max_keepalive_connections: int = 50,
        keepalive_expiry: float = 60.0,
        default_timeout: float = 30.0,
    ):
        self.max_connections = max_connections
        self.max_keepalive_connections = max_keepalive_connections
        self.keepalive_expiry = keepalive_expiry
        self.default_timeout = default_timeout

        self._limits = httpx.Limits(
            max_connections=self.max_connections,
            max_keepalive_connections=self.max_keepalive_connections,
            keepalive_expiry=self.keepalive_expiry,
        )
        self._shared_client: Optional[httpx.AsyncClient] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @classmethod
    def get_instance(cls) -> "HTTPTransportManager":
        if cls._instance is None:
            cls._instance = HTTPTransportManager(
                max_connections=getattr(settings, "HTTP_MAX_CONNECTIONS", 200),
                max_keepalive_connections=getattr(settings, "HTTP_MAX_KEEPALIVE", 50),
                keepalive_expiry=getattr(settings, "HTTP_KEEPALIVE_EXPIRY", 60.0),
                default_timeout=getattr(settings, "DEFAULT_TIMEOUT_SECONDS", 30.0),
            )
        return cls._instance

    async def get_client(self, timeout: Optional[float] = None) -> httpx.AsyncClient:
        """Returns the shared, pooled httpx.AsyncClient instance."""
        current_loop = asyncio.get_running_loop()
        if self._shared_client is None or self._shared_client.is_closed or self._loop != current_loop:
            async with self._lock:
                if self._shared_client is None or self._shared_client.is_closed or self._loop != current_loop:
                    if self._shared_client and not self._shared_client.is_closed:
                        try:
                            await self._shared_client.aclose()
                        except Exception:
                            pass
                    self._loop = current_loop
                    timeout_config = httpx.Timeout(
                        timeout or self.default_timeout,
                        connect=10.0,
                        read=timeout or self.default_timeout,
                        write=10.0,
                        pool=10.0,
                    )
                    self._shared_client = httpx.AsyncClient(
                        limits=self._limits,
                        timeout=timeout_config,
                        follow_redirects=True,
                    )
                    logger.debug("Initialized shared HTTP connection pool with max=%d, keepalive=%d", self.max_connections, self.max_keepalive_connections)
        return self._shared_client

    async def close(self) -> None:
        """Closes all active pooled HTTP connections gracefully."""
        async with self._lock:
            if self._shared_client is not None and not self._shared_client.is_closed:
                try:
                    await self._shared_client.aclose()
                    logger.debug("Closed shared HTTP connection pool.")
                except Exception as e:
                    logger.warning("Error closing shared HTTP connection pool: %s", str(e))
                finally:
                    self._shared_client = None
                    self._loop = None


async def get_http_client(timeout: Optional[float] = None) -> httpx.AsyncClient:
    """Helper function to obtain the shared pooled HTTP client."""
    manager = HTTPTransportManager.get_instance()
    return await manager.get_client(timeout=timeout)


async def close_http_client() -> None:
    """Helper function to shut down the shared HTTP client."""
    manager = HTTPTransportManager.get_instance()
    await manager.close()

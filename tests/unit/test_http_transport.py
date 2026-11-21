"""Unit and concurrency tests for HTTP connection pooling and transport manager."""

import asyncio
import pytest
import httpx

from token_router.core.http import HTTPTransportManager, close_http_client, get_http_client


@pytest.mark.asyncio
async def test_singleton_client_reuse():
    """Verify that multiple calls to get_http_client return the identical pooled client."""
    client1 = await get_http_client()
    client2 = await get_http_client()
    assert client1 is client2
    assert not client1.is_closed


@pytest.mark.asyncio
async def test_graceful_close_and_reopen():
    """Verify graceful shutdown closes the pool and subsequent call creates a new active client."""
    client1 = await get_http_client()
    await close_http_client()
    assert client1.is_closed

    client2 = await get_http_client()
    assert client2 is not client1
    assert not client2.is_closed
    await close_http_client()


@pytest.mark.asyncio
async def test_concurrent_access_safety():
    """Verify concurrent requests get the same initialized client without race conditions."""
    clients = await asyncio.gather(*[get_http_client() for _ in range(50)])
    first_client = clients[0]
    for c in clients:
        assert c is first_client
    await close_http_client()

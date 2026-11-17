import pytest
from httpx import AsyncClient, ASGITransport
from token_router.main import app


@pytest.mark.asyncio
async def test_health_endpoints():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        live = await client.get("/health/live")
        assert live.status_code == 200
        assert live.json()["status"] == "alive"

        ready = await client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"


@pytest.mark.asyncio
async def test_consumer_registration_and_auth():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Register consumer
        admin_headers = {"X-Admin-Key": "wtr_admin_secret_key_change_in_production"}
        res = await client.post(
            "/api/v1/admin/consumers",
            headers=admin_headers,
            json={"name": "test_saas_app", "description": "Test consumer app"},
        )
        assert res.status_code == 201
        consumer_id = res.json()["id"]

        # Generate key
        key_res = await client.post(
            f"/api/v1/admin/consumers/{consumer_id}/keys",
            headers=admin_headers,
            json={"label": "Production Primary Key"},
        )
        assert key_res.status_code == 201
        api_key = key_res.json()["api_key"]
        assert api_key.startswith("wtr_live_")

        # Test unauthorized route attempt without key
        fail_route = await client.post("/api/v1/route", json={"capability": "chat", "request": {}})
        assert fail_route.status_code == 401

        # Test route attempt with valid key (404 because no providers yet)
        consumer_headers = {"X-Consumer-Key": api_key}
        valid_route = await client.post(
            "/api/v1/route",
            headers=consumer_headers,
            json={"capability": "chat", "request": {"messages": [{"role": "user", "content": "hi"}]}},
        )
        assert valid_route.status_code == 404

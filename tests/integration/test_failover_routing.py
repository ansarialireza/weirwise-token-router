import pytest
from unittest.mock import patch
from httpx import AsyncClient, ASGITransport
from token_router.core.config import settings
from token_router.main import app


@pytest.mark.asyncio
async def test_complete_routing_with_failover_and_audit():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        admin_headers = {"X-Admin-Key": settings.ADMIN_API_KEY}

        # 1. Register Consumer
        c_res = await client.post(
            "/api/v1/admin/consumers",
            headers=admin_headers,
            json={"name": "weirwise_platform", "allowed_capabilities": ["chat", "embeddings"]},
        )
        assert c_res.status_code == 201
        consumer_id = c_res.json()["id"]

        # 2. Issue Consumer API Key
        k_res = await client.post(
            f"/api/v1/admin/consumers/{consumer_id}/keys",
            headers=admin_headers,
            json={"label": "Weirwise Prod Key"},
        )
        consumer_api_key = k_res.json()["api_key"]
        consumer_headers = {"X-Consumer-Key": consumer_api_key}

        # 3. Register Provider (OpenAI)
        p_res = await client.post(
            "/api/v1/admin/providers",
            headers=admin_headers,
            json={
                "name": "openai",
                "display_name": "OpenAI Production",
                "capabilities": ["chat", "embeddings"],
                "default_routing_strategy": "priority",
            },
        )
        assert p_res.status_code == 201
        provider_id = p_res.json()["id"]

        # 4. Register two credentials: Cred A (priority 1), Cred B (priority 2)
        cred_a_res = await client.post(
            f"/api/v1/admin/providers/{provider_id}/credentials",
            headers=admin_headers,
            json={"label": "Primary Key", "secret": "sk-proj-primary-key-12345", "priority": 1},
        )
        assert cred_a_res.status_code == 201

        cred_b_res = await client.post(
            f"/api/v1/admin/providers/{provider_id}/credentials",
            headers=admin_headers,
            json={"label": "Backup Key", "secret": "sk-proj-backup-key-67890", "priority": 2},
        )
        assert cred_b_res.status_code == 201

        async def mock_execute(secret, capability, model, payload, timeout):
            if "primary-key" in secret:
                return 429, {"error": {"message": "Rate limit exceeded"}}, {}
            elif "backup-key" in secret:
                return 200, {
                    "id": "chatcmpl-123",
                    "choices": [{"message": {"role": "assistant", "content": "Hello from backup key!"}}],
                }, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
            return 401, {"error": "unauthorized"}, {}

        with patch("token_router.adapters.openai_adapter.OpenAIAdapter.execute_request", side_effect=mock_execute):
            route_res = await client.post(
                "/api/v1/route",
                headers=consumer_headers,
                json={
                    "provider": "openai",
                    "capability": "chat",
                    "model": "gpt-4o-mini",
                    "request": {"messages": [{"role": "user", "content": "Hello"}]},
                },
            )
            assert route_res.status_code == 200
            data = route_res.json()
            assert data["status"] == "success"
            assert data["failover_attempts"] == 1
            assert data["data"]["choices"][0]["message"]["content"] == "Hello from backup key!"
            assert data["usage"]["total_tokens"] == 15

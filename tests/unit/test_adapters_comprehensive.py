"""Comprehensive Unit Tests for all Provider Adapters (OpenAI, Anthropic, Gemini, OpenRouter, Deepgram, Jina, Resend)."""

import pytest
import httpx
from unittest.mock import AsyncMock, patch
from token_router.adapters.registry import get_adapter, list_supported_providers


@pytest.mark.asyncio
async def test_all_adapters_registered():
    providers = list_supported_providers()
    assert "openai" in providers
    assert "anthropic" in providers
    assert "gemini" in providers
    assert "openrouter" in providers
    assert "deepgram" in providers
    assert "jina" in providers
    assert "resend" in providers


@pytest.mark.asyncio
async def test_openai_adapter_execution():
    adapter = get_adapter("openai")
    mock_resp = httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "choices": [{"message": {"role": "assistant", "content": "OpenAI mock response"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        },
        headers={"content-type": "application/json"},
    )
    with patch("httpx.AsyncClient.post", return_value=mock_resp):
        code, data, tokens = await adapter.execute_request(
            secret="sk-mock-key",
            capability="chat",
            model="gpt-4o-mini",
            payload={"messages": [{"role": "user", "content": "hi"}]},
            timeout=10.0,
        )
        assert code == 200
        assert tokens["total_tokens"] == 8
        assert data["choices"][0]["message"]["content"] == "OpenAI mock response"


@pytest.mark.asyncio
async def test_anthropic_adapter_execution():
    adapter = get_adapter("anthropic")
    mock_resp = httpx.Response(
        200,
        json={
            "id": "msg_1",
            "content": [{"type": "text", "text": "Anthropic Claude mock"}],
            "usage": {"input_tokens": 12, "output_tokens": 6},
        },
        headers={"content-type": "application/json"},
    )
    with patch("httpx.AsyncClient.post", return_value=mock_resp):
        code, data, tokens = await adapter.execute_request(
            secret="sk-ant-mock",
            capability="chat",
            model="claude-3-5-haiku-20241022",
            payload={"messages": [{"role": "user", "content": "hi"}]},
            timeout=10.0,
        )
        assert code == 200
        assert tokens["total_tokens"] == 18


@pytest.mark.asyncio
async def test_gemini_adapter_execution():
    adapter = get_adapter("gemini")
    mock_resp = httpx.Response(
        200,
        json={
            "candidates": [{"content": {"parts": [{"text": "Gemini mock"}]}}],
            "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 4, "totalTokenCount": 11},
        },
        headers={"content-type": "application/json"},
    )
    with patch("httpx.AsyncClient.post", return_value=mock_resp):
        code, data, tokens = await adapter.execute_request(
            secret="ai-goog-mock",
            capability="chat",
            model="gemini-1.5-flash",
            payload={"contents": [{"parts": [{"text": "hi"}]}]},
            timeout=10.0,
        )
        assert code == 200
        assert tokens["total_tokens"] == 11


@pytest.mark.asyncio
async def test_jina_adapter_execution():
    adapter = get_adapter("jina")
    mock_resp = httpx.Response(
        200,
        json={
            "data": [{"embedding": [0.1, 0.2, 0.3]}],
            "usage": {"prompt_tokens": 4, "total_tokens": 4},
        },
        headers={"content-type": "application/json"},
    )
    with patch("httpx.AsyncClient.post", return_value=mock_resp):
        code, data, tokens = await adapter.execute_request(
            secret="jina_mock_key",
            capability="embeddings",
            model="jina-embeddings-v3",
            payload={"input": ["test sentence"]},
            timeout=10.0,
        )
        assert code == 200
        assert tokens["total_tokens"] == 4

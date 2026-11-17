from typing import Dict
from token_router.adapters.base import ProviderAdapter
from token_router.adapters.openai_adapter import OpenAIAdapter
from token_router.adapters.anthropic_adapter import AnthropicAdapter
from token_router.adapters.gemini_adapter import GeminiAdapter
from token_router.adapters.openrouter_adapter import OpenRouterAdapter
from token_router.adapters.deepgram_adapter import DeepgramAdapter
from token_router.adapters.jina_adapter import JinaAdapter
from token_router.adapters.resend_adapter import ResendAdapter

_ADAPTERS: Dict[str, ProviderAdapter] = {
    "openai": OpenAIAdapter(),
    "anthropic": AnthropicAdapter(),
    "gemini": GeminiAdapter(),
    "openrouter": OpenRouterAdapter(),
    "deepgram": DeepgramAdapter(),
    "jina": JinaAdapter(),
    "resend": ResendAdapter(),
}


def get_adapter(provider_name: str) -> ProviderAdapter:
    adapter = _ADAPTERS.get(provider_name.lower())
    if not adapter:
        raise ValueError(f"No adapter registered for provider: {provider_name}")
    return adapter


def list_supported_providers() -> list[str]:
    return list(_ADAPTERS.keys())

from typing import Dict, List
from token_router.adapters.anthropic_adapter import AnthropicAdapter
from token_router.adapters.base import ProviderAdapter
from token_router.adapters.deepgram_adapter import DeepgramAdapter
from token_router.adapters.gemini_adapter import GeminiAdapter
from token_router.adapters.jina_adapter import JinaAdapter
from token_router.adapters.openai_adapter import OpenAIAdapter
from token_router.adapters.openrouter_adapter import OpenRouterAdapter
from token_router.adapters.resend_adapter import ResendAdapter
from token_router.adapters.stripe_adapter import StripeAdapter
from token_router.adapters.twilio_adapter import TwilioAdapter
from token_router.adapters.s3_adapter import S3CompatibleAdapter

_ADAPTER_REGISTRY: Dict[str, ProviderAdapter] = {
    # AI & Intelligence
    "openai": OpenAIAdapter(),
    "anthropic": AnthropicAdapter(),
    "gemini": GeminiAdapter(),
    "openrouter": OpenRouterAdapter(),
    "deepgram": DeepgramAdapter(),
    "jina": JinaAdapter(),
    "jina_search": JinaAdapter(),

    # Communication
    "resend": ResendAdapter(),
    "twilio": TwilioAdapter(),

    # Payments & Financial
    "stripe": StripeAdapter(),

    # Storage & Files
    "s3_compatible": S3CompatibleAdapter(),
}


def get_adapter(provider_name: str) -> ProviderAdapter:
    adapter = _ADAPTER_REGISTRY.get(provider_name.lower())
    if not adapter:
        raise ValueError(f"No adapter registered for provider: {provider_name}")
    return adapter


def list_supported_providers() -> List[str]:
    return list(_ADAPTER_REGISTRY.keys())

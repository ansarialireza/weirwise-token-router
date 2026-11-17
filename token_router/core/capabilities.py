"""Central Provider & Capability Registry for Agent discovery and introspection."""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class ServiceDefinition(BaseModel):
    id: str  # e.g. "chat", "embeddings", "speech-to-text", "text-to-speech", "email"
    name: str
    description: str
    supported_providers: List[str]
    input_format: str
    supports_streaming: bool


class ProviderMetadata(BaseModel):
    id: str  # "openai", "anthropic", "gemini", "openrouter", "deepgram", "jina", "resend"
    display_name: str
    description: str
    website: str
    supported_services: List[str]
    models: List[str]
    streaming_supported: bool
    auth_header_type: str  # "Bearer", "x-api-key", "x-goog-api-key", "Token"
    required_credential_fields: List[str] = Field(default_factory=lambda: ["secret"])
    pricing_estimate: Dict[str, Any] = Field(default_factory=dict)


PROVIDER_CATALOG: Dict[str, ProviderMetadata] = {
    "openai": ProviderMetadata(
        id="openai",
        display_name="OpenAI",
        description="Industry standard generative models (GPT-4o, GPT-4o-mini, text-embedding-3).",
        website="https://openai.com",
        supported_services=["chat", "embeddings"],
        models=["gpt-4o", "gpt-4o-mini", "text-embedding-3-small", "text-embedding-3-large"],
        streaming_supported=True,
        auth_header_type="Bearer",
        pricing_estimate={"gpt-4o-mini": {"input_per_m": 0.15, "output_per_m": 0.60}},
    ),
    "anthropic": ProviderMetadata(
        id="anthropic",
        display_name="Anthropic",
        description="Claude 3.5 Sonnet and Haiku models specialized for reasoning and coding.",
        website="https://anthropic.com",
        supported_services=["chat"],
        models=["claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022"],
        streaming_supported=True,
        auth_header_type="x-api-key",
        pricing_estimate={"claude-3-5-haiku": {"input_per_m": 1.00, "output_per_m": 5.00}},
    ),
    "gemini": ProviderMetadata(
        id="gemini",
        display_name="Google Gemini",
        description="High-speed multimodal models (Gemini 1.5 Flash, Gemini 1.5 Pro).",
        website="https://ai.google.dev",
        supported_services=["chat"],
        models=["gemini-1.5-flash", "gemini-1.5-pro"],
        streaming_supported=True,
        auth_header_type="x-goog-api-key",
        pricing_estimate={"gemini-1.5-flash": {"input_per_m": 0.075, "output_per_m": 0.30}},
    ),
    "openrouter": ProviderMetadata(
        id="openrouter",
        display_name="OpenRouter",
        description="Unified gateway across open-source and proprietary models (Llama 3, DeepSeek, Mistral).",
        website="https://openrouter.ai",
        supported_services=["chat"],
        models=["meta-llama/llama-3.1-8b-instruct", "meta-llama/llama-3.1-70b-instruct", "deepseek/deepseek-chat"],
        streaming_supported=True,
        auth_header_type="Bearer",
        pricing_estimate={"llama-3.1-8b": {"input_per_m": 0.06, "output_per_m": 0.06}},
    ),
    "deepgram": ProviderMetadata(
        id="deepgram",
        display_name="Deepgram",
        description="Low-latency voice AI: Aura text-to-speech and Nova speech-to-text.",
        website="https://deepgram.com",
        supported_services=["text-to-speech", "speech-to-text"],
        models=["aura-asteria-en", "aura-luna-en", "nova-2"],
        streaming_supported=True,
        auth_header_type="Token",
        pricing_estimate={"aura": {"per_char": 0.000015}},
    ),
    "jina": ProviderMetadata(
        id="jina",
        display_name="Jina AI",
        description="State-of-the-art multilingual embedding and reranking models.",
        website="https://jina.ai",
        supported_services=["embeddings"],
        models=["jina-embeddings-v3"],
        streaming_supported=False,
        auth_header_type="Bearer",
        pricing_estimate={"jina-embeddings-v3": {"input_per_m": 0.02}},
    ),
    "resend": ProviderMetadata(
        id="resend",
        display_name="Resend",
        description="Developer-first transactional email delivery API with delivery tracking.",
        website="https://resend.com",
        supported_services=["email"],
        models=["transactional-email"],
        streaming_supported=False,
        auth_header_type="Bearer",
        pricing_estimate={"email": {"per_email": 0.001}},
    ),
}

SERVICE_CATALOG: Dict[str, ServiceDefinition] = {
    "chat": ServiceDefinition(
        id="chat",
        name="Chat Completion",
        description="Conversational agent intelligence and question answering.",
        supported_providers=["openai", "anthropic", "gemini", "openrouter"],
        input_format="OpenAI chat messages format [{\"role\": \"user\", \"content\": \"...\"}]",
        supports_streaming=True,
    ),
    "embeddings": ServiceDefinition(
        id="embeddings",
        name="Vector Embeddings",
        description="Semantic dense vector representation for knowledge bases and retrieval.",
        supported_providers=["openai", "jina"],
        input_format="List of texts [\"text1\", \"text2\"] or single string",
        supports_streaming=False,
    ),
    "text-to-speech": ServiceDefinition(
        id="text-to-speech",
        name="Text to Speech (TTS)",
        description="High quality real-time voice synthesis.",
        supported_providers=["deepgram"],
        input_format="JSON object with {\"text\": \"...\"}",
        supports_streaming=True,
    ),
    "speech-to-text": ServiceDefinition(
        id="speech-to-text",
        name="Speech to Text (STT)",
        description="Audio transcription.",
        supported_providers=["deepgram"],
        input_format="Binary audio buffer or audio URL",
        supports_streaming=True,
    ),
    "email": ServiceDefinition(
        id="email",
        name="Transactional Email",
        description="Email OTP, password resets, and customer notification delivery.",
        supported_providers=["resend"],
        input_format="JSON object with to, subject, html, text",
        supports_streaming=False,
    ),
}


class CapabilityRegistry:
    """Introspection and query interface for Agents."""

    @classmethod
    def list_services(cls) -> List[ServiceDefinition]:
        return list(SERVICE_CATALOG.values())

    @classmethod
    def get_service(cls, service_id: str) -> Optional[ServiceDefinition]:
        return SERVICE_CATALOG.get(service_id.lower())

    @classmethod
    def list_providers(cls, service_filter: Optional[str] = None) -> List[ProviderMetadata]:
        if not service_filter:
            return list(PROVIDER_CATALOG.values())
        return [p for p in PROVIDER_CATALOG.values() if service_filter.lower() in p.supported_services]

    @classmethod
    def get_provider(cls, provider_id: str) -> Optional[ProviderMetadata]:
        return PROVIDER_CATALOG.get(provider_id.lower())

    @classmethod
    def discover(cls) -> Dict[str, Any]:
        return {
            "services": [s.model_dump() for s in cls.list_services()],
            "providers": [p.model_dump() for p in cls.list_providers()],
            "system": {
                "version": "1.0.0",
                "capabilities_count": len(SERVICE_CATALOG),
                "providers_count": len(PROVIDER_CATALOG),
            },
        }

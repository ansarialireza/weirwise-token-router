"""Universal Capability and Service Registry for all external service domains.

Supports AI, Communication, Payments, Storage, Data Search, Productivity, and Social.
"""

from typing import Any, Dict, List, Optional
from token_router.core.contracts import (
    OperationSafety,
    ServiceCategory,
    ServiceOperation,
    UniversalServiceDefinition,
)
from pydantic import BaseModel, Field


class ProviderMetadata(BaseModel):
    id: str
    display_name: str
    category: ServiceCategory
    description: str
    website: str
    supported_services: List[str]
    operations_supported: List[str]
    streaming_supported: bool = False
    auth_header_type: str = "Bearer"  # "Bearer", "x-api-key", "x-goog-api-key", "Token", "Basic", "Signed"
    required_credential_fields: List[str] = Field(default_factory=lambda: ["secret"])
    pricing_estimate: Dict[str, Any] = Field(default_factory=dict)


# --- Universal Service Catalog ---
UNIVERSAL_SERVICES: Dict[str, UniversalServiceDefinition] = {
    # 1. AI: LLM & Intelligence
    "chat": UniversalServiceDefinition(
        id="chat",
        category=ServiceCategory.AI,
        name="Conversational Intelligence",
        description="Text generation, reasoning, and conversational agents.",
        supported_providers=["openai", "anthropic", "gemini", "openrouter"],
        operations=[
            ServiceOperation(
                id="chat_completion",
                name="Chat Completion",
                description="Generate completions for conversational prompts.",
                safety=OperationSafety(
                    is_idempotent=True,
                    allows_automatic_failover=True,
                    supports_streaming=True,
                ),
            )
        ],
    ),
    "embeddings": UniversalServiceDefinition(
        id="embeddings",
        category=ServiceCategory.AI,
        name="Vector Embeddings",
        description="Semantic dense vector representation for knowledge bases.",
        supported_providers=["openai", "jina"],
        operations=[
            ServiceOperation(
                id="create_embeddings",
                name="Create Embeddings",
                description="Generate vector embeddings from text.",
                safety=OperationSafety(
                    is_idempotent=True,
                    allows_automatic_failover=True,
                    supports_streaming=False,
                ),
            )
        ],
    ),
    "audio": UniversalServiceDefinition(
        id="audio",
        category=ServiceCategory.AI,
        name="Audio & Voice AI",
        description="Speech synthesis and audio transcription.",
        supported_providers=["deepgram"],
        operations=[
            ServiceOperation(
                id="text_to_speech",
                name="Text to Speech",
                description="Synthesize natural voice audio from text.",
                safety=OperationSafety(
                    is_idempotent=True,
                    allows_automatic_failover=True,
                    supports_streaming=True,
                ),
            ),
            ServiceOperation(
                id="speech_to_text",
                name="Speech to Text",
                description="Transcribe audio streams or recordings into text.",
                safety=OperationSafety(
                    is_idempotent=True,
                    allows_automatic_failover=True,
                    supports_streaming=True,
                ),
            ),
        ],
    ),

    # 2. Communication: Email & SMS
    "email": UniversalServiceDefinition(
        id="email",
        category=ServiceCategory.COMMUNICATION,
        name="Transactional Email",
        description="Reliable transactional email delivery with template and tracking support.",
        supported_providers=["resend"],
        operations=[
            ServiceOperation(
                id="send_email",
                name="Send Email",
                description="Send an email message to one or more recipients.",
                safety=OperationSafety(
                    is_idempotent=False,
                    allows_automatic_failover=False,  # Non-idempotent without deduplication check
                    supports_streaming=False,
                    default_timeout_seconds=15.0,
                ),
            )
        ],
    ),
    "sms": UniversalServiceDefinition(
        id="sms",
        category=ServiceCategory.COMMUNICATION,
        name="SMS & Mobile Messaging",
        description="Outbound SMS and OTP delivery across cellular carriers.",
        supported_providers=["twilio"],
        operations=[
            ServiceOperation(
                id="send_sms",
                name="Send SMS",
                description="Dispatch SMS message to a mobile number.",
                safety=OperationSafety(
                    is_idempotent=False,
                    allows_automatic_failover=False,
                    supports_streaming=False,
                    default_timeout_seconds=10.0,
                ),
            )
        ],
    ),

    # 3. Payments: Payment Gateways
    "payments": UniversalServiceDefinition(
        id="payments",
        category=ServiceCategory.PAYMENTS,
        name="Payment Gateway & Checkout",
        description="Customer billing, card charges, subscriptions, and financial checkout.",
        supported_providers=["stripe"],
        operations=[
            ServiceOperation(
                id="create_payment_intent",
                name="Create Payment Intent",
                description="Initiate and authorize a customer payment session.",
                safety=OperationSafety(
                    is_idempotent=True,  # Idempotent ONLY with Idempotency-Key
                    allows_automatic_failover=False,  # STRICT: Zero automatic failover without transaction status query!
                    supports_streaming=False,
                    requires_approval=False,
                    default_timeout_seconds=30.0,
                ),
            ),
            ServiceOperation(
                id="refund_payment",
                name="Refund Payment",
                description="Refund an existing customer transaction.",
                safety=OperationSafety(
                    is_idempotent=True,
                    allows_automatic_failover=False,
                    supports_streaming=False,
                    requires_approval=True,  # Sensitive operation requiring human/approval guard
                ),
            ),
        ],
    ),

    # 4. Storage: Object Storage
    "storage": UniversalServiceDefinition(
        id="storage",
        category=ServiceCategory.STORAGE,
        name="Cloud Object Storage",
        description="Scalable file uploads, signed download URLs, and asset persistence.",
        supported_providers=["s3_compatible"],
        operations=[
            ServiceOperation(
                id="put_object",
                name="Put Object",
                description="Upload a file or object buffer to an S3 or R2 bucket.",
                safety=OperationSafety(
                    is_idempotent=True,
                    allows_automatic_failover=True,
                    supports_streaming=False,
                ),
            ),
            ServiceOperation(
                id="get_signed_url",
                name="Get Signed URL",
                description="Generate a pre-signed URL for direct download/upload.",
                safety=OperationSafety(
                    is_idempotent=True,
                    allows_automatic_failover=True,
                    supports_streaming=False,
                ),
            ),
        ],
    ),

    # 5. Data: Web Search & Crawling
    "data_search": UniversalServiceDefinition(
        id="data_search",
        category=ServiceCategory.DATA,
        name="Search & Web Extraction",
        description="Search indices, web crawling, and knowledge extraction APIs.",
        supported_providers=["jina_search"],
        operations=[
            ServiceOperation(
                id="search_web",
                name="Search Web",
                description="Search the web and retrieve grounded text snippets.",
                safety=OperationSafety(
                    is_idempotent=True,
                    allows_automatic_failover=True,
                    supports_streaming=False,
                ),
            )
        ],
    ),
}


# --- Universal Provider Catalog ---
UNIVERSAL_PROVIDERS: Dict[str, ProviderMetadata] = {
    # AI Providers
    "openai": ProviderMetadata(
        id="openai",
        display_name="OpenAI",
        category=ServiceCategory.AI,
        description="Industry standard generative models (GPT-4o, text-embedding-3).",
        website="https://openai.com",
        supported_services=["chat", "embeddings"],
        operations_supported=["chat_completion", "create_embeddings"],
        streaming_supported=True,
        auth_header_type="Bearer",
        pricing_estimate={"gpt-4o-mini": {"input_per_m": 0.15, "output_per_m": 0.60}},
    ),
    "anthropic": ProviderMetadata(
        id="anthropic",
        display_name="Anthropic",
        category=ServiceCategory.AI,
        description="Claude 3.5 Sonnet and Haiku models specialized for reasoning.",
        website="https://anthropic.com",
        supported_services=["chat"],
        operations_supported=["chat_completion"],
        streaming_supported=True,
        auth_header_type="x-api-key",
        pricing_estimate={"claude-3-5-haiku": {"input_per_m": 1.00, "output_per_m": 5.00}},
    ),
    "gemini": ProviderMetadata(
        id="gemini",
        display_name="Google Gemini",
        category=ServiceCategory.AI,
        description="High-speed multimodal models (Gemini 1.5 Flash, Pro).",
        website="https://ai.google.dev",
        supported_services=["chat"],
        operations_supported=["chat_completion"],
        streaming_supported=True,
        auth_header_type="x-goog-api-key",
        pricing_estimate={"gemini-1.5-flash": {"input_per_m": 0.075, "output_per_m": 0.30}},
    ),
    "openrouter": ProviderMetadata(
        id="openrouter",
        display_name="OpenRouter",
        category=ServiceCategory.AI,
        description="Unified gateway across open-source and proprietary models.",
        website="https://openrouter.ai",
        supported_services=["chat"],
        operations_supported=["chat_completion"],
        streaming_supported=True,
        auth_header_type="Bearer",
    ),
    "deepgram": ProviderMetadata(
        id="deepgram",
        display_name="Deepgram",
        category=ServiceCategory.AI,
        description="Low-latency voice AI: Aura TTS and Nova STT.",
        website="https://deepgram.com",
        supported_services=["audio"],
        operations_supported=["text_to_speech", "speech_to_text"],
        streaming_supported=True,
        auth_header_type="Token",
    ),
    "jina": ProviderMetadata(
        id="jina",
        display_name="Jina AI",
        category=ServiceCategory.AI,
        description="Multilingual embedding models (jina-embeddings-v3).",
        website="https://jina.ai",
        supported_services=["embeddings"],
        operations_supported=["create_embeddings"],
        streaming_supported=False,
        auth_header_type="Bearer",
    ),

    # Communication Providers
    "resend": ProviderMetadata(
        id="resend",
        display_name="Resend",
        category=ServiceCategory.COMMUNICATION,
        description="Transactional email delivery API with open/click tracking.",
        website="https://resend.com",
        supported_services=["email"],
        operations_supported=["send_email"],
        streaming_supported=False,
        auth_header_type="Bearer",
        pricing_estimate={"email": {"per_email": 0.001}},
    ),
    "twilio": ProviderMetadata(
        id="twilio",
        display_name="Twilio",
        category=ServiceCategory.COMMUNICATION,
        description="Cloud communications platform for programmable SMS and Voice.",
        website="https://twilio.com",
        supported_services=["sms"],
        operations_supported=["send_sms"],
        streaming_supported=False,
        auth_header_type="Basic",
        required_credential_fields=["account_sid", "auth_token"],
        pricing_estimate={"sms": {"per_segment": 0.0079}},
    ),

    # Payments Providers
    "stripe": ProviderMetadata(
        id="stripe",
        display_name="Stripe",
        category=ServiceCategory.PAYMENTS,
        description="Global economic infrastructure for internet businesses.",
        website="https://stripe.com",
        supported_services=["payments"],
        operations_supported=["create_payment_intent", "refund_payment"],
        streaming_supported=False,
        auth_header_type="Bearer",
        required_credential_fields=["secret_key", "webhook_signing_secret"],
    ),

    # Storage Providers
    "s3_compatible": ProviderMetadata(
        id="s3_compatible",
        display_name="S3 Compatible (AWS / Cloudflare R2 / MinIO)",
        category=ServiceCategory.STORAGE,
        description="High-performance object storage with S3 protocol compatibility.",
        website="https://aws.amazon.com/s3",
        supported_services=["storage"],
        operations_supported=["put_object", "get_signed_url"],
        streaming_supported=False,
        auth_header_type="Signed",
        required_credential_fields=["access_key_id", "secret_access_key", "endpoint_url", "bucket"],
    ),

    # Data / Search Providers
    "jina_search": ProviderMetadata(
        id="jina_search",
        display_name="Jina Reader & Web Search",
        category=ServiceCategory.DATA,
        description="Converts URLs to LLM-ready clean markdown and performs web search.",
        website="https://jina.ai/reader",
        supported_services=["data_search"],
        operations_supported=["search_web"],
        streaming_supported=False,
        auth_header_type="Bearer",
    ),
}


class CapabilityRegistry:
    """Introspection and query interface for Agents and consuming applications."""

    @classmethod
    def list_categories(cls) -> List[str]:
        return [c.value for c in ServiceCategory]

    @classmethod
    def list_services(cls, category_filter: Optional[str] = None) -> List[UniversalServiceDefinition]:
        services = list(UNIVERSAL_SERVICES.values())
        if category_filter:
            services = [s for s in services if s.category.value == category_filter.lower()]
        return services

    @classmethod
    def get_service(cls, service_id: str) -> Optional[UniversalServiceDefinition]:
        return UNIVERSAL_SERVICES.get(service_id.lower())

    @classmethod
    def get_operation(cls, service_id: str, operation_id: str) -> Optional[ServiceOperation]:
        svc = cls.get_service(service_id)
        if not svc:
            return None
        for op in svc.operations:
            if op.id == operation_id:
                return op
        return None

    @classmethod
    def list_providers(cls, category_filter: Optional[str] = None, service_filter: Optional[str] = None) -> List[ProviderMetadata]:
        providers = list(UNIVERSAL_PROVIDERS.values())
        if category_filter:
            providers = [p for p in providers if p.category.value == category_filter.lower()]
        if service_filter:
            providers = [p for p in providers if service_filter.lower() in p.supported_services]
        return providers

    @classmethod
    def get_provider(cls, provider_id: str) -> Optional[ProviderMetadata]:
        return UNIVERSAL_PROVIDERS.get(provider_id.lower())

    @classmethod
    def discover(cls, category_filter: Optional[str] = None) -> Dict[str, Any]:
        return {
            "categories": cls.list_categories(),
            "services": [s.model_dump() for s in cls.list_services(category_filter=category_filter)],
            "providers": [p.model_dump() for p in cls.list_providers(category_filter=category_filter)],
            "system": {
                "version": "2.0.0",
                "platform": "Weirwise Universal Service Integration Platform",
                "categories_count": len(ServiceCategory),
                "services_count": len(UNIVERSAL_SERVICES),
                "providers_count": len(UNIVERSAL_PROVIDERS),
            },
        }

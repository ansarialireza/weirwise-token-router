"""Provider Pricing Registry.

Stores, validates, and calculates model-specific input/output token pricing,
request-based pricing, and tracks pricing source and verification timestamps.
Supports free-tier, paid, and local (zero-cost) providers.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class ModelPricing(BaseModel):
    """Granular pricing definition for a specific provider model."""
    provider: str
    model: str
    tier: str = "paid"  # "free", "paid", "local"
    input_cost_per_m_tokens: Decimal = Decimal("0.0")
    output_cost_per_m_tokens: Decimal = Decimal("0.0")
    per_request_cost: Decimal = Decimal("0.0")
    currency: str = "USD"
    context_window: int = 128000
    supports_vision: bool = False
    supports_tools: bool = True
    quality_score: float = 8.0  # 1.0 to 10.0 scale for capability ranking
    source: str = "official_docs"
    last_verified_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def calculate_cost(self, input_tokens: int = 0, output_tokens: int = 0) -> Decimal:
        """Calculate estimated cost in USD (or configured currency) for token usage."""
        if self.tier in ("free", "local"):
            return Decimal("0.0")
        
        in_cost = (Decimal(str(input_tokens)) / Decimal("1000000.0")) * self.input_cost_per_m_tokens
        out_cost = (Decimal(str(output_tokens)) / Decimal("1000000.0")) * self.output_cost_per_m_tokens
        total = in_cost + out_cost + self.per_request_cost
        return total.quantize(Decimal("0.000001"))


class PricingRegistry:
    """In-memory and dynamically updatable provider pricing registry.

    Provides documented pricing across AI LLMs, embeddings, audio, and local endpoints.
    """

    _pricing: Dict[str, Dict[str, ModelPricing]] = {}

    @classmethod
    def _init_defaults(cls):
        """Initializes canonical, documented baseline pricing for supported providers."""
        if cls._pricing:
            return

        defaults = [
            # 1. Google Gemini (Documented Free-Tier & Paid Limits)
            # Free tier: Gemini 1.5 Flash is 0 cost up to 15 RPM, 1M TPM, 1500 RPD
            ModelPricing(
                provider="gemini",
                model="gemini-1.5-flash",
                tier="free",
                input_cost_per_m_tokens=Decimal("0.075"),
                output_cost_per_m_tokens=Decimal("0.30"),
                per_request_cost=Decimal("0.0"),
                context_window=1000000,
                supports_vision=True,
                quality_score=8.5,
                source="Google AI Studio Official Docs",
            ),
            ModelPricing(
                provider="gemini",
                model="gemini-1.5-pro",
                tier="paid",
                input_cost_per_m_tokens=Decimal("1.25"),
                output_cost_per_m_tokens=Decimal("5.00"),
                per_request_cost=Decimal("0.0"),
                context_window=2000000,
                supports_vision=True,
                quality_score=9.4,
                source="Google AI Studio Official Docs",
            ),
            ModelPricing(
                provider="gemini",
                model="gemini-2.0-flash",
                tier="free",
                input_cost_per_m_tokens=Decimal("0.10"),
                output_cost_per_m_tokens=Decimal("0.40"),
                per_request_cost=Decimal("0.0"),
                context_window=1000000,
                supports_vision=True,
                quality_score=9.0,
                source="Google AI Studio Official Docs",
            ),
            ModelPricing(
                provider="gemini",
                model="gemini-3.5-flash-lite",
                tier="free",
                input_cost_per_m_tokens=Decimal("0.075"),
                output_cost_per_m_tokens=Decimal("0.30"),
                per_request_cost=Decimal("0.0"),
                context_window=1000000,
                supports_vision=True,
                quality_score=8.8,
                source="Google AI Studio Official Docs (Verified 2026)",
            ),

            # 2. OpenRouter Free & Paid models
            ModelPricing(
                provider="openrouter",
                model="meta-llama/llama-3.1-8b-instruct:free",
                tier="free",
                input_cost_per_m_tokens=Decimal("0.0"),
                output_cost_per_m_tokens=Decimal("0.0"),
                per_request_cost=Decimal("0.0"),
                context_window=131072,
                quality_score=7.8,
                source="OpenRouter Free Tier Catalog",
            ),
            ModelPricing(
                provider="openrouter",
                model="google/gemini-2.0-flash-exp:free",
                tier="free",
                input_cost_per_m_tokens=Decimal("0.0"),
                output_cost_per_m_tokens=Decimal("0.0"),
                per_request_cost=Decimal("0.0"),
                context_window=1000000,
                quality_score=8.9,
                source="OpenRouter Free Tier Catalog",
            ),
            ModelPricing(
                provider="openrouter",
                model="meta-llama/llama-3.1-8b-instruct",
                tier="paid",
                input_cost_per_m_tokens=Decimal("0.06"),
                output_cost_per_m_tokens=Decimal("0.06"),
                per_request_cost=Decimal("0.0"),
                context_window=131072,
                quality_score=7.8,
                source="OpenRouter Docs",
            ),

            # 3. OpenAI Models
            ModelPricing(
                provider="openai",
                model="gpt-4o-mini",
                tier="paid",
                input_cost_per_m_tokens=Decimal("0.15"),
                output_cost_per_m_tokens=Decimal("0.60"),
                per_request_cost=Decimal("0.0"),
                context_window=128000,
                supports_vision=True,
                quality_score=8.7,
                source="OpenAI Official Pricing",
            ),
            ModelPricing(
                provider="openai",
                model="gpt-4o",
                tier="paid",
                input_cost_per_m_tokens=Decimal("2.50"),
                output_cost_per_m_tokens=Decimal("10.00"),
                per_request_cost=Decimal("0.0"),
                context_window=128000,
                supports_vision=True,
                quality_score=9.6,
                source="OpenAI Official Pricing",
            ),
            ModelPricing(
                provider="openai",
                model="text-embedding-3-small",
                tier="paid",
                input_cost_per_m_tokens=Decimal("0.02"),
                output_cost_per_m_tokens=Decimal("0.0"),
                per_request_cost=Decimal("0.0"),
                context_window=8191,
                quality_score=8.5,
                source="OpenAI Official Pricing",
            ),

            # 4. Anthropic Models
            ModelPricing(
                provider="anthropic",
                model="claude-3-5-haiku-latest",
                tier="paid",
                input_cost_per_m_tokens=Decimal("0.80"),
                output_cost_per_m_tokens=Decimal("4.00"),
                per_request_cost=Decimal("0.0"),
                context_window=200000,
                supports_vision=True,
                quality_score=8.8,
                source="Anthropic Official Pricing",
            ),
            ModelPricing(
                provider="anthropic",
                model="claude-3-5-sonnet-latest",
                tier="paid",
                input_cost_per_m_tokens=Decimal("3.00"),
                output_cost_per_m_tokens=Decimal("15.00"),
                per_request_cost=Decimal("0.0"),
                context_window=200000,
                supports_vision=True,
                quality_score=9.8,
                source="Anthropic Official Pricing",
            ),

            # 5. Local Provider (Ollama / Local vLLM)
            # Local providers run on owned/local hardware -> 0 token/request cost
            ModelPricing(
                provider="local",
                model="llama3.2:latest",
                tier="local",
                input_cost_per_m_tokens=Decimal("0.0"),
                output_cost_per_m_tokens=Decimal("0.0"),
                per_request_cost=Decimal("0.0"),
                context_window=131072,
                supports_vision=False,
                quality_score=8.0,
                source="Local Inference Engine",
            ),
            ModelPricing(
                provider="local",
                model="qwen2.5-coder:latest",
                tier="local",
                input_cost_per_m_tokens=Decimal("0.0"),
                output_cost_per_m_tokens=Decimal("0.0"),
                per_request_cost=Decimal("0.0"),
                context_window=32768,
                supports_vision=False,
                quality_score=8.3,
                source="Local Inference Engine",
            ),

            # 6. Jina AI
            ModelPricing(
                provider="jina",
                model="jina-embeddings-v3",
                tier="paid",
                input_cost_per_m_tokens=Decimal("0.02"),
                output_cost_per_m_tokens=Decimal("0.0"),
                per_request_cost=Decimal("0.0"),
                context_window=8192,
                quality_score=8.6,
                source="Jina AI Pricing",
            ),
            ModelPricing(
                provider="jina_search",
                model="search",
                tier="paid",
                input_cost_per_m_tokens=Decimal("0.0"),
                output_cost_per_m_tokens=Decimal("0.0"),
                per_request_cost=Decimal("0.002"),
                quality_score=8.5,
                source="Jina AI Reader Docs",
            ),
        ]

        for p in defaults:
            cls.register_model_pricing(p)

    @classmethod
    def register_model_pricing(cls, pricing: ModelPricing) -> None:
        p_name = pricing.provider.lower()
        if p_name not in cls._pricing:
            cls._pricing[p_name] = {}
        cls._pricing[p_name][pricing.model.lower()] = pricing

    @classmethod
    def get_model_pricing(cls, provider: str, model: str) -> Optional[ModelPricing]:
        cls._init_defaults()
        p_name = provider.lower()
        m_name = model.lower()
        return cls._pricing.get(p_name, {}).get(m_name)

    @classmethod
    def list_pricing(cls, provider: Optional[str] = None) -> List[ModelPricing]:
        cls._init_defaults()
        results = []
        if provider:
            p_dict = cls._pricing.get(provider.lower(), {})
            results.extend(p_dict.values())
        else:
            for p_dict in cls._pricing.values():
                results.extend(p_dict.values())
        return results

    @classmethod
    def estimate_cost(
        cls,
        provider: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> Decimal:
        cls._init_defaults()
        pricing = cls.get_model_pricing(provider, model)
        if not pricing:
            # Fallback heuristic if unknown: assume small paid model
            return Decimal("0.0001")
        return pricing.calculate_cost(input_tokens, output_tokens)

import uuid
from datetime import UTC, datetime
from typing import Any
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.types import JSON
from token_router.db.session import Base

# Universal JSON type compatible with Postgres JSONB or SQLite JSON
UniversalJSON = JSONB().with_variant(JSON(), "sqlite")


def utc_now() -> datetime:
    return datetime.now(UTC)


class Consumer(Base):
    """Consumer application authorized to request token routing."""

    __tablename__ = "consumers"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(100), unique=True, nullable=False, index=True)
    description = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False, index=True)

    # Scopes / Allowed capabilities e.g. ["chat", "embeddings", "speech-to-text", "email"]
    allowed_capabilities = Column(UniversalJSON, default=list, nullable=False)
    # Allowed providers e.g. ["*"] or ["openai", "anthropic"]
    allowed_providers = Column(UniversalJSON, default=lambda: ["*"], nullable=False)

    monthly_budget_usd = Column(Numeric(12, 6), default=1000.0, nullable=False)
    current_month_spend_usd = Column(Numeric(12, 6), default=0.0, nullable=False)

    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    api_keys = relationship("ConsumerApiKey", back_populates="consumer", cascade="all, delete-orphan")
    usage_records = relationship("UsageRecord", back_populates="consumer")


class ConsumerApiKey(Base):
    """Hashed API keys for authenticating consumers."""

    __tablename__ = "consumer_api_keys"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    consumer_id = Column(String(36), ForeignKey("consumers.id", ondelete="CASCADE"), nullable=False, index=True)
    key_hash = Column(String(64), unique=True, nullable=False, index=True)
    key_prefix = Column(String(16), nullable=False)
    label = Column(String(100), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)

    consumer = relationship("Consumer", back_populates="api_keys")


class Provider(Base):
    """AI or infrastructure provider definition (e.g. OpenAI, Anthropic, Gemini, Deepgram, Resend)."""

    __tablename__ = "providers"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(50), unique=True, nullable=False, index=True)  # openai, anthropic, etc.
    display_name = Column(String(100), nullable=False)
    description = Column(String(255), nullable=True)
    is_enabled = Column(Boolean, default=True, nullable=False, index=True)

    # Capabilities supported: ["chat", "embeddings", "speech-to-text", "email"]
    capabilities = Column(UniversalJSON, default=list, nullable=False)
    # Models supported & metadata: {"gpt-4o": {"input_cost_per_m": 2.5, "output_cost_per_m": 10.0}}
    supported_models = Column(UniversalJSON, default=dict, nullable=False)
    # Default routing strategy: priority | round_robin | weighted | least_recently_used | lowest_cost
    default_routing_strategy = Column(String(50), default="round_robin", nullable=False)

    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    credentials = relationship("ProviderCredential", back_populates="provider", cascade="all, delete-orphan")


class ProviderCredential(Base):
    """Encrypted credential assigned to a provider."""

    __tablename__ = "provider_credentials"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    provider_id = Column(String(36), ForeignKey("providers.id", ondelete="CASCADE"), nullable=False, index=True)
    label = Column(String(100), nullable=False)

    key_prefix = Column(String(16), nullable=False)
    encrypted_secret = Column(Text, nullable=False)
    secret_hash = Column(String(64), unique=True, nullable=False, index=True)

    is_active = Column(Boolean, default=True, nullable=False, index=True)
    # Health status: unknown | validating | healthy | degraded | rate_limited | quota_exhausted | failed | disabled
    health_status = Column(String(50), default="unknown", nullable=False, index=True)

    priority = Column(Integer, default=10, nullable=False)
    routing_weight = Column(Integer, default=1, nullable=False)
    models_allowed = Column(UniversalJSON, default=lambda: ["*"], nullable=False)

    monthly_budget_usd = Column(Numeric(12, 6), default=100.0, nullable=False)
    current_month_spend_usd = Column(Numeric(12, 6), default=0.0, nullable=False)
    rate_limit_rpm = Column(Integer, default=60, nullable=False)
    rate_limit_tpm = Column(Integer, default=100000, nullable=False)
    max_concurrency = Column(Integer, default=10, nullable=False)

    consecutive_failures = Column(Integer, default=0, nullable=False)
    last_error_reason = Column(String(255), nullable=True)
    last_error_code = Column(Integer, nullable=True)

    last_used_at = Column(DateTime(timezone=True), nullable=True)
    last_health_check_at = Column(DateTime(timezone=True), nullable=True)
    cooling_down_until = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    provider = relationship("Provider", back_populates="credentials")
    usage_records = relationship("UsageRecord", back_populates="credential")


class UsageRecord(Base):
    """Append-only audit ledger of every routed request, accounting for token spend and latency."""

    __tablename__ = "usage_records"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    consumer_id = Column(String(36), ForeignKey("consumers.id", ondelete="SET NULL"), nullable=True, index=True)
    credential_id = Column(String(36), ForeignKey("provider_credentials.id", ondelete="SET NULL"), nullable=True, index=True)

    provider_name = Column(String(50), nullable=False, index=True)
    model_name = Column(String(100), nullable=False, index=True)
    capability = Column(String(50), nullable=False)

    status = Column(String(20), nullable=False, index=True)  # success | failed | rate_limited | timeout
    status_code = Column(Integer, nullable=True)
    error_category = Column(String(50), nullable=True)

    input_tokens = Column(Integer, default=0, nullable=False)
    output_tokens = Column(Integer, default=0, nullable=False)
    total_tokens = Column(Integer, default=0, nullable=False)

    latency_ms = Column(Float, default=0.0, nullable=False)
    estimated_cost_usd = Column(Numeric(12, 6), default=0.0, nullable=False)
    provider_cost_usd = Column(Numeric(12, 6), nullable=True)

    failover_attempts = Column(Integer, default=0, nullable=False)
    correlation_id = Column(String(128), nullable=True, index=True)

    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)

    consumer = relationship("Consumer", back_populates="usage_records")
    credential = relationship("ProviderCredential", back_populates="usage_records")

from pydantic import BaseModel, ConfigDict, Field
from typing import Any, Dict, List, Optional
from datetime import datetime
from decimal import Decimal


# --- Consumer Schemas ---
class ConsumerCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    description: Optional[str] = None
    allowed_capabilities: List[str] = Field(default_factory=lambda: ["*"])
    allowed_providers: List[str] = Field(default_factory=lambda: ["*"])
    monthly_budget_usd: Decimal = Field(default=Decimal("1000.0"))


class ConsumerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    description: Optional[str]
    is_active: bool
    allowed_capabilities: List[str]
    allowed_providers: List[str]
    monthly_budget_usd: Decimal
    current_month_spend_usd: Decimal
    created_at: datetime


class ConsumerKeyCreate(BaseModel):
    label: str = Field(..., min_length=2, max_length=100)
    expires_in_days: Optional[int] = None


class ConsumerKeyResponse(BaseModel):
    id: str
    consumer_id: str
    label: str
    key_prefix: str
    api_key: Optional[str] = None  # Returned ONLY on creation
    created_at: datetime


# --- Provider Schemas ---
class ProviderCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=50)  # openai, stripe, twilio, s3_compatible
    display_name: str = Field(..., min_length=2, max_length=100)
    description: Optional[str] = None
    capabilities: List[str] = Field(default_factory=lambda: ["chat"])
    supported_models: Dict[str, Any] = Field(default_factory=dict)
    default_routing_strategy: str = "round_robin"


class ProviderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    display_name: str
    description: Optional[str]
    is_enabled: bool
    capabilities: List[str]
    supported_models: Dict[str, Any]
    default_routing_strategy: str
    created_at: datetime


# --- Credential Schemas ---
class CredentialCreate(BaseModel):
    label: str = Field(..., min_length=2, max_length=100)
    secret: str = Field(..., min_length=4)  # Raw secret, will be encrypted in vault immediately
    priority: int = 10
    routing_weight: int = 1
    models_allowed: List[str] = Field(default_factory=lambda: ["*"])
    monthly_budget_usd: Decimal = Field(default=Decimal("100.0"))
    rate_limit_rpm: int = 60
    rate_limit_tpm: int = 100000
    max_concurrency: int = 10


class CredentialResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    provider_id: str
    label: str
    key_prefix: str
    is_active: bool
    health_status: str
    priority: int
    routing_weight: int
    models_allowed: List[str]
    monthly_budget_usd: Decimal
    current_month_spend_usd: Decimal
    rate_limit_rpm: int
    rate_limit_tpm: int
    max_concurrency: int
    consecutive_failures: int
    last_error_reason: Optional[str]
    last_used_at: Optional[datetime]
    last_health_check_at: Optional[datetime]
    cooling_down_until: Optional[datetime]
    created_at: datetime


# --- Route Schemas ---
class RouteRequest(BaseModel):
    provider: Optional[str] = None  # Specific provider e.g. "openai", "stripe" or None for auto
    capability: str = "chat"  # "chat", "create_payment_intent", "send_email", "send_sms", "put_object"
    model: str = "auto"
    request: Dict[str, Any]  # Provider-specific payload (messages, amount, to, key, etc.)
    timeout_seconds: Optional[float] = None
    max_failover_attempts: int = 3
    strategy: Optional[str] = None  # Override strategy (priority, round_robin, weighted, etc.)
    idempotency_key: Optional[str] = None
    approval_id: Optional[str] = None  # Required if operation requires_approval=True


class RouteResponse(BaseModel):
    request_id: Optional[str] = None
    consumer_name: Optional[str] = None
    category: Optional[str] = None
    service: Optional[str] = None
    operation: Optional[str] = None
    provider: str
    model: str
    credential_id: str
    status: str
    status_code: int
    latency_ms: float
    usage: Dict[str, Any] = Field(default_factory=dict)
    estimated_cost_usd: Decimal = Decimal("0.0")
    failover_attempts: int = 0
    idempotency_key: Optional[str] = None
    data: Any  # Decoded response body from provider

"""Universal Service Contracts and Category Definitions for Token Router.

Integrate Once. Manage Centrally. Use Everywhere.
"""

from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class ServiceCategory(str, Enum):
    AI = "ai"
    COMMUNICATION = "communication"
    PAYMENTS = "payments"
    STORAGE = "storage"
    DATA = "data"
    PRODUCTIVITY = "productivity"
    SOCIAL = "social"


class OperationSafety(BaseModel):
    """Operation safety semantics."""
    is_idempotent: bool = False
    allows_automatic_failover: bool = False
    supports_streaming: bool = False
    requires_approval: bool = False
    default_timeout_seconds: float = 30.0


class ServiceOperation(BaseModel):
    """Definition of a concrete operation within a service."""
    id: str  # e.g. "chat_completion", "send_email", "create_payment_intent", "upload_file"
    name: str
    description: str
    safety: OperationSafety
    input_schema: Dict[str, Any] = Field(default_factory=dict)
    output_schema: Dict[str, Any] = Field(default_factory=dict)


class UniversalServiceDefinition(BaseModel):
    """Definition of a service category and its capabilities."""
    id: str  # e.g. "llm", "email", "sms", "payments", "object_storage", "web_search"
    category: ServiceCategory
    name: str
    description: str
    supported_providers: List[str]
    operations: List[ServiceOperation]


class ExecutionEnvelope(BaseModel):
    """Standardized metadata envelope for all executed operations across all categories."""
    request_id: str
    consumer_name: str
    category: ServiceCategory
    service: str
    operation: str
    provider: str
    status: str  # success | error | rate_limited | pending_approval
    status_code: int
    duration_ms: float
    failover_attempts: int = 0
    idempotency_key: Optional[str] = None
    usage: Dict[str, Any] = Field(default_factory=dict)
    estimated_cost_usd: float = 0.0
    data: Optional[Any] = None
    error: Optional[Dict[str, Any]] = None

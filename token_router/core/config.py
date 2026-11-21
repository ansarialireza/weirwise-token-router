import os
import secrets
from typing import List, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "Weirwise Token Router"
    VERSION: str = "1.0.0"
    ENVIRONMENT: str = "development"
    DEBUG: bool = False

    HOST: str = "0.0.0.0"
    PORT: int = 8080

    # PostgreSQL Database URL or fallback to SQLite for lightweight/test setups
    DATABASE_URL: str = "sqlite+aiosqlite:///./token_router.db"
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 10
    DB_TIMEOUT_SECONDS: int = 30

    # AES-256 GCM Master Vault Encryption Key
    # In production, this MUST be explicitly configured.
    VAULT_MASTER_KEY: Optional[str] = None

    # Master Admin API Key for administrative endpoints
    # In production, this MUST be explicitly configured.
    ADMIN_API_KEY: Optional[str] = None

    # Optional Redis for distributed state / rate limiting
    REDIS_URL: Optional[str] = None

    # Distributed Quota Failure Policy: "fail_closed" (default for distributed production)
    # or "local_fallback" (only for single-instance setups where independent local state is acceptable)
    DISTRIBUTED_MODE: bool = False  # Set to True when multiple router instances are deployed
    QUOTA_FAILURE_POLICY: str = "local_fallback"  # "fail_closed" or "local_fallback"
    REQUIRE_REDIS_PERSISTENCE: bool = False  # When True, startup checks verify Redis AOF is active

    # Allowed CORS Origins
    ALLOWED_ORIGINS: List[str] = ["*"]

    # Global Timeouts and Circuit Breaker Settings
    DEFAULT_TIMEOUT_SECONDS: float = 30.0
    CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = 5
    CIRCUIT_BREAKER_COOLDOWN_SECONDS: int = 60

    # Trusted Reverse Proxy configuration for public webhook URL determination
    # Only if client IP is in TRUSTED_PROXIES will X-Forwarded-Proto / X-Forwarded-Host be honored.
    TRUSTED_PROXIES: List[str] = ["127.0.0.1", "::1"]
    PUBLIC_WEBHOOK_URL_BASE: Optional[str] = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    def validate_production_secrets(self) -> None:
        """Enforces that production startup immediately fails if critical secrets are missing or default."""
        if self.ENVIRONMENT.lower() in ("production", "prod"):
            errors = []
            if not self.VAULT_MASTER_KEY or len(self.VAULT_MASTER_KEY.strip()) < 32:
                errors.append("VAULT_MASTER_KEY must be configured with at least 32 characters in production.")
            if not self.ADMIN_API_KEY or len(self.ADMIN_API_KEY.strip()) < 24:
                errors.append("ADMIN_API_KEY must be configured with a secure high-entropy string in production.")
            if "sqlite" in self.DATABASE_URL.lower():
                errors.append("DATABASE_URL must be a production PostgreSQL database, not SQLite.")
            if self.DISTRIBUTED_MODE and not self.REDIS_URL:
                errors.append("REDIS_URL must be configured when DISTRIBUTED_MODE=true.")
            if errors:
                raise RuntimeError(f"FATAL: Insecure production configuration:\n" + "\n".join(f"- {e}" for e in errors))


settings = Settings()

from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List, Optional


class Settings(BaseSettings):
    PROJECT_NAME: str = "Weirwise Token Router"
    VERSION: str = "1.0.0"
    ENVIRONMENT: str = "development"
    DEBUG: bool = False

    HOST: str = "0.0.0.0"
    PORT: int = 8080

    # PostgreSQL Database URL
    DATABASE_URL: str = "postgresql+asyncpg://token_router:token_router_pass@127.0.0.1:5432/token_router_db"
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 10
    DB_TIMEOUT_SECONDS: int = 30

    # AES-256 GCM Master Vault Encryption Key (Must be 32 URL-safe base64-encoded bytes or hex)
    VAULT_MASTER_KEY: str = "dGhpc2lzYXRlc3RtYXN0ZXJrZXkxMjM0NTY3ODkwMTI="

    # Master Admin API Key for administrative endpoints
    ADMIN_API_KEY: str = "wtr_admin_secret_key_change_in_production"

    # Optional Redis for distributed state / rate limiting
    REDIS_URL: Optional[str] = None

    # Allowed CORS Origins
    ALLOWED_ORIGINS: List[str] = ["*"]

    # Global Timeouts and Circuit Breaker Settings
    DEFAULT_TIMEOUT_SECONDS: float = 30.0
    CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = 5
    CIRCUIT_BREAKER_COOLDOWN_SECONDS: int = 60

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


settings = Settings()

# Weirwise Token Router

**Universal, Agent-Native Service Integration & Execution Platform.**

The Weirwise Token Router is an independent, production-grade routing and orchestration service for managing AI providers, communications, payments, storage, and local inference engines. It provides zero-coupling integration, cryptographic credential vaulting, distributed rate-limit intelligence, atomic consumer budget enforcement, and strict execution boundary safety.

---

## Table of Contents

- [Core Architecture](#core-architecture)
- [Supported Providers & Capabilities](#supported-providers--capabilities)
- [Prerequisites](#prerequisites)
- [Local Installation](#local-installation)
- [Environment Configuration](#environment-configuration)
- [Database Setup & Migrations](#database-setup--migrations)
- [Redis Setup & Persistence](#redis-setup--persistence)
- [Running the Application](#running-the-application)
- [Running Tests](#running-tests)
- [Docker Deployment](#docker-deployment)
- [Security Considerations](#security-considerations)
- [Contributing](#contributing)
- [License](#license)

---

## Core Architecture

```
Client / Agent Request
        │
        ▼
[ API Authentication & Consumer Scoping ]  (X-Consumer-Key, allowed_providers, allowed_capabilities)
        │
        ▼
[ Concurrency-Safe Budget Reservation ]    (PostgreSQL row-level atomic lock: current_month_spend_usd)
        │
        ▼
[ Persistent Idempotency Check ]           (Durable idempotency state machine: started, completed, uncertain)
        │
        ▼
[ Execution Boundary Approval Guard ]      (Persistent database approvals for financial/irreversible ops)
        │
        ▼
[ Intelligent Routing Engine ]             (Strategies: free_tier_first, lowest_cost, quality_first, priority)
        │
        ▼
[ Distributed Quota Tracker (Redis) ]      (Atomic Lua sliding-window reservation: RPM, TPM, RPD)
        │
        ▼
[ Credential Vault Decryption ]            (AES-256-GCM zero-plaintext decryption strictly at dispatch)
        │
        ▼
[ Provider Execution & Header Extraction ] (Upstream API call + vendor rate-limit header parsing)
        │
        ▼
[ Dynamic Reconciliation & Budget Final ]  (Adjust estimated token cost to exact actual usage)
```

### Architectural Guarantees
1. **Zero Downstream Financial Bleed**: Financial operations (payments, SMS) enforce `allows_automatic_failover = False`. Pre-dispatch failures safely roll back reservations; ambiguous outcomes (e.g. 504 timeouts) preserve reservations without double-refunding.
2. **Fail-Closed Distributed Mode**: When `DISTRIBUTED_MODE=true` or `QUOTA_FAILURE_POLICY="fail_closed"`, if Redis is unreachable, requests fail closed (`RedisUnavailableError`) rather than silently degrading into local oversubscription.
3. **Sliding-Window Reservation Cleanup**: Expired reservations older than 60 seconds are automatically purged by Redis Lua scripts on subsequent calls, preventing permanent quota starvation from crashed workers.

---

## Supported Providers & Capabilities

| Provider | Category | Supported Operations | Routing Strategies | Free-Tier Support |
| :--- | :--- | :--- | :--- | :--- |
| **OpenAI** | AI | `chat:completion`, `embeddings:generate` | `lowest_cost`, `priority`, `round_robin` | Paid tier |
| **Anthropic** | AI | `chat:completion` | `quality_first`, `priority` | Paid tier |
| **Google Gemini** | AI | `chat:completion`, `multimodal` | `free_tier_first`, `lowest_cost` | `gemini-3.5-flash-lite`, `gemini-2.0-flash` |
| **OpenRouter** | AI Aggregator | `chat:completion` | `free_tier_first`, `lowest_cost` | Filtered `:free` models (e.g. `nemotron`, `ernie`) |
| **Local Daemons** | AI / Local | `chat:completion`, `embeddings:generate` | `lowest_cost` (0.0 cost) | Ollama, vLLM, LM Studio with health checks |
| **Jina AI** | AI Search | `embeddings:generate` | `round_robin`, `priority` | Multi-key load balancing |
| **Deepgram** | Audio | `stt:transcribe`, `tts:speak` | `priority` | Pay-as-you-go |
| **Resend** | Communications | `email:send` | `priority` (Non-failover) | Free & paid tiers |
| **Twilio** | Communications | `sms:send`, `voice:call` | `priority` (Non-failover) | Strict RFC signature validation |
| **Stripe** | Payments | `payment:intent`, `refund:create` | Approval-bound (Non-failover) | Webhook signing verification |
| **Amazon S3** | Storage | `storage:presign`, `storage:put` | `priority` | Standard S3 API |

---

## Prerequisites

- **Python**: 3.12 or newer
- **PostgreSQL**: 16 (for production and concurrency tests)
- **Redis**: 7.x with AOF persistence enabled
- **Docker & Docker Compose**: (Optional, for containerized deployment)

---

## Local Installation

```bash
# Clone the repository
git clone https://github.com/ansarialireza/weirwise-token-router.git
cd weirwise-token-router

# Create virtual environment
python3.12 -m venv .venv
source .venv/bin/activate

# Install dependencies in editable mode
pip install --upgrade pip
pip install -e ".[dev]"
```

---

## Environment Configuration

Copy the example template:
```bash
cp .env.example .env
```

Key environment variables:
```ini
ENVIRONMENT=development
HOST=0.0.0.0
PORT=8080

# Database Configuration (PostgreSQL recommended; SQLite fallback for local unit tests)
DATABASE_URL=sqlite+aiosqlite:///./token_router.db

# Cryptographic Keys (Required in production, min 32 characters entropy)
VAULT_MASTER_KEY=generate_with_openssl_rand_base64_32_characters_here
ADMIN_API_KEY=generate_with_openssl_rand_hex_24_characters_here

# Distributed State & Redis Persistence
REDIS_URL=redis://127.0.0.1:6379/0
DISTRIBUTED_MODE=false
QUOTA_FAILURE_POLICY=local_fallback
REQUIRE_REDIS_PERSISTENCE=false
```

---

## Database Setup & Migrations

Weirwise Token Router uses **Alembic** for schema migrations.

```bash
# Apply migrations to latest revision
alembic upgrade head

# Downgrade by one revision (if needed)
alembic downgrade -1
```

---

## Redis Setup & Persistence

For production multi-instance deployments, Redis must be configured with **AOF persistence**:
```bash
redis-server \
  --appendonly yes \
  --appendfsync everysec \
  --save "900 1 300 10 60 10000" \
  --maxmemory 100mb \
  --maxmemory-policy noeviction
```

When `REQUIRE_REDIS_PERSISTENCE=true` is set, Token Router checks `CONFIG GET appendonly` on startup and raises a fatal error if AOF is not enabled.

---

## Running the Application

```bash
# Start the Uvicorn ASGI server
uvicorn token_router.main:app --host 0.0.0.0 --port 8080 --reload
```

Interactive API documentation will be available at:
- **Swagger UI**: `http://localhost:8080/docs`
- **ReDoc**: `http://localhost:8080/redoc`
- **Health Check**: `http://localhost:8080/health`

---

## Running Tests

Tests are categorized into **Unit**, **Integration**, and **Live Upstream** suites.

```bash
# Run all unit and integration tests (isolated, no real credentials required)
pytest tests/ -v

# Run with local PostgreSQL & Redis integration
PG_TEST_URL="postgresql+asyncpg://test_user:test_pass@127.0.0.1:5433/test_token_router" \
REDIS_URL="redis://127.0.0.1:6379/0" \
pytest tests/ -v

# Run live upstream provider network tests (requires explicit opt-in)
RUN_LIVE_UPSTREAM_TESTS=true pytest tests/integration/test_live_upstream_e2e.py -v
```

---

## Docker Deployment

Launch Token Router, PostgreSQL, and Redis in an isolated internal network:
```bash
docker compose up -d
```

Check service status and logs:
```bash
docker compose ps
docker compose logs -f token-router
```

---

## Security Considerations

1. **Zero Hardcoded Secrets**: Production container startup immediately halts via `validate_production_secrets()` if default keys or weak passwords are detected.
2. **Vault Encryption**: Credentials stored in `provider_credentials.encrypted_secret` are encrypted with AES-256-GCM. Secret values are never returned via the Admin API; keys are displayed only as masked previews (`sk-abc...****`).
3. **Approval Binding**: Sensitive financial operations (payment refunds, credit adjustments) strictly enforce persistent database approvals bound by cryptographic payload hash (`payload_hash`). Replay attacks or payload tampering are blocked automatically.
4. **Twilio Webhook Verification**: Strict RFC 2822 Twilio HMAC-SHA1 signature verification prevents spoofed inbound communication events.

---

## Contributing

1. Fork the repository and create a feature branch (`git checkout -b feat/my-feature`).
2. Write unit and integration tests for new adapters or capabilities.
3. Ensure all tests pass (`pytest tests/`).
4. Follow conventional commit format: `feat: ...`, `fix: ...`, `docs: ...`, `test: ...`.
5. Open a Pull Request against `develop`.

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

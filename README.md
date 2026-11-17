# Weirwise Token Router

Production-grade, independent Provider and Credential Routing Service for the Weirwise ecosystem and external SaaS applications.

## Key Capabilities
- **Decoupled Architecture**: Zero database coupling with consumers. Operates entirely via versioned REST APIs.
- **Provider Adapters**: Out-of-the-box adapters for OpenAI, Anthropic, Google Gemini, OpenRouter, Deepgram, Jina, and Resend.
- **Credential Vault**: AES-256 GCM authenticated encryption. Zero plaintext secrets returned after creation.
- **Intelligent Routing**: Priority, Round-Robin, Weighted, Least Recently Used, and Lowest Cost strategies.
- **Bounded Failover**: Automatic rate-limit (429) backoff, circuit breaking, and failover across backup credentials.
- **Multi-Consumer Isolation**: Consumer API key authentication, per-consumer quotas, and append-only token accounting.

## Quickstart

### 1. Run via Docker Compose
```bash
docker compose up -d
```

### 2. Register Consumer & Retrieve Key
```bash
# Register Consumer
curl -X POST http://localhost:8080/api/v1/admin/consumers \
  -H "X-Admin-Key: wtr_admin_secret_key_change_in_production" \
  -H "Content-Type: application/json" \
  -d '{"name": "my_app", "allowed_capabilities": ["chat", "embeddings"]}'

# Generate Consumer API Key
curl -X POST http://localhost:8080/api/v1/admin/consumers/<CONSUMER_ID>/keys \
  -H "X-Admin-Key: wtr_admin_secret_key_change_in_production" \
  -H "Content-Type: application/json" \
  -d '{"label": "Production Primary Key"}'
```

### 3. Route Request
```bash
curl -X POST http://localhost:8080/api/v1/route \
  -H "X-Consumer-Key: wtr_live_xxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "openai",
    "capability": "chat",
    "model": "gpt-4o-mini",
    "request": {
      "messages": [{"role": "user", "content": "Hello world!"}]
    }
  }'
```

## Testing
```bash
pytest tests/unit tests/integration -v
```

## License
MIT

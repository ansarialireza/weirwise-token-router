# Weirwise Token Router Architecture

## Architectural Principles
1. **One Core, Multiple Interfaces**: REST API, MCP Server, and CLI all delegate to `token_router.services.router_service.TokenRouterService`.
2. **Independence**: Zero database or ORM coupling with Weirwise Platform.
3. **Security by Default**: AES-256 GCM envelope encryption with key versioning, zero plaintext credential leakage, strict consumer authorization, and approval workflows for high-risk actions.
4. **Resilience & Bounded Failover**: Automatic rate-limit cooldowns, exponential backoff with jitter, circuit breaking, and multi-credential fallback.

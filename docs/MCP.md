# Model Context Protocol (MCP) Server Guide

Weirwise Token Router exposes a native Model Context Protocol server enabling AI Agents to independently discover capabilities, inspect health, execute requests, and diagnose infrastructure.

## Tool Manifest
- `discover_capabilities`: Introspect supported AI services, models, and registered providers.
- `execute_request`: Route requests with bounded failover.
- `run_diagnostics`: Automated self-check of database, vaults, and routing health.
- `list_credentials_safe`: Masked inspection of credential status (zero secret leakage).
- `request_sensitive_operation`: Submit high-risk actions (e.g. deletion, budget expansion) to human approval queue.

## Launching via stdio
```bash
python -m token_router.mcp.server
```

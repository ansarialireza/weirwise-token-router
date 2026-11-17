# Security & Secret Management

## Authenticated Encryption (AES-256 GCM)
All credentials are encrypted at rest using AES-256 GCM with unique 96-bit random nonces and versioned JSON envelopes (`v`, `n`, `c`).

## Zero Secret Exposure
- Secret keys are never returned in REST or MCP responses.
- Keys are masked (`sk-1...cdef`) in list operations.
- Production startup validates that `VAULT_MASTER_KEY` and `ADMIN_API_KEY` are not using default development secrets.

## Approval Workflows
High-risk operations (credential deletion, master key rotation) generate an opaque `operation_id` that must be authorized by an admin before execution.

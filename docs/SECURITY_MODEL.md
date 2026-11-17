# Weirwise Token Router — Multi-Tenant Security Model

This document outlines the security architecture, credential vaulting, and tenant isolation model.

---

## 1. Multi-Tenant Authorization & Isolation

Every incoming request is authenticated to a specific `Consumer`:
1. **API Key Authentication**: Hashed with SHA-256 (`ConsumerApiKey.key_hash`). Plaintext secrets are never stored in the database.
2. **Capability Scope Enforcement**: If `allowed_capabilities` is defined (and does not contain `*`), executing any other capability raises `PermissionError` (HTTP 403).
3. **Provider Scope Enforcement**: If `allowed_providers` is defined, requests to unapproved providers raise `PermissionError` (HTTP 403).
4. **Approval & Ledger Isolation**: Consumers can only access approval requests, usage records, and idempotency records tied to their `consumer_id`.

---

## 2. Vault Envelope Encryption (AES-256-GCM)

All third-party credentials and webhook secrets are stored using envelope encryption:
- **Key Versioning**: Keys are prefixed with version headers (e.g. `v1$nonce$ciphertext`).
- **Rotation Support**: Vault supports `register_key_version()` and transparent `reencrypt()`.
- **Zero Raw Exposure**: Secrets are permanently masked (`sk-...1234`) after initial registration. Admin and MCP endpoints only return masked views.

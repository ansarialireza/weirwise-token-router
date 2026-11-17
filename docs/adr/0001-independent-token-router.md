# ADR 0001: Independent Token Router Service Architecture

## Context & Motivation
Previously, `weirwise-rag-platform` embedded its own internal Token Manager, credential encryption vault, and multi-token routing state directly inside the core SaaS application.
This direct coupling had several critical disadvantages:
1. Provider credentials, cooldowns, and failover mechanics were entangled with platform multi-tenant database logic.
2. Credentials and routing policies could not be reused across other applications, independent background workers, external agents, or voice pipelines.
3. Every application restart or platform deployment forced re-initialization of routing state and connection pools.

## Decision
We extracted all provider credential storage, token validation, health monitoring, and intelligent routing into a standalone, independently deployable microservice: `weirwise-token-router`.
- **Zero Database Sharing**: Consumers interact strictly through a versioned REST API (`/api/v1/route`) using authenticated consumer API keys (`X-Consumer-Key`).
- **Secret Zero-Exposure**: Plaintext provider API keys are never exposed in responses, logs, or metrics. Credentials are encrypted using AES-256 GCM authenticated encryption.
- **Failover & Bounded Retries**: The Token Router handles rate-limit backoff, circuit breaking, and failover across multiple keys before returning a response to the consumer.

## Consequences
- Clean separation of concerns: The platform acts purely as an authenticated consumer.
- Reusability: Weirwise Core, Weirwise Platform, internal bots, and future applications can share a single unified pool of provider credentials.
- Multi-Consumer Governance: Consumers have independent rate limits, budgets, and auditable usage ledgers.

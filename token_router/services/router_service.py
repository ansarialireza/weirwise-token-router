"""Unified Application Service Layer for Token Router.

Shared by REST API, MCP Server, and CLI to prevent code duplication.
Supports universal execution across AI, Communication, Payments, and Storage.
"""

import time
import uuid
from decimal import Decimal
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from token_router.adapters.registry import get_adapter
from token_router.core.capabilities import CapabilityRegistry
from token_router.core.contracts import ExecutionEnvelope, ServiceCategory
from token_router.core.config import settings
from token_router.core.vault import generate_api_key, hash_token, mask_credential, vault
from token_router.models.entities import Consumer, ConsumerApiKey, Provider, ProviderCredential, UsageRecord
from token_router.services.approvals import ApprovalManager
from token_router.services.routing_engine import RoutingEngine


class TokenRouterService:
    """Core domain orchestrator encapsulating all business logic."""

    @classmethod
    async def discover_capabilities(
        cls,
        db: AsyncSession,
        consumer: Optional[Consumer] = None,
        category_filter: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Returns structured capability catalog combined with live database health."""
        base_discovery = CapabilityRegistry.discover(category_filter=category_filter)

        # Augment with live provider configured status in DB
        db_providers = list((await db.execute(select(Provider))).scalars().all())
        configured_map = {p.name: p.is_enabled for p in db_providers}

        for p in base_discovery["providers"]:
            p["is_configured"] = p["id"] in configured_map
            p["is_enabled"] = configured_map.get(p["id"], False)

        return base_discovery

    @classmethod
    async def execute_route(
        cls,
        db: AsyncSession,
        consumer: Consumer,
        capability: str,
        model: str,
        request_payload: Dict[str, Any],
        provider_name: Optional[str] = None,
        strategy: Optional[str] = None,
        max_failover_attempts: int = 3,
        timeout_seconds: Optional[float] = None,
        idempotency_key: Optional[str] = None,
        approval_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Shared execution pipeline for REST, MCP, and CLI with safety semantics."""
        # 1. Check consumer permissions
        if consumer.allowed_capabilities and "*" not in consumer.allowed_capabilities:
            if capability not in consumer.allowed_capabilities:
                raise PermissionError(f"Consumer '{consumer.name}' not authorized for capability: {capability}")

        # 2. Check operation safety semantics if registered
        op_def = None
        for svc in CapabilityRegistry.list_services():
            for op in svc.operations:
                if op.id == capability or svc.id == capability:
                    op_def = op
                    break
            if op_def:
                break

        # ENFORCE APPROVAL AT CORE EXECUTION BOUNDARY
        if op_def and op_def.safety.requires_approval:
            ApprovalManager.verify_and_consume_approval(approval_id=approval_id, action_type=op_def.id)

        allows_failover = True
        if op_def and not op_def.safety.allows_automatic_failover:
            allows_failover = False
            max_failover_attempts = 1  # Strict: payments/financial/sms cannot fail over blindly!

        # 3. Locate eligible enabled providers
        provider_stmt = select(Provider).where(Provider.is_enabled.is_(True))
        if provider_name:
            provider_stmt = provider_stmt.where(Provider.name == provider_name.lower())
        providers = list((await db.execute(provider_stmt)).scalars().all())

        if not providers:
            raise LookupError(f"No enabled provider found matching '{provider_name or 'any'}'")

        timeout = timeout_seconds or (op_def.safety.default_timeout_seconds if op_def else settings.DEFAULT_TIMEOUT_SECONDS)
        failover_attempts = 0
        last_error = None
        last_status = 502
        attempted_cred_ids = set()

        for provider in providers:
            adapter = get_adapter(provider.name)
            for _ in range(max_failover_attempts):
                cred = await RoutingEngine.select_credential(
                    db=db,
                    provider=provider,
                    model=model,
                    strategy=strategy,
                    exclude_credential_ids=attempted_cred_ids,
                )
                if not cred:
                    break

                attempted_cred_ids.add(cred.id)
                secret = vault.decrypt(cred.encrypted_secret)
                start_time = time.perf_counter()

                try:
                    status_code, response_data, usage_info = await adapter.execute_operation(
                        secret=secret,
                        operation=capability,
                        payload=request_payload,
                        timeout=timeout,
                        idempotency_key=idempotency_key,
                    )
                    duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

                    await RoutingEngine.handle_request_outcome(
                        db=db,
                        credential=cred,
                        status_code=status_code,
                        error_message=str(response_data) if status_code >= 400 else None,
                    )

                    if status_code < 400:
                        # Append audit log
                        usage = UsageRecord(
                            consumer_id=consumer.id,
                            credential_id=cred.id,
                            provider_name=provider.name,
                            model_name=model,
                            capability=capability,
                            status="success",
                            status_code=status_code,
                            input_tokens=usage_info.get("input_tokens", 0),
                            output_tokens=usage_info.get("output_tokens", 0),
                            total_tokens=usage_info.get("total_tokens", 0),
                            latency_ms=duration_ms,
                            estimated_cost_usd=Decimal("0.0"),
                            failover_attempts=failover_attempts,
                        )
                        db.add(usage)
                        await db.commit()

                        return {
                            "request_id": f"req_{uuid.uuid4().hex[:12]}",
                            "consumer_name": consumer.name,
                            "category": getattr(provider, "category", "ai"),
                            "service": capability,
                            "operation": capability,
                            "provider": provider.name,
                            "model": model,
                            "credential_id": cred.id,
                            "status": "success",
                            "status_code": status_code,
                            "latency_ms": duration_ms,
                            "usage": usage_info,
                            "estimated_cost_usd": Decimal("0.0"),
                            "failover_attempts": failover_attempts,
                            "idempotency_key": idempotency_key,
                            "data": response_data,
                        }

                    # Error handling
                    last_status = status_code
                    last_error = response_data
                    if not allows_failover:
                        # CRITICAL: When failover is forbidden, NEVER try another credential OR provider!
                        raise RuntimeError(
                            f"Operation '{capability}' failed on provider '{provider.name}' with status {status_code}: {response_data}. "
                            "Automatic failover is strictly disabled for non-idempotent/financial operations."
                        )
                    failover_attempts += 1

                except Exception as e:
                    await RoutingEngine.handle_request_outcome(
                        db=db,
                        credential=cred,
                        status_code=500,
                        error_message=str(e),
                    )
                    last_error = {"error": str(e)}
                    if not allows_failover:
                        # CRITICAL: Prevent cross-provider fallback on timeouts or 5xx
                        raise RuntimeError(
                            f"Operation '{capability}' failed on provider '{provider.name}': {str(e)}. "
                            "Cross-provider failover is strictly prohibited for non-failover-safe operations."
                        )
                    failover_attempts += 1

            if not allows_failover:
                break

        raise RuntimeError(f"All routed attempts failed ({last_status}): {last_error}")

    @classmethod
    async def list_credentials_safe(cls, db: AsyncSession, provider_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Safe view of credentials with masked keys."""
        query = select(ProviderCredential)
        if provider_id:
            query = query.where(ProviderCredential.provider_id == provider_id)
        creds = list((await db.execute(query)).scalars().all())

        return [
            {
                "id": c.id,
                "provider_id": c.provider_id,
                "label": c.label,
                "key_prefix": c.key_prefix,
                "masked_preview": mask_credential(c.key_prefix + "..." if c.key_prefix else "sk-..."),
                "is_active": c.is_active,
                "health_status": c.health_status,
                "priority": c.priority,
                "models_allowed": c.models_allowed,
                "consecutive_failures": c.consecutive_failures,
                "cooling_down_until": c.cooling_down_until.isoformat() if c.cooling_down_until else None,
                "last_health_check_at": c.last_health_check_at.isoformat() if c.last_health_check_at else None,
            }
            for c in creds
        ]

    @classmethod
    async def run_diagnostics(cls, db: AsyncSession) -> Dict[str, Any]:
        """Automated system check for DB, vault, and provider states."""
        from token_router.core.vault import vault

        db_healthy = True
        providers_count = 0
        credentials_count = 0
        try:
            p_res = await db.execute(select(Provider))
            providers_count = len(list(p_res.scalars().all()))
            c_res = await db.execute(select(ProviderCredential))
            credentials_count = len(list(c_res.scalars().all()))
        except Exception:
            db_healthy = False

        # Test vault encryption cycle
        vault_healthy = True
        try:
            token = "diag_test_secret"
            encrypted = vault.encrypt(token)
            decrypted = vault.decrypt(encrypted)
            if decrypted != token:
                vault_healthy = False
        except Exception:
            vault_healthy = False

        return {
            "status": "healthy" if (db_healthy and vault_healthy) else "degraded",
            "checks": {
                "database_connection": db_healthy,
                "vault_encryption": vault_healthy,
            },
            "database_connected": db_healthy,
            "vault_encryption_verified": vault_healthy,
            "registered_providers_count": providers_count,
            "registered_credentials_count": credentials_count,
            "supported_categories": CapabilityRegistry.list_categories(),
            "universal_services_count": len(CapabilityRegistry.list_services()),
        }

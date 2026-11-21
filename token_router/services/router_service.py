"""Unified Application Service Layer for Token Router.

Shared by REST API, MCP Server, and CLI to prevent code duplication.
Supports universal execution across AI, Communication, Payments, Storage, and Local inference.
Enforces consumer budget constraints, free-tier intelligent routing,
cost estimation, database-backed approvals, and distributed idempotency.
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
from token_router.core.pricing import PricingRegistry
from token_router.core.quota import QuotaManager
from token_router.core.vault import generate_api_key, hash_token, mask_credential, vault
from token_router.models.entities import Consumer, ConsumerApiKey, Provider, ProviderCredential, UsageRecord
from token_router.services.approvals import PersistentApprovalManager
from token_router.services.idempotency import (
    IdempotencyConflictError,
    IdempotencyExecutionFailedError,
    IdempotencyManager,
    IdempotencyPayloadMismatchError,
    IdempotencyUncertainError,
)
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
        # 1. Enforce Consumer Active State
        if not consumer.is_active:
            raise PermissionError(f"Consumer account '{consumer.name}' is inactive or suspended.")

        # 2. Pre-estimate prompt tokens and cost
        estimated_input_tokens = 200
        if "messages" in request_payload and isinstance(request_payload["messages"], list):
            raw_text = " ".join(str(m.get("content", "")) for m in request_payload["messages"])
            estimated_input_tokens = max(10, len(raw_text) // 4)
        elif "input" in request_payload:
            estimated_input_tokens = max(10, len(str(request_payload["input"])) // 4)

        estimated_initial_cost = PricingRegistry.estimate_cost(
            provider=provider_name or "openai",
            model=model,
            input_tokens=estimated_input_tokens,
            output_tokens=200,
        )

        # 3. Concurrency-Safe Atomic Budget Reservation
        from token_router.services.budget_service import BudgetManager
        await BudgetManager.reserve_budget(
            db=db,
            consumer_id=consumer.id,
            estimated_cost=estimated_initial_cost,
        )

        # 3. Enforce Consumer Capabilities Permission
        if consumer.allowed_capabilities and "*" not in consumer.allowed_capabilities:
            if capability not in consumer.allowed_capabilities:
                await BudgetManager.release_reservation(db=db, consumer_id=consumer.id, estimated_cost=estimated_initial_cost)
                raise PermissionError(f"Consumer '{consumer.name}' not authorized for capability: {capability}")

        # 4. Enforce Consumer Providers Permission
        if provider_name and consumer.allowed_providers and "*" not in consumer.allowed_providers:
            if provider_name.lower() not in [p.lower() for p in consumer.allowed_providers]:
                await BudgetManager.release_reservation(db=db, consumer_id=consumer.id, estimated_cost=estimated_initial_cost)
                raise PermissionError(f"Consumer '{consumer.name}' not authorized to use provider: {provider_name}")

        # 5. Check operation safety semantics from registry
        op_def = None
        for svc in CapabilityRegistry.list_services():
            for op in svc.operations:
                if op.id == capability or svc.id == capability:
                    op_def = op
                    break
            if op_def:
                break

        # 6. Persistent Idempotency Check
        is_new_request = True
        idemp_rec = None
        if idempotency_key:
            is_new_request, idemp_rec = await IdempotencyManager.acquire_or_get(
                db=db,
                consumer_id=consumer.id,
                idempotency_key=idempotency_key,
                capability=capability,
                payload=request_payload,
            )
            if not is_new_request:
                # Release budget reservation since request was already executed and accounted for
                await BudgetManager.release_reservation(db=db, consumer_id=consumer.id, estimated_cost=estimated_initial_cost)
                # ONLY status == 'succeeded' or 'completed' is allowed here
                if idemp_rec.status not in ("succeeded", "completed"):
                    raise RuntimeError(
                        f"Non-successful idempotency status '{idemp_rec.status}' for key '{idempotency_key}'. Cached success response prohibited."
                    )
                return {
                    "request_id": f"req_cached_{uuid.uuid4().hex[:8]}",
                    "consumer_name": consumer.name,
                    "category": "cached",
                    "service": capability,
                    "operation": capability,
                    "provider": "idempotency_cache",
                    "model": model,
                    "credential_id": "cache",
                    "status": "success",
                    "status_code": idemp_rec.status_code or 200,
                    "latency_ms": 0.0,
                    "usage": {"cached": True},
                    "estimated_cost_usd": Decimal("0.0"),
                    "failover_attempts": 0,
                    "idempotency_key": idempotency_key,
                    "data": idemp_rec.response_data,
                }

        # 7. ENFORCE PERSISTENT APPROVAL AT CORE EXECUTION BOUNDARY
        if op_def and op_def.safety.requires_approval:
            target_id = (
                request_payload.get("target_id")
                or request_payload.get("transaction_id")
                or request_payload.get("payment_intent")
                or request_payload.get("payment_intent_id")
                or request_payload.get("charge_id")
                or request_payload.get("id")
            )
            amount = request_payload.get("amount")
            currency = request_payload.get("currency")

            try:
                await PersistentApprovalManager.verify_and_consume(
                    db=db,
                    approval_id=approval_id,
                    action_type=op_def.id,
                    consumer_id=consumer.id,
                    payload=request_payload,
                    provider_name=provider_name,
                    target_id=target_id,
                    amount=amount,
                    currency=currency,
                )
            except Exception:
                await BudgetManager.release_reservation(db=db, consumer_id=consumer.id, estimated_cost=estimated_initial_cost)
                raise

        allows_failover = True
        if op_def and not op_def.safety.allows_automatic_failover:
            allows_failover = False
            max_failover_attempts = 1  # Strict: payments/financial/sms cannot fail over blindly!

        # 8. Locate eligible enabled providers matching consumer allowed_providers
        provider_stmt = select(Provider).where(Provider.is_enabled.is_(True))
        if provider_name:
            provider_stmt = provider_stmt.where(Provider.name == provider_name.lower())
        all_enabled_providers = list((await db.execute(provider_stmt)).scalars().all())

        # Filter by consumer allowed_providers
        if consumer.allowed_providers and "*" not in consumer.allowed_providers:
            allowed_set = {p.lower() for p in consumer.allowed_providers}
            providers = [p for p in all_enabled_providers if p.name.lower() in allowed_set]
        else:
            providers = all_enabled_providers

        if not providers:
            raise LookupError(f"No authorized enabled provider found matching '{provider_name or 'any'}'")

        # Estimate prompt tokens for cost & quota window checks
        estimated_input_tokens = 200
        if "messages" in request_payload and isinstance(request_payload["messages"], list):
            raw_text = " ".join(str(m.get("content", "")) for m in request_payload["messages"])
            estimated_input_tokens = max(10, len(raw_text) // 4)
        elif "input" in request_payload:
            estimated_input_tokens = max(10, len(str(request_payload["input"])) // 4)

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
                    estimated_tokens=estimated_input_tokens,
                )
                if not cred:
                    break

                attempted_cred_ids.add(cred.id)
                secret = vault.decrypt(cred.encrypted_secret)
                start_time = time.perf_counter()

                res_id = None
                try:
                    # 1. Atomically reserve quota across cluster before dispatch
                    res_ok, res_reason, wait_s, res_id = await QuotaManager.reserve_execution(
                        credential_id=cred.id,
                        provider=provider.name,
                        model=model,
                        estimated_tokens=estimated_input_tokens,
                        credential_rpm=cred.rate_limit_rpm,
                        credential_tpm=cred.rate_limit_tpm,
                    )
                    if not res_ok:
                        # Skip this credential if quota was claimed concurrently by another worker
                        continue

                    # 2. Dispatch request to upstream provider with header extraction
                    status_code, response_data, usage_info, resp_headers = await adapter.execute_operation_with_headers(
                        secret=secret,
                        operation=capability,
                        payload=request_payload,
                        timeout=timeout,
                        idempotency_key=idempotency_key,
                        model=model,
                    )
                    duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
                    in_tok = usage_info.get("input_tokens", estimated_input_tokens)
                    out_tok = usage_info.get("output_tokens", 0)
                    total_tok = usage_info.get("total_tokens", in_tok + out_tok)

                    # 3. Dynamic Quota Reconciliation with upstream headers
                    parsed_limits = await QuotaManager.reconcile_from_headers(
                        credential_id=cred.id,
                        provider=provider.name,
                        model=model,
                        headers=resp_headers,
                        status_code=status_code,
                        response_body=response_data,
                    )

                    # 4. Update health status, circuit breaker, and cooldown
                    await RoutingEngine.handle_request_outcome(
                        db=db,
                        credential=cred,
                        status_code=status_code,
                        error_message=str(response_data) if status_code >= 400 else None,
                        tokens_used=total_tok if status_code < 400 else 0,
                        rate_limit_info=parsed_limits,
                    )

                    if status_code < 400:
                        # Calculate granular estimated cost
                        estimated_cost = PricingRegistry.estimate_cost(
                            provider=provider.name,
                            model=model,
                            input_tokens=in_tok,
                            output_tokens=out_tok,
                        )

                        # Reconcile budget reservation with actual cost
                        await BudgetManager.reconcile_actual_cost(
                            db=db,
                            consumer_id=consumer.id,
                            estimated_cost=estimated_initial_cost,
                            actual_cost=estimated_cost,
                        )
                        cred.current_month_spend_usd = Decimal(str(cred.current_month_spend_usd or 0.0)) + estimated_cost

                        # Append audit log
                        usage = UsageRecord(
                            consumer_id=consumer.id,
                            credential_id=cred.id,
                            provider_name=provider.name,
                            model_name=model,
                            capability=capability,
                            status="success",
                            status_code=status_code,
                            input_tokens=in_tok,
                            output_tokens=out_tok,
                            total_tokens=total_tok,
                            latency_ms=duration_ms,
                            estimated_cost_usd=estimated_cost,
                            failover_attempts=failover_attempts,
                        )
                        db.add(usage)
                        await db.commit()

                        # Complete idempotency record
                        if idempotency_key:
                            await IdempotencyManager.mark_completed(
                                db=db,
                                consumer_id=consumer.id,
                                idempotency_key=idempotency_key,
                                status_code=status_code,
                                response_data=response_data,
                            )

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
                            "estimated_cost_usd": estimated_cost,
                            "failover_attempts": failover_attempts,
                            "idempotency_key": idempotency_key,
                            "data": response_data,
                        }

                    # Error handling
                    last_status = status_code
                    last_error = response_data
                    if not allows_failover:
                        if idempotency_key:
                            if status_code < 500:
                                await IdempotencyManager.mark_failed(
                                    db=db,
                                    consumer_id=consumer.id,
                                    idempotency_key=idempotency_key,
                                    status_code=status_code,
                                    error_data=response_data,
                                )
                            else:
                                await IdempotencyManager.mark_uncertain(
                                    db=db,
                                    consumer_id=consumer.id,
                                    idempotency_key=idempotency_key,
                                    last_error=str(response_data),
                                )
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
                        if idempotency_key:
                            await IdempotencyManager.mark_uncertain(
                                db=db,
                                consumer_id=consumer.id,
                                idempotency_key=idempotency_key,
                                last_error=str(e),
                            )
                        raise RuntimeError(
                            f"Operation '{capability}' failed on provider '{provider.name}': {str(e)}. "
                            "Cross-provider failover is strictly prohibited for non-failover-safe operations."
                        )
                    failover_attempts += 1

            if not allows_failover:
                break

        # If all routed attempts failed, release the initial budget reservation
        await BudgetManager.release_reservation(db=db, consumer_id=consumer.id, estimated_cost=estimated_initial_cost)
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
            "pricing_entries_count": len(PricingRegistry.list_pricing()),
        }

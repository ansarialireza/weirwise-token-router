"""Intelligent Provider Selection & Distributed Rate-Limit Routing Engine.

Selects the optimal healthy credential for a given capability, prioritizing:
1. Health & circuit breaker state (healthy/unknown; not cooling down).
2. Rate-limit & quota availability (atomic RPM, TPM, RPD windows).
3. Strategy selection:
   - "free_tier_first": Prioritizes free / local tiers, falling back to paid if quota exhausted.
   - "lowest_cost": Evaluates estimated cost in USD using model pricing.
   - "quality_first": Prioritizes model capability score.
   - "priority", "round_robin", "weighted", "least_recently_used".
4. Dynamic quota feedback & cooldown management.
"""

import asyncio
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from token_router.core.pricing import ModelPricing, PricingRegistry
from token_router.core.quota import QuotaManager
from token_router.core.rate_limits import ParsedRateLimit
from token_router.models.entities import Provider, ProviderCredential


class RoutingEngine:
    """Selects the optimal healthy credential for a given provider and capability."""

    @staticmethod
    def _is_eligible_basic(cred: ProviderCredential, now: datetime) -> bool:
        if not cred.is_active:
            return False
        # Unhealthy, disabled or quota exhausted cannot be routed
        if cred.health_status in ("disabled", "quota_exhausted", "failed"):
            return False
        # Cooldown check
        if cred.cooling_down_until:
            cd_until = cred.cooling_down_until
            if cd_until.tzinfo is None:
                cd_until = cd_until.replace(tzinfo=UTC)
            if cd_until > now:
                return False
        return True

    @classmethod
    async def filter_by_quota(
        cls,
        candidates: List[ProviderCredential],
        provider_name: str,
        model: str,
        estimated_tokens: int = 500,
    ) -> List[ProviderCredential]:
        """Filters out credentials whose RPM/TPM/RPD quota windows are currently exhausted."""
        eligible = []
        for cred in candidates:
            is_ok, reason, wait_sec = await QuotaManager.is_eligible(
                credential_id=cred.id,
                provider=provider_name,
                model=model,
                estimated_tokens=estimated_tokens,
                credential_rpm=cred.rate_limit_rpm,
                credential_tpm=cred.rate_limit_tpm,
            )
            if is_ok:
                eligible.append(cred)
        return eligible

    @classmethod
    async def select_credential(
        cls,
        db: AsyncSession,
        provider: Provider,
        model: str = "auto",
        strategy: Optional[str] = None,
        exclude_credential_ids: Optional[Set[str]] = None,
        estimated_tokens: int = 500,
        min_quality: float = 0.0,
    ) -> Optional[ProviderCredential]:
        """Applies chosen routing policy (free_tier_first, lowest_cost, quality_first, priority,

        round_robin, weighted, least_recently_used) to return the optimal active credential.
        """
        now = datetime.now(UTC)

        stmt = (
            select(ProviderCredential)
            .where(ProviderCredential.provider_id == provider.id)
            .order_by(ProviderCredential.priority.asc())
        )
        candidates = list((await db.execute(stmt)).scalars().all())
        if exclude_credential_ids:
            candidates = [c for c in candidates if c.id not in exclude_credential_ids]

        # 1. Basic eligibility (active, not disabled/failed, not in cooldown)
        eligible = [c for c in candidates if cls._is_eligible_basic(c, now)]
        if not eligible:
            # Fallback: if all healthy are cooling down or none tested yet, consider "unknown"
            eligible = [c for c in candidates if c.is_active and c.health_status in ("unknown", "healthy")]

        if not eligible:
            return None

        # 2. Filter by model allowance if model is specified
        if model != "auto":
            model_matched = [c for c in eligible if "*" in c.models_allowed or model in c.models_allowed]
            if model_matched:
                eligible = model_matched

        # 3. Quota check: RPM/TPM/RPD window availability
        quota_eligible = await cls.filter_by_quota(
            candidates=eligible,
            provider_name=provider.name,
            model=model,
            estimated_tokens=estimated_tokens,
        )
        effective_pool = quota_eligible if quota_eligible else eligible

        effective_strategy = strategy or provider.default_routing_strategy or "free_tier_first"

        # 4. Strategy execution
        if effective_strategy == "free_tier_first":
            pricing_info = PricingRegistry.get_model_pricing(provider.name, model)
            is_model_free = pricing_info and pricing_info.tier in ("free", "local")

            free_candidates = [
                c for c in effective_pool
                if (c.priority <= 5 or "free" in c.label.lower() or is_model_free)
            ]
            if free_candidates:
                return sorted(free_candidates, key=lambda c: (c.last_used_at or datetime.min.replace(tzinfo=UTC)))[0]
            return sorted(effective_pool, key=lambda c: c.priority)[0]

        elif effective_strategy == "lowest_cost":
            pricing_cost = PricingRegistry.estimate_cost(provider.name, model, input_tokens=estimated_tokens)
            return sorted(effective_pool, key=lambda c: (pricing_cost, c.current_month_spend_usd))[0]

        elif effective_strategy == "quality_first":
            pricing_info = PricingRegistry.get_model_pricing(provider.name, model)
            score = pricing_info.quality_score if pricing_info else 5.0
            return sorted(effective_pool, key=lambda c: (-score, c.priority))[0]

        elif effective_strategy == "priority":
            return sorted(effective_pool, key=lambda c: c.priority)[0]

        elif effective_strategy == "least_recently_used":
            return sorted(effective_pool, key=lambda c: (c.last_used_at or datetime.min.replace(tzinfo=UTC)))[0]

        elif effective_strategy == "weighted":
            total_weight = sum(max(c.routing_weight, 1) for c in effective_pool)
            r = random.uniform(0, total_weight)
            upto = 0.0
            for c in effective_pool:
                w = max(c.routing_weight, 1)
                if upto + w >= r:
                    return c
                upto += w
            return effective_pool[0]

        else:
            return random.choice(effective_pool)

    @classmethod
    async def handle_request_outcome(
        cls,
        db: AsyncSession,
        credential: ProviderCredential,
        status_code: int,
        error_message: Optional[str] = None,
        retry_after_seconds: Optional[int] = None,
        tokens_used: int = 0,
        rate_limit_info: Optional[ParsedRateLimit] = None,
    ) -> None:
        """Updates health status, failure counters, quota window, and triggers circuit breaker / cooldown."""
        now = datetime.now(UTC)
        credential.last_used_at = now

        # If provider returned explicit retry-after or throttling headers
        if rate_limit_info and rate_limit_info.retry_after_seconds is not None:
            cooldown_sec = max(1, int(rate_limit_info.retry_after_seconds))
            credential.cooling_down_until = now + timedelta(seconds=cooldown_sec)

        if status_code in (200, 201):
            credential.health_status = "healthy"
            credential.consecutive_failures = 0
            credential.last_error_reason = None
            credential.last_error_code = None
            if not (rate_limit_info and rate_limit_info.retry_after_seconds):
                credential.cooling_down_until = None

        elif status_code in (401, 403):
            credential.health_status = "failed"
            credential.consecutive_failures += 1
            credential.last_error_reason = error_message or "Authentication failed (401/403)"
            credential.last_error_code = status_code

        elif status_code == 429:
            cooldown = retry_after_seconds or (int(rate_limit_info.retry_after_seconds) if (rate_limit_info and rate_limit_info.retry_after_seconds) else 60)
            credential.health_status = "rate_limited"
            credential.consecutive_failures += 1
            credential.cooling_down_until = now + timedelta(seconds=cooldown)
            credential.last_error_reason = error_message or "Rate limit exceeded (429)"
            credential.last_error_code = 429

        elif status_code in (500, 502, 503, 504):
            credential.consecutive_failures += 1
            credential.last_error_reason = error_message or f"Upstream provider error ({status_code})"
            credential.last_error_code = status_code
            if credential.consecutive_failures >= 3:
                credential.health_status = "degraded"
                credential.cooling_down_until = now + timedelta(seconds=30)
        else:
            credential.consecutive_failures += 1
            credential.last_error_reason = error_message or f"HTTP {status_code}"
            credential.last_error_code = status_code

        await db.commit()

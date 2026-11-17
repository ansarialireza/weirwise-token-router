import asyncio
import random
from datetime import UTC, datetime, timedelta
from typing import List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from token_router.models.entities import Provider, ProviderCredential


class RoutingEngine:
    """Selects the optimal healthy credential for a given provider and capability."""

    @staticmethod
    def _is_eligible(cred: ProviderCredential, now: datetime) -> bool:
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
    async def select_credential(
        cls,
        db: AsyncSession,
        provider: Provider,
        model: str = "auto",
        strategy: Optional[str] = None,
    ) -> Optional[ProviderCredential]:
        """Applies chosen routing policy (priority, round_robin, weighted, least_recently_used, lowest_cost)

        to return the optimal active credential.
        """
        now = datetime.now(UTC)

        stmt = (
            select(ProviderCredential)
            .where(ProviderCredential.provider_id == provider.id)
            .order_by(ProviderCredential.priority.asc())
        )
        candidates = list((await db.execute(stmt)).scalars().all())

        eligible = [c for c in candidates if cls._is_eligible(c, now)]
        if not eligible:
            # Fallback: if all healthy are cooling down or none tested yet, consider "unknown"
            eligible = [c for c in candidates if c.is_active and c.health_status in ("unknown", "healthy")]

        if not eligible:
            return None

        # Filter by model allowance if model is specified
        if model != "auto":
            model_matched = [c for c in eligible if "*" in c.models_allowed or model in c.models_allowed]
            if model_matched:
                eligible = model_matched

        effective_strategy = strategy or provider.default_routing_strategy or "round_robin"

        if effective_strategy == "priority":
            # Sort strictly by priority (lower integer = higher priority)
            return sorted(eligible, key=lambda c: c.priority)[0]

        elif effective_strategy == "least_recently_used":
            # Return credential with oldest or None last_used_at
            return sorted(eligible, key=lambda c: (c.last_used_at or datetime.min.replace(tzinfo=UTC)))[0]

        elif effective_strategy == "weighted":
            total_weight = sum(max(c.routing_weight, 1) for c in eligible)
            r = random.uniform(0, total_weight)
            upto = 0.0
            for c in eligible:
                w = max(c.routing_weight, 1)
                if upto + w >= r:
                    return c
                upto += w
            return eligible[0]

        elif effective_strategy == "lowest_cost":
            # Prefer credentials with lower current_month_spend_usd to balance spend
            return sorted(eligible, key=lambda c: c.current_month_spend_usd)[0]

        else:
            # Default: Round Robin with jitter
            return random.choice(eligible)

    @classmethod
    async def handle_request_outcome(
        cls,
        db: AsyncSession,
        credential: ProviderCredential,
        status_code: int,
        error_message: Optional[str] = None,
        retry_after_seconds: Optional[int] = None,
    ) -> None:
        """Updates health status, failure counters, and triggers circuit breaker / cooldown."""
        now = datetime.now(UTC)
        credential.last_used_at = now

        if status_code in (200, 201):
            credential.health_status = "healthy"
            credential.consecutive_failures = 0
            credential.last_error_reason = None
            credential.last_error_code = None
            credential.cooling_down_until = None

        elif status_code in (401, 403):
            # Authentication failure -> immediate failure, disable credential
            credential.health_status = "failed"
            credential.consecutive_failures += 1
            credential.last_error_reason = error_message or "Authentication failed (401/403)"
            credential.last_error_code = status_code

        elif status_code == 429:
            # Rate limited
            cooldown = retry_after_seconds or 60
            credential.health_status = "rate_limited"
            credential.consecutive_failures += 1
            credential.cooling_down_until = now + timedelta(seconds=cooldown)
            credential.last_error_reason = error_message or "Rate limit exceeded (429)"
            credential.last_error_code = 429

        elif status_code in (500, 502, 503, 504):
            # Upstream server error / timeout
            credential.consecutive_failures += 1
            credential.last_error_reason = error_message or f"Upstream provider error ({status_code})"
            credential.last_error_code = status_code
            if credential.consecutive_failures >= 3:
                # Trip circuit breaker
                credential.health_status = "degraded"
                credential.cooling_down_until = now + timedelta(seconds=30)
        else:
            credential.consecutive_failures += 1
            credential.last_error_reason = error_message or f"HTTP {status_code}"
            credential.last_error_code = status_code

        await db.commit()

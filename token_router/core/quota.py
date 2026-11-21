"""Distributed Quota Management & Rate Limit Intelligence.

Implements centralized, multi-instance quota tracking supporting:
1. Redis-backed sliding window counters with atomic reservations via Lua scripts.
2. Explicit failure policies:
   - "fail_closed": When distributed coordination (Redis) is unreachable in distributed mode,
     rejection occurs with a clear RedisUnavailableError (HTTP 503/429) rather than silent local degradation.
   - "local_fallback": Allowed only in explicitly configured single-instance mode.
3. Two-phase reservation lifecycle:
   - Unique reservation ticket (`reservation_id`).
   - Atomic reservation before dispatch (`reserve_quota`).
   - Safe post-dispatch reconciliation or release on confirmed pre-dispatch failure.
4. Model-level and credential-level isolation.
5. Dynamic reconciliation with upstream rate-limit headers.
"""

import asyncio
from collections import deque
from datetime import UTC, datetime, timedelta
import logging
from typing import Any, Dict, List, Optional, Tuple
import uuid
from pydantic import BaseModel, Field

from token_router.core.config import settings
from token_router.core.rate_limits import ParsedRateLimit

logger = logging.getLogger(__name__)


class RedisUnavailableError(RuntimeError):
    """Raised when Redis is required for distributed quota coordination but is unreachable."""
    pass


class QuotaWindow(BaseModel):
    """Documented quota window specification for a model or credential tier."""
    rpm_limit: Optional[int] = None
    tpm_limit: Optional[int] = None
    rpd_limit: Optional[int] = None
    is_hard_limit: bool = True
    window_type: str = "rolling"
    reset_schedule_desc: str = "Rolling 60-second window"


class QuotaReservationTicket(BaseModel):
    """Immutable reservation ticket tracking a pending reservation."""
    reservation_id: str
    credential_id: str
    provider: str
    model: str
    estimated_tokens: int
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    committed: bool = False
    released: bool = False


# Canonical Documented Free-Tier Quota Limits:
DOCUMENTED_FREE_TIER_QUOTAS: Dict[str, Dict[str, QuotaWindow]] = {
    "gemini": {
        "gemini-1.5-flash": QuotaWindow(
            rpm_limit=15,
            tpm_limit=1000000,
            rpd_limit=1500,
            is_hard_limit=True,
            window_type="calendar_day",
            reset_schedule_desc="15 RPM rolling, 1500 RPD resets at 00:00 UTC",
        ),
        "gemini-1.5-pro": QuotaWindow(
            rpm_limit=2,
            tpm_limit=32000,
            rpd_limit=50,
            is_hard_limit=True,
            window_type="calendar_day",
            reset_schedule_desc="2 RPM rolling, 50 RPD resets at 00:00 UTC",
        ),
        "gemini-2.0-flash": QuotaWindow(
            rpm_limit=10,
            tpm_limit=4000000,
            rpd_limit=1500,
            is_hard_limit=True,
            window_type="calendar_day",
            reset_schedule_desc="10 RPM rolling, 1500 RPD resets at 00:00 UTC",
        ),
        "gemini-3.5-flash-lite": QuotaWindow(
            rpm_limit=15,
            tpm_limit=1000000,
            rpd_limit=1500,
            is_hard_limit=True,
            window_type="calendar_day",
            reset_schedule_desc="15 RPM rolling, 1500 RPD resets at 00:00 UTC (Verified 2026)",
        ),
    },
    "openrouter": {
        "meta-llama/llama-3.1-8b-instruct:free": QuotaWindow(
            rpm_limit=20,
            tpm_limit=200000,
            rpd_limit=200,
            is_hard_limit=True,
            window_type="calendar_day",
            reset_schedule_desc="20 RPM rolling, 200 RPD",
        ),
        "google/gemini-2.0-flash-exp:free": QuotaWindow(
            rpm_limit=20,
            tpm_limit=200000,
            rpd_limit=200,
            is_hard_limit=True,
            window_type="calendar_day",
            reset_schedule_desc="20 RPM rolling, 200 RPD",
        ),
    },
}

# Redis Lua Script for Atomic Quota Reservation
REDIS_RESERVE_SCRIPT = """
local rpm_key = KEYS[1]
local tpm_key = KEYS[2]
local rpd_key = KEYS[3]

local now = tonumber(ARGV[1])
local res_id = ARGV[2]
local tokens = tonumber(ARGV[3])
local rpm_limit = tonumber(ARGV[4])
local tpm_limit = tonumber(ARGV[5])
local rpd_limit = tonumber(ARGV[6])
local rpd_expire = tonumber(ARGV[7])

local window_cutoff = now - 60.0

-- 0. Check upstream throttling or active cooldown
local cd_key = (string.gsub(rpm_key, ':rpm$', ':cooldown'))
local cooldown = redis.call('GET', cd_key)
if cooldown then
    return {0, "Upstream rate-limit throttling in effect", 0, 0, 0, 10.0}
end

local rem_key = (string.gsub(rpm_key, ':rpm$', ':upstream_rem_req'))
local upstream_rem = redis.call('GET', rem_key)
if upstream_rem and tonumber(upstream_rem) <= 0 then
    return {0, "Upstream rate-limit throttling reported 0 remaining requests", 0, 0, 0, 10.0}
end

-- 1. Purge expired entries older than 60s
redis.call('ZREMRANGEBYSCORE', rpm_key, '-inf', window_cutoff)
redis.call('ZREMRANGEBYSCORE', tpm_key, '-inf', window_cutoff)

-- 2. Count current RPM
local current_rpm = redis.call('ZCARD', rpm_key)
if rpm_limit > 0 and current_rpm >= rpm_limit then
    local oldest = redis.call('ZRANGE', rpm_key, 0, 0, 'WITHSCORES')
    local wait_sec = 60.0
    if oldest and #oldest >= 2 then
        wait_sec = math.max(0.1, 60.0 - (now - tonumber(oldest[2])))
    end
    return {0, string.format("RPM limit exceeded (%d/%d)", current_rpm, rpm_limit), current_rpm, 0, 0, wait_sec}
end

-- 3. Calculate current TPM
local current_tpm = 0
local tpm_members = redis.call('ZRANGE', tpm_key, 0, -1)
for _, mem in ipairs(tpm_members) do
    local tok = tonumber(string.match(mem, ":(%d+)$") or 0)
    current_tpm = current_tpm + tok
end

if tpm_limit > 0 and (current_tpm + tokens) > tpm_limit then
    local oldest = redis.call('ZRANGE', tpm_key, 0, 0, 'WITHSCORES')
    local wait_sec = 60.0
    if oldest and #oldest >= 2 then
        wait_sec = math.max(0.1, 60.0 - (now - tonumber(oldest[2])))
    end
    return {0, string.format("TPM limit exceeded (%d + %d > %d)", current_tpm, tokens, tpm_limit), current_rpm, current_tpm, 0, wait_sec}
end

-- 4. Check RPD
local current_rpd = tonumber(redis.call('GET', rpd_key) or 0)
if rpd_limit > 0 and current_rpd >= rpd_limit then
    return {0, string.format("Daily RPD limit exceeded (%d/%d)", current_rpd, rpd_limit), current_rpm, current_tpm, current_rpd, 3600.0}
end

-- 5. Atomically reserve slot
redis.call('ZADD', rpm_key, now, res_id)
redis.call('EXPIRE', rpm_key, 70)

redis.call('ZADD', tpm_key, now, string.format("%s:%d", res_id, tokens))
redis.call('EXPIRE', tpm_key, 70)

local new_rpd = redis.call('INCR', rpd_key)
if new_rpd == 1 then
    redis.call('EXPIRE', rpd_key, rpd_expire)
end

return {1, "OK", current_rpm + 1, current_tpm + tokens, new_rpd, 0}
"""

# Redis Lua Script for Releasing a Failed Pre-Dispatch Reservation
REDIS_RELEASE_SCRIPT = """
local rpm_key = KEYS[1]
local tpm_key = KEYS[2]
local rpd_key = KEYS[3]

local res_id = ARGV[1]
local tokens = tonumber(ARGV[2])

redis.call('ZREM', rpm_key, res_id)
redis.call('ZREM', tpm_key, string.format("%s:%d", res_id, tokens))

local cur_rpd = tonumber(redis.call('GET', rpd_key) or 0)
if cur_rpd > 0 then
    redis.call('DECR', rpd_key)
end

return 1
"""


class CredentialUsageWindow:
    """Thread-safe, process-local window for single-instance mode only."""

    def __init__(self, key: str):
        self.key = key
        self._lock = asyncio.Lock()
        self.minute_records: deque[Tuple[float, int, str]] = deque()  # (ts, tokens, res_id)
        self.current_day_str: str = datetime.now(UTC).strftime("%Y-%m-%d")
        self.daily_request_count: int = 0
        self.daily_token_count: int = 0
        self.upstream_remaining_requests: Optional[int] = None
        self.upstream_remaining_tokens: Optional[int] = None
        self.last_reconciliation_at: Optional[datetime] = None

    def _purge_expired(self, now: float) -> None:
        cutoff_minute = now - 60.0
        while self.minute_records and self.minute_records[0][0] < cutoff_minute:
            self.minute_records.popleft()

        today_str = datetime.now(UTC).strftime("%Y-%m-%d")
        if today_str != self.current_day_str:
            self.current_day_str = today_str
            self.daily_request_count = 0
            self.daily_token_count = 0

    async def reserve_or_check(
        self,
        quota: QuotaWindow,
        estimated_tokens: int = 0,
        commit: bool = True,
        reservation_id: Optional[str] = None,
    ) -> Tuple[bool, Optional[str], Optional[float]]:
        async with self._lock:
            now_dt = datetime.now(UTC)
            now = now_dt.timestamp()
            self._purge_expired(now)

            if self.upstream_remaining_requests is not None and self.upstream_remaining_requests <= 0:
                return False, "Upstream rate-limit throttling reported 0 remaining requests", 10.0

            if quota.rpm_limit is not None:
                current_rpm = len(self.minute_records)
                if current_rpm >= quota.rpm_limit:
                    oldest_ts = self.minute_records[0][0]
                    wait_sec = max(0.1, round(60.0 - (now - oldest_ts), 2))
                    return False, f"RPM limit exceeded ({current_rpm}/{quota.rpm_limit})", wait_sec

            if quota.tpm_limit is not None:
                current_tpm = sum(tokens for _, tokens, _ in self.minute_records)
                if current_tpm + estimated_tokens > quota.tpm_limit:
                    oldest_ts = self.minute_records[0][0]
                    wait_sec = max(0.1, round(60.0 - (now - oldest_ts), 2))
                    return False, f"TPM limit exceeded ({current_tpm} + {estimated_tokens} > {quota.tpm_limit})", wait_sec

            if quota.rpd_limit is not None:
                if self.daily_request_count >= quota.rpd_limit:
                    tomorrow_midnight = (now_dt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                    wait_sec = max(1.0, (tomorrow_midnight - now_dt).total_seconds())
                    return False, f"Daily RPD limit exceeded ({self.daily_request_count}/{quota.rpd_limit})", wait_sec

            if commit:
                r_id = reservation_id or str(uuid.uuid4())
                self.minute_records.append((now, estimated_tokens, r_id))
                self.daily_request_count += 1
                self.daily_token_count += estimated_tokens

            return True, None, None

    async def release_reservation(self, reservation_id: str, estimated_tokens: int) -> None:
        async with self._lock:
            for i, (ts, tok, r_id) in enumerate(list(self.minute_records)):
                if r_id == reservation_id:
                    del self.minute_records[i]
                    self.daily_request_count = max(0, self.daily_request_count - 1)
                    self.daily_token_count = max(0, self.daily_token_count - estimated_tokens)
                    break

    async def reconcile(self, rate_limit: ParsedRateLimit) -> None:
        async with self._lock:
            if self.last_reconciliation_at and rate_limit.timestamp < self.last_reconciliation_at:
                # Ignore stale headers
                return
            self.last_reconciliation_at = rate_limit.timestamp
            if rate_limit.remaining_requests is not None:
                self.upstream_remaining_requests = rate_limit.remaining_requests
            if rate_limit.remaining_tokens is not None:
                self.upstream_remaining_tokens = rate_limit.remaining_tokens

    async def check_availability(self, quota: QuotaWindow, estimated_tokens: int = 0) -> Tuple[bool, Optional[str], Optional[float]]:
        return await self.reserve_or_check(quota=quota, estimated_tokens=estimated_tokens, commit=False)

    async def record_usage(self, token_count: int = 0) -> None:
        async with self._lock:
            now = datetime.now(UTC).timestamp()
            self._purge_expired(now)
            self.minute_records.append((now, token_count, str(uuid.uuid4())))
            self.daily_request_count += 1
            self.daily_token_count += token_count

    async def get_metrics(self) -> Dict[str, Any]:
        async with self._lock:
            now = datetime.now(UTC).timestamp()
            self._purge_expired(now)
            return {
                "key": self.key,
                "mode": "memory_fallback (single-instance only)",
                "current_rpm": len(self.minute_records),
                "current_tpm": sum(t for _, t, _ in self.minute_records),
                "daily_request_count": self.daily_request_count,
                "daily_token_count": self.daily_token_count,
                "calendar_day": self.current_day_str,
                "upstream_remaining_requests": self.upstream_remaining_requests,
                "upstream_remaining_tokens": self.upstream_remaining_tokens,
                "last_reconciliation_at": self.last_reconciliation_at.isoformat() if self.last_reconciliation_at else None,
            }


class DistributedQuotaTracker:
    """Distributed Quota Tracker managing multi-instance reservations via Redis."""

    def __init__(
        self,
        redis_url: Optional[str] = None,
        distributed_mode: Optional[bool] = None,
        failure_policy: Optional[str] = None,
    ):
        self.redis_url = redis_url if redis_url is not None else settings.REDIS_URL
        self.distributed_mode = distributed_mode if distributed_mode is not None else settings.DISTRIBUTED_MODE
        self.failure_policy = failure_policy or settings.QUOTA_FAILURE_POLICY
        self._redis_client = None
        self._reserve_sha: Optional[str] = None
        self._release_sha: Optional[str] = None
        self._memory_windows: Dict[str, CredentialUsageWindow] = {}
        self._redis_available = False

    async def _get_redis(self):
        if not self.redis_url:
            return None
        if self._redis_client is None:
            try:
                import redis.asyncio as aioredis
                self._redis_client = aioredis.from_url(
                    self.redis_url,
                    decode_responses=True,
                    socket_connect_timeout=2.0,
                    socket_timeout=2.0,
                )
                await self._redis_client.ping()
                self._reserve_sha = await self._redis_client.script_load(REDIS_RESERVE_SCRIPT)
                self._release_sha = await self._redis_client.script_load(REDIS_RELEASE_SCRIPT)
                self._redis_available = True
                logger.info("Connected to Redis: %s", self.redis_url)

                if settings.REQUIRE_REDIS_PERSISTENCE:
                    try:
                        cfg_appendonly = await self._redis_client.config_get("appendonly")
                        val = cfg_appendonly.get("appendonly", "no") if isinstance(cfg_appendonly, dict) else "no"
                        if str(val).lower() != "yes":
                            raise RuntimeError(
                                f"Redis at {self.redis_url} has appendonly={val}. REQUIRE_REDIS_PERSISTENCE=true requires AOF ('appendonly yes')."
                            )
                    except Exception as pe:
                        if isinstance(pe, RuntimeError):
                            raise
                        logger.warning("Could not verify Redis persistence via CONFIG GET: %s", str(pe))
            except Exception as e:
                logger.warning("Redis connection failure: %s", str(e))
                self._redis_client = None
                self._redis_available = False
        return self._redis_client

    def _get_memory_window(self, key: str) -> CredentialUsageWindow:
        if key not in self._memory_windows:
            self._memory_windows[key] = CredentialUsageWindow(key)
        return self._memory_windows[key]

    async def reserve_quota(
        self,
        credential_id: str,
        provider: str,
        model: str,
        quota: QuotaWindow,
        estimated_tokens: int = 500,
        commit: bool = True,
        reservation_id: Optional[str] = None,
    ) -> Tuple[bool, Optional[str], Optional[float], Optional[str]]:
        """Atomically checks and reserves RPM, TPM, and RPD quota.

        Returns (is_allowed, reason_if_rejected, wait_seconds, reservation_id).
        """
        r_id = reservation_id or f"res_{uuid.uuid4().hex[:12]}"
        key_base = f"quota:{provider.lower()}:{credential_id}"
        if model != "auto":
            key_base += f":{model.lower()}"

        redis_client = await self._get_redis()
        if redis_client and self._redis_available and commit:
            try:
                now_dt = datetime.now(UTC)
                now_ts = now_dt.timestamp()

                tomorrow = (now_dt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                rpd_expire = max(60, int((tomorrow - now_dt).total_seconds()))

                keys = [
                    f"{key_base}:rpm",
                    f"{key_base}:tpm",
                    f"{key_base}:rpd:{now_dt.strftime('%Y-%m-%d')}",
                ]
                args = [
                    str(now_ts),
                    r_id,
                    str(estimated_tokens),
                    str(quota.rpm_limit or -1),
                    str(quota.tpm_limit or -1),
                    str(quota.rpd_limit or -1),
                    str(rpd_expire),
                ]

                res = await redis_client.evalsha(self._reserve_sha, len(keys), *keys, *args)
                allowed = bool(res[0])
                reason = str(res[1])
                wait_sec = float(res[5]) if len(res) > 5 else 0.0

                if not allowed:
                    return False, reason, wait_sec, None
                return True, None, None, r_id

            except Exception as e:
                logger.warning("Redis reservation failed: %s", str(e))
                self._redis_available = False

        # If Redis is unavailable or unconfigured:
        if self.distributed_mode or self.failure_policy == "fail_closed":
            raise RedisUnavailableError(
                f"Distributed quota service unreachable ({self.redis_url or 'unconfigured'}). "
                "Fail-closed policy prevented unsafe multi-instance quota oversubscription."
            )

        # In-memory tracking (for local mode or non-committed candidate checks)
        mem = self._get_memory_window(key_base)
        ok, reason, wait_sec = await mem.reserve_or_check(
            quota, estimated_tokens=estimated_tokens, commit=commit, reservation_id=r_id
        )
        return ok, reason, wait_sec, r_id if ok else None

    async def release_reservation(
        self,
        credential_id: str,
        provider: str,
        model: str,
        reservation_id: str,
        estimated_tokens: int,
    ) -> None:
        """Safely rolls back a pre-dispatch quota reservation upon confirmed pre-dispatch failure."""
        key_base = f"quota:{provider.lower()}:{credential_id}"
        if model != "auto":
            key_base += f":{model.lower()}"

        redis_client = await self._get_redis()
        if redis_client and self._redis_available:
            try:
                now_dt = datetime.now(UTC)
                keys = [
                    f"{key_base}:rpm",
                    f"{key_base}:tpm",
                    f"{key_base}:rpd:{now_dt.strftime('%Y-%m-%d')}",
                ]
                await redis_client.evalsha(self._release_sha, len(keys), *keys, reservation_id, str(estimated_tokens))
                return
            except Exception as e:
                logger.warning("Failed to release Redis reservation: %s", str(e))

        mem = self._get_memory_window(key_base)
        await mem.release_reservation(reservation_id, estimated_tokens)

    async def reconcile_upstream(
        self,
        credential_id: str,
        provider: str,
        model: str,
        rate_limit: ParsedRateLimit,
    ) -> None:
        key_base = f"quota:{provider.lower()}:{credential_id}"
        if model != "auto":
            key_base += f":{model.lower()}"

        mem = self._get_memory_window(key_base)
        await mem.reconcile(rate_limit)

        redis_client = await self._get_redis()
        if redis_client and self._redis_available and rate_limit.has_quota_info:
            try:
                pipe = redis_client.pipeline()
                if rate_limit.remaining_requests is not None:
                    pipe.set(f"{key_base}:upstream_rem_req", rate_limit.remaining_requests, ex=120)
                if rate_limit.remaining_tokens is not None:
                    pipe.set(f"{key_base}:upstream_rem_tok", rate_limit.remaining_tokens, ex=120)
                if rate_limit.retry_after_seconds is not None:
                    pipe.set(f"{key_base}:cooldown", "1", ex=max(1, int(rate_limit.retry_after_seconds)))
                await pipe.execute()
            except Exception as e:
                logger.warning("Failed to reconcile upstream headers to Redis: %s", str(e))

    async def get_metrics(self, credential_id: str, provider: str = "generic", model: str = "auto") -> Dict[str, Any]:
        key_base = f"quota:{provider.lower()}:{credential_id}"
        if model != "auto":
            key_base += f":{model.lower()}"

        redis_client = await self._get_redis()
        if redis_client and self._redis_available:
            try:
                today_str = datetime.now(UTC).strftime('%Y-%m-%d')
                rpm_count = await redis_client.zcard(f"{key_base}:rpm")
                rpd_count = int(await redis_client.get(f"{key_base}:rpd:{today_str}") or 0)
                tpm_members = await redis_client.zrange(f"{key_base}:tpm", 0, -1)
                tpm_count = sum(int(str(m).split(":")[-1]) for m in tpm_members if ":" in str(m))
                rem_req = await redis_client.get(f"{key_base}:upstream_rem_req")
                rem_tok = await redis_client.get(f"{key_base}:upstream_rem_tok")

                # Inspect persistence health if supported
                aof_enabled = False
                try:
                    cfg_aof = await redis_client.config_get("appendonly")
                    aof_enabled = (cfg_aof.get("appendonly", "no") == "yes") if isinstance(cfg_aof, dict) else False
                except Exception:
                    pass

                return {
                    "key": key_base,
                    "mode": "redis_distributed",
                    "redis_persistence": "aof_enabled" if aof_enabled else "rdb_only",
                    "current_rpm": rpm_count,
                    "current_tpm": tpm_count,
                    "daily_request_count": rpd_count,
                    "calendar_day": today_str,
                    "upstream_remaining_requests": int(rem_req) if rem_req else None,
                    "upstream_remaining_tokens": int(rem_tok) if rem_tok else None,
                }
            except Exception as e:
                logger.warning("Failed to fetch Redis metrics: %s", str(e))

        mem = self._get_memory_window(key_base)
        return await mem.get_metrics()


class QuotaManager:
    """Global access point for distributed quota management."""

    _tracker = DistributedQuotaTracker()

    @classmethod
    def set_tracker(cls, tracker: DistributedQuotaTracker) -> None:
        cls._tracker = tracker

    @classmethod
    def get_tracker(cls) -> DistributedQuotaTracker:
        return cls._tracker

    @classmethod
    def get_quota_definition(
        cls,
        provider: str,
        model: str,
        credential_rpm: Optional[int] = None,
        credential_tpm: Optional[int] = None,
    ) -> QuotaWindow:
        p_name = provider.lower()
        m_name = model.lower()

        doc_quota = DOCUMENTED_FREE_TIER_QUOTAS.get(p_name, {}).get(m_name)
        if doc_quota:
            eff_rpm = min(doc_quota.rpm_limit, credential_rpm) if (credential_rpm and doc_quota.rpm_limit) else (credential_rpm or doc_quota.rpm_limit)
            eff_tpm = min(doc_quota.tpm_limit, credential_tpm) if (credential_tpm and doc_quota.tpm_limit) else (credential_tpm or doc_quota.tpm_limit)
            return QuotaWindow(
                rpm_limit=eff_rpm,
                tpm_limit=eff_tpm,
                rpd_limit=doc_quota.rpd_limit,
                is_hard_limit=doc_quota.is_hard_limit,
                window_type=doc_quota.window_type,
                reset_schedule_desc=doc_quota.reset_schedule_desc,
            )

        rpm = credential_rpm if credential_rpm and credential_rpm > 0 else 60
        tpm = credential_tpm if credential_tpm and credential_tpm > 0 else 100000
        return QuotaWindow(
            rpm_limit=rpm,
            tpm_limit=tpm,
            rpd_limit=None,
            is_hard_limit=False,
            window_type="rolling",
            reset_schedule_desc=f"{rpm} RPM rolling window",
        )

    @classmethod
    async def is_eligible(
        cls,
        credential_id: str,
        provider: str,
        model: str,
        estimated_tokens: int = 0,
        credential_rpm: Optional[int] = None,
        credential_tpm: Optional[int] = None,
    ) -> Tuple[bool, Optional[str], Optional[float]]:
        quota_def = cls.get_quota_definition(provider, model, credential_rpm, credential_tpm)
        ok, reason, wait_sec, _ = await cls._tracker.reserve_quota(
            credential_id=credential_id,
            provider=provider,
            model=model,
            quota=quota_def,
            estimated_tokens=estimated_tokens,
            commit=False,
        )
        return ok, reason, wait_sec

    @classmethod
    async def reserve_execution(
        cls,
        credential_id: str,
        provider: str,
        model: str,
        estimated_tokens: int = 500,
        credential_rpm: Optional[int] = None,
        credential_tpm: Optional[int] = None,
    ) -> Tuple[bool, Optional[str], Optional[float], Optional[str]]:
        quota_def = cls.get_quota_definition(provider, model, credential_rpm, credential_tpm)
        return await cls._tracker.reserve_quota(
            credential_id=credential_id,
            provider=provider,
            model=model,
            quota=quota_def,
            estimated_tokens=estimated_tokens,
            commit=True,
        )

    @classmethod
    async def release_reservation(
        cls,
        credential_id: str,
        provider: str,
        model: str,
        reservation_id: str,
        estimated_tokens: int = 500,
    ) -> None:
        await cls._tracker.release_reservation(
            credential_id=credential_id,
            provider=provider,
            model=model,
            reservation_id=reservation_id,
            estimated_tokens=estimated_tokens,
        )

    @classmethod
    async def record_execution(
        cls,
        credential_id: str,
        tokens: int = 0,
        provider: str = "gemini",
        model: str = "gemini-1.5-flash",
    ) -> None:
        quota_def = cls.get_quota_definition(provider, model)
        key_base = f"quota:{provider.lower()}:{credential_id}"
        if model != "auto":
            key_base += f":{model.lower()}"
        mem = cls._tracker._get_memory_window(key_base)
        await mem.record_usage(tokens)
        try:
            await cls._tracker.reserve_quota(
                credential_id=credential_id,
                provider=provider,
                model=model,
                quota=quota_def,
                estimated_tokens=tokens,
                commit=True,
            )
        except Exception:
            pass

    @classmethod
    async def reconcile_from_headers(
        cls,
        credential_id: str,
        provider: str,
        model: str,
        headers: Any,
        status_code: int = 200,
        response_body: Optional[Any] = None,
    ) -> ParsedRateLimit:
        from token_router.core.rate_limits import RateLimitExtractor
        parsed = RateLimitExtractor.extract(
            provider=provider,
            headers=headers if isinstance(headers, dict) else dict(headers),
            status_code=status_code,
            response_body=response_body,
        )
        await cls._tracker.reconcile_upstream(
            credential_id=credential_id,
            provider=provider,
            model=model,
            rate_limit=parsed,
        )
        return parsed

    @classmethod
    async def get_credential_metrics(
        cls,
        credential_id: str,
        provider: str = "generic",
        model: str = "auto",
    ) -> Dict[str, Any]:
        return await cls._tracker.get_metrics(credential_id, provider, model)

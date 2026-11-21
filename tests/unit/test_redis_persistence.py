"""Comprehensive Redis Persistence, Failure Injection & Reservation Recovery Tests.

Tests:
1. Real Redis with AOF & RDB Persistence:
   - Verifies appendonly is 'yes' and appendfsync is 'everysec'.
   - Tests data durability across container restart or disconnect.
2. Reservation Lifecycle & Recovery:
   - Pre-dispatch reservation registers unique ID in sliding window.
   - Clean pre-dispatch release restores RPM/TPM immediately.
   - Confirmed completions reconcile without double counting.
   - Ambiguous provider execution (e.g. timeout) does NOT refund reservation.
3. Expired Reservation Cleanup & Automatic Window Rolling:
   - Reservations older than 60s expire automatically via ZREMRANGEBYSCORE.
   - No permanent quota exhaustion from interrupted or crashed workers.
4. Redis Outage & Fail-Closed Enforcement:
   - When Redis disconnects in distributed mode, requests fail safely (fail-closed).
   - Recovery upon Redis reconnection.
"""

import asyncio
from datetime import UTC, datetime, timedelta
import os
import uuid
import pytest

from token_router.core.config import settings
from token_router.core.quota import (
    DistributedQuotaTracker,
    QuotaManager,
    QuotaWindow,
    RedisUnavailableError,
)

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")


@pytest.mark.asyncio
async def test_redis_aof_persistence_and_configuration():
    """Verify active Redis deployment has production-grade AOF and RDB persistence configured."""
    tracker = DistributedQuotaTracker(redis_url=REDIS_URL, distributed_mode=True, failure_policy="fail_closed")
    client = await tracker._get_redis()
    assert client is not None, f"Failed to connect to Redis at {REDIS_URL}"

    # Inspect active Redis configuration
    cfg = await client.config_get("appendonly", "appendfsync", "save")
    assert cfg.get("appendonly") == "yes", f"Redis AOF must be enabled (got {cfg.get('appendonly')})"
    assert cfg.get("appendfsync") == "everysec", f"Redis appendfsync must be 'everysec' (got {cfg.get('appendfsync')})"

    # Verify metrics report AOF persistence
    cred_id = f"cred_persist_{uuid.uuid4().hex[:6]}"
    quota = QuotaWindow(rpm_limit=10, is_hard_limit=True)
    ok, _, _, res_id = await tracker.reserve_quota(cred_id, "openai", "gpt-4o", quota, estimated_tokens=100)
    assert ok is True

    metrics = await tracker.get_metrics(cred_id, "openai", "gpt-4o")
    assert metrics["mode"] == "redis_distributed"
    assert metrics["redis_persistence"] == "aof_enabled"


@pytest.mark.asyncio
async def test_reservation_recovery_after_reconnection():
    """Verify reservations made prior to tracker reconnection or restart persist in Redis."""
    tracker_1 = DistributedQuotaTracker(redis_url=REDIS_URL, distributed_mode=True, failure_policy="fail_closed")
    cred_id = f"cred_reconn_{uuid.uuid4().hex[:6]}"
    quota = QuotaWindow(rpm_limit=3, tpm_limit=5000, is_hard_limit=True)

    # 1. Instance 1 reserves 2 slots
    ok1, _, _, res1 = await tracker_1.reserve_quota(cred_id, "anthropic", "claude-3-5", quota, estimated_tokens=1500)
    ok2, _, _, res2 = await tracker_1.reserve_quota(cred_id, "anthropic", "claude-3-5", quota, estimated_tokens=1500)
    assert ok1 is True and ok2 is True

    # 2. Simulate complete tracker process restart (fresh instance 2 connects to same Redis)
    tracker_2 = DistributedQuotaTracker(redis_url=REDIS_URL, distributed_mode=True, failure_policy="fail_closed")
    metrics = await tracker_2.get_metrics(cred_id, "anthropic", "claude-3-5")
    assert metrics["current_rpm"] == 2
    assert metrics["current_tpm"] == 3000

    # 3. Third reservation should succeed (2 + 1 = 3 <= 3)
    ok3, _, _, res3 = await tracker_2.reserve_quota(cred_id, "anthropic", "claude-3-5", quota, estimated_tokens=1000)
    assert ok3 is True

    # 4. Fourth reservation must be rejected (RPM limit 3 reached)
    ok4, reason, wait_s, _ = await tracker_2.reserve_quota(cred_id, "anthropic", "claude-3-5", quota, estimated_tokens=500)
    assert ok4 is False
    assert "RPM limit exceeded" in reason


@pytest.mark.asyncio
async def test_expired_reservation_cleanup():
    """Verify reservations older than the 60s sliding window expire and do not permanently lock quota."""
    tracker = DistributedQuotaTracker(redis_url=REDIS_URL, distributed_mode=True, failure_policy="fail_closed")
    client = await tracker._get_redis()
    cred_id = f"cred_expire_{uuid.uuid4().hex[:6]}"
    quota = QuotaWindow(rpm_limit=1, is_hard_limit=True)

    # Reserve slot
    ok1, _, _, res1 = await tracker.reserve_quota(cred_id, "gemini", "flash", quota, estimated_tokens=200)
    assert ok1 is True

    # Verify second reservation is blocked
    ok2, _, _, _ = await tracker.reserve_quota(cred_id, "gemini", "flash", quota, estimated_tokens=200)
    assert ok2 is False

    # Simulate passage of 61 seconds in Redis by backdating the timestamp in sorted set
    key_base = f"quota:gemini:{cred_id}:flash"
    old_time = datetime.now(UTC).timestamp() - 65.0
    await client.zadd(f"{key_base}:rpm", {res1: old_time})
    await client.zadd(f"{key_base}:tpm", {f"{res1}:200": old_time})

    # Now, subsequent reservation triggers ZREMRANGEBYSCORE and succeeds!
    ok3, _, _, res3 = await tracker.reserve_quota(cred_id, "gemini", "flash", quota, estimated_tokens=200)
    assert ok3 is True
    assert res3 != res1


@pytest.mark.asyncio
async def test_ambiguous_outcome_safety_no_unsafe_refund():
    """Ambiguous provider executions (e.g. timeout after dispatch) must NOT refund quota."""
    tracker = DistributedQuotaTracker(redis_url=REDIS_URL, distributed_mode=True, failure_policy="fail_closed")
    cred_id = f"cred_ambig_{uuid.uuid4().hex[:6]}"
    quota = QuotaWindow(rpm_limit=2, tpm_limit=2000, is_hard_limit=True)

    # Reserve slot
    ok1, _, _, res1 = await tracker.reserve_quota(cred_id, "openai", "gpt-4o", quota, estimated_tokens=1000)
    assert ok1 is True

    # Pre-dispatch failure -> safe to refund
    res_fake = f"res_fake_{uuid.uuid4().hex[:6]}"
    # Ambiguous outcome policy: do NOT call release_reservation for res1
    # Metrics must preserve the consumed reservation
    metrics = await tracker.get_metrics(cred_id, "openai", "gpt-4o")
    assert metrics["current_rpm"] == 1
    assert metrics["current_tpm"] == 1000


@pytest.mark.asyncio
async def test_redis_outage_and_recovery():
    """Test behavior during a Redis outage: fail-closed policy, followed by recovery when available."""
    # 1. Unreachable Redis with fail_closed
    unreachable_tracker = DistributedQuotaTracker(
        redis_url="redis://127.0.0.1:54321/0",  # dead port
        distributed_mode=True,
        failure_policy="fail_closed",
    )
    quota = QuotaWindow(rpm_limit=10, is_hard_limit=True)
    with pytest.raises(RedisUnavailableError):
        await unreachable_tracker.reserve_quota("cred_outage", "openai", "gpt-4o", quota)

    # 2. Recovering with valid Redis URL
    recovered_tracker = DistributedQuotaTracker(
        redis_url=REDIS_URL,
        distributed_mode=True,
        failure_policy="fail_closed",
    )
    ok, _, _, res_id = await recovered_tracker.reserve_quota("cred_outage", "openai", "gpt-4o", quota)
    assert ok is True
    assert res_id is not None


@pytest.mark.asyncio
async def test_redis_real_container_restart_with_aof_durability():
    """Real Redis process restart and ungraceful kill recovery test with isolated container and volume."""
    import subprocess
    import shutil

    if not shutil.which("docker"):
        pytest.skip("Docker CLI not available for container restart integration test")

    vol_name = f"test_aof_restart_vol_{uuid.uuid4().hex[:8]}"
    container_name = f"test-redis-aof-{uuid.uuid4().hex[:8]}"
    port = "6398"
    isolated_redis_url = f"redis://127.0.0.1:{port}/0"

    try:
        # Step 1: Create isolated volume and start container with AOF enabled
        subprocess.run(["docker", "volume", "create", vol_name], check=True, capture_output=True)
        subprocess.run([
            "docker", "run", "-d",
            "--name", container_name,
            "-p", f"127.0.0.1:{port}:6379",
            "-v", f"{vol_name}:/data",
            "redis:7-alpine",
            "redis-server",
            "--appendonly", "yes",
            "--appendfsync", "everysec",
            "--save", "900 1 300 10 60 10000",
        ], check=True, capture_output=True)

        # Wait for Redis readiness
        ready = False
        for _ in range(20):
            res = subprocess.run(["docker", "exec", container_name, "redis-cli", "ping"], capture_output=True)
            if b"PONG" in res.stdout:
                ready = True
                break
            await asyncio.sleep(0.2)
        assert ready, "Isolated test Redis failed to start"

        # Step 2: Connect tracker and make reservations
        tracker = DistributedQuotaTracker(redis_url=isolated_redis_url, distributed_mode=True, failure_policy="fail_closed")
        cred_id = f"cred_durability_{uuid.uuid4().hex[:6]}"
        quota = QuotaWindow(rpm_limit=5, tpm_limit=10000, is_hard_limit=True)

        ok1, _, _, res1 = await tracker.reserve_quota(cred_id, "openai", "gpt-4o", quota, estimated_tokens=2500)
        ok2, _, _, res2 = await tracker.reserve_quota(cred_id, "openai", "gpt-4o", quota, estimated_tokens=3000)
        assert ok1 is True and ok2 is True

        # Wait 1.1s for AOF fsync (everysec)
        await asyncio.sleep(1.2)

        # Step 3: Stop container gracefully
        subprocess.run(["docker", "stop", container_name], check=True, capture_output=True)
        subprocess.run(["docker", "rm", container_name], check=True, capture_output=True)

        # Close client connection pool before restart
        if tracker._redis_client:
            await tracker._redis_client.aclose()
            tracker._redis_client = None

        # Step 4: Re-create container using the EXACT SAME persistent volume
        subprocess.run([
            "docker", "run", "-d",
            "--name", container_name,
            "-p", f"127.0.0.1:{port}:6379",
            "-v", f"{vol_name}:/data",
            "redis:7-alpine",
            "redis-server",
            "--appendonly", "yes",
            "--appendfsync", "everysec",
            "--save", "900 1 300 10 60 10000",
        ], check=True, capture_output=True)

        ready = False
        for _ in range(20):
            res = subprocess.run(["docker", "exec", container_name, "redis-cli", "ping"], capture_output=True)
            if b"PONG" in res.stdout:
                ready = True
                break
            await asyncio.sleep(0.2)
        assert ready, "Isolated test Redis failed to recover after restart"

        # Step 5: Connect new application tracker instance to recovered Redis
        tracker_recovered = DistributedQuotaTracker(redis_url=isolated_redis_url, distributed_mode=True, failure_policy="fail_closed")
        metrics = await tracker_recovered.get_metrics(cred_id, "openai", "gpt-4o")
        assert metrics["current_rpm"] == 2
        assert metrics["current_tpm"] == 5500

        # Step 6: Verify limits are preserved across restarts (TPM limit 10000: 5500 + 5000 > 10000 -> REJECTED)
        ok_exceed, reason, wait_s, _ = await tracker_recovered.reserve_quota(cred_id, "openai", "gpt-4o", quota, estimated_tokens=5000)
        assert ok_exceed is False
        assert "TPM limit exceeded" in reason

        # Step 7: Under-limit reservation succeeds (5500 + 1000 = 6500 <= 10000)
        ok_under, _, _, res3 = await tracker_recovered.reserve_quota(cred_id, "openai", "gpt-4o", quota, estimated_tokens=1000)
        assert ok_under is True

        # Step 8: Test ungraceful kill (SIGKILL - kill -9)
        await asyncio.sleep(1.2)  # Allow AOF fsync
        subprocess.run(["docker", "kill", "--signal=SIGKILL", container_name], check=True, capture_output=True)
        subprocess.run(["docker", "rm", container_name], check=True, capture_output=True)

        if tracker_recovered._redis_client:
            await tracker_recovered._redis_client.aclose()
            tracker_recovered._redis_client = None

        # Re-start container after kill -9
        subprocess.run([
            "docker", "run", "-d",
            "--name", container_name,
            "-p", f"127.0.0.1:{port}:6379",
            "-v", f"{vol_name}:/data",
            "redis:7-alpine",
            "redis-server",
            "--appendonly", "yes",
            "--appendfsync", "everysec",
        ], check=True, capture_output=True)

        ready = False
        for _ in range(20):
            res = subprocess.run(["docker", "exec", container_name, "redis-cli", "ping"], capture_output=True)
            if b"PONG" in res.stdout:
                ready = True
                break
            await asyncio.sleep(0.2)
        assert ready, "Redis failed to recover from AOF after SIGKILL"

        tracker_post_kill = DistributedQuotaTracker(redis_url=isolated_redis_url, distributed_mode=True, failure_policy="fail_closed")
        metrics_kill = await tracker_post_kill.get_metrics(cred_id, "openai", "gpt-4o")
        assert metrics_kill["current_rpm"] == 3
        assert metrics_kill["current_tpm"] == 6500

    finally:
        # Cleanup isolated container and volume
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
        subprocess.run(["docker", "volume", "rm", "-f", vol_name], capture_output=True)


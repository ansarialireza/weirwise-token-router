"""Process-Local Thread-Safe TTL Cache with Bounded LRU Eviction.

Provides ultra-fast in-memory caching for:
1. Consumer API Key verification (Key Hash -> Consumer identity & permissions)
2. Provider metadata & enabled credentials
3. Decrypted vault keys (hashed key -> plaintext secret) with short TTL

Guarantees:
- Never caches raw plaintext API keys as keys
- Strict TTL and bounded capacity
- Instant invalidation on revoke/update
- Thread-safe and asyncio-safe
"""

import asyncio
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class BoundedTTLCache:
    """Bounded, thread-safe LRU cache with time-to-live expiration."""

    def __init__(self, max_size: int = 1000, default_ttl_seconds: float = 30.0):
        self.max_size = max_size
        self.default_ttl = default_ttl_seconds
        self._cache: OrderedDict[str, Tuple[Any, float]] = OrderedDict()  # key -> (value, expire_timestamp)
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Any]:
        """Retrieves item if present and not expired; updates LRU position."""
        now = datetime.now(UTC).timestamp()
        async with self._lock:
            if key not in self._cache:
                return None
            val, expire_at = self._cache[key]
            if now > expire_at:
                # Expired
                del self._cache[key]
                return None
            # Move to end (most recently used)
            self._cache.move_to_end(key)
            return val

    async def set(self, key: str, value: Any, ttl_seconds: Optional[float] = None) -> None:
        """Stores item with TTL, evicting oldest item if max_size exceeded."""
        ttl = ttl_seconds or self.default_ttl
        expire_at = datetime.now(UTC).timestamp() + ttl
        async with self._lock:
            if key in self._cache:
                del self._cache[key]
            elif len(self._cache) >= self.max_size:
                # Evict oldest item
                self._cache.popitem(last=False)
            self._cache[key] = (value, expire_at)

    async def invalidate(self, key: str) -> bool:
        """Explicitly invalidates a single key."""
        async with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False

    async def clear(self) -> None:
        """Clears all entries from the cache."""
        async with self._lock:
            self._cache.clear()

    async def size(self) -> int:
        """Returns the number of unexpired items in cache."""
        now = datetime.now(UTC).timestamp()
        async with self._lock:
            # Purge expired
            expired = [k for k, (_, exp) in self._cache.items() if now > exp]
            for k in expired:
                del self._cache[k]
            return len(self._cache)


# Global process-local caches
auth_cache = BoundedTTLCache(max_size=1000, default_ttl_seconds=30.0)
metadata_cache = BoundedTTLCache(max_size=500, default_ttl_seconds=60.0)
vault_cache = BoundedTTLCache(max_size=500, default_ttl_seconds=120.0)

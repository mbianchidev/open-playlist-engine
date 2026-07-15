"""Centralised provider/account rate limiting.

The duck review noted adapter-local backoff cannot coordinate across concurrent
jobs, so quota and account-flag protection must live centrally. This is a
token-bucket skeleton with an in-memory backend; production uses Valkey so the
limit is shared across worker processes.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    capacity: float
    refill_per_s: float
    tokens: float
    updated: float = field(default_factory=time.monotonic)


class RateLimiter:
    """Per-key token bucket. Key is typically ``f"{provider}:{account_id}"``."""

    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    def configure(self, key: str, *, capacity: float, refill_per_s: float) -> None:
        self._buckets[key] = _Bucket(capacity, refill_per_s, capacity)

    async def try_consume(
        self,
        key: str,
        *,
        capacity: float,
        refill_per_s: float,
        cost: float = 1.0,
    ) -> float | None:
        """Consume immediately, returning retry seconds instead of sleeping."""
        if capacity <= 0 or refill_per_s <= 0 or cost <= 0:
            raise ValueError("rate limit values must be positive")
        async with self._lock:
            bucket = self._buckets.get(key)
            if (
                bucket is None
                or bucket.capacity != capacity
                or bucket.refill_per_s != refill_per_s
            ):
                bucket = _Bucket(capacity, refill_per_s, capacity)
                self._buckets[key] = bucket
            now = time.monotonic()
            bucket.tokens = min(
                bucket.capacity, bucket.tokens + (now - bucket.updated) * bucket.refill_per_s
            )
            bucket.updated = now
            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return None
            return (cost - bucket.tokens) / bucket.refill_per_s

    async def clear(self) -> None:
        async with self._lock:
            self._buckets.clear()

    async def acquire(self, key: str, cost: float = 1.0) -> None:
        """Block until ``cost`` tokens are available for ``key``."""
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                # Unconfigured keys are unthrottled; adapters should configure on init.
                return
            while True:
                now = time.monotonic()
                bucket.tokens = min(
                    bucket.capacity, bucket.tokens + (now - bucket.updated) * bucket.refill_per_s
                )
                bucket.updated = now
                if bucket.tokens >= cost:
                    bucket.tokens -= cost
                    return
                deficit = cost - bucket.tokens
                await asyncio.sleep(deficit / bucket.refill_per_s)


rate_limiter = RateLimiter()

"""Sliding-window rate limiting.

Every policy shares the same five-minute window; only the allowance changes by
route class. The store is behind a Protocol so a Redis implementation can be
dropped in for multi-instance deployments without touching the limiter or the
middleware (DIP + OCP).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from api.core.config import settings


class RateLimitTier(str, Enum):
    """Route classes. Names match the RATE_LIMIT_* settings."""

    AUTH = "auth"
    SCAN = "scan"
    WRITE = "write"
    READ = "read"
    EXPORT = "export"


@dataclass(frozen=True)
class RateLimitPolicy:
    limit: int
    window_seconds: int

    @classmethod
    def for_tier(cls, tier: RateLimitTier) -> "RateLimitPolicy":
        limit = getattr(settings, f"RATE_LIMIT_{tier.name}")
        return cls(limit=limit, window_seconds=settings.RATE_LIMIT_WINDOW_SECONDS)


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_after: int

    @property
    def retry_after(self) -> int:
        return max(1, self.reset_after)


class RateLimitStore(Protocol):
    """Records one hit and reports whether the caller is still within budget."""

    def hit(self, key: str, policy: RateLimitPolicy) -> RateLimitResult: ...
    def reset(self) -> None: ...


class InMemorySlidingWindowStore:
    """Per-process sliding window.

    Holds the timestamps of recent hits per key. Idle keys are swept
    periodically, so a flood of one-off keys (the obvious way to attack a
    memory-backed limiter) cannot grow the map without bound.
    """

    #: Never track more distinct keys than this; beyond it, sweep aggressively.
    MAX_KEYS = 50_000
    SWEEP_INTERVAL_SECONDS = 60

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()

    def hit(self, key: str, policy: RateLimitPolicy) -> RateLimitResult:
        now = time.monotonic()
        cutoff = now - policy.window_seconds

        with self._lock:
            self._maybe_sweep(now, policy.window_seconds)

            timestamps = self._hits.setdefault(key, deque())
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()

            if len(timestamps) >= policy.limit:
                reset_after = int(timestamps[0] + policy.window_seconds - now) + 1
                return RateLimitResult(False, policy.limit, 0, reset_after)

            timestamps.append(now)
            remaining = policy.limit - len(timestamps)
            reset_after = int(timestamps[0] + policy.window_seconds - now) + 1
            return RateLimitResult(True, policy.limit, remaining, reset_after)

    def _maybe_sweep(self, now: float, window_seconds: int) -> None:
        """Drop keys with no hits left inside the window. Caller holds the lock."""
        due = now - self._last_sweep >= self.SWEEP_INTERVAL_SECONDS
        if not due and len(self._hits) < self.MAX_KEYS:
            return

        cutoff = now - window_seconds
        self._hits = {
            key: stamps
            for key, stamps in self._hits.items()
            if stamps and stamps[-1] > cutoff
        }
        self._last_sweep = now

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
            self._last_sweep = time.monotonic()


class RateLimiter:
    """Applies a tier's policy to a caller key."""

    def __init__(self, store: RateLimitStore):
        self._store = store

    def check(self, key: str, tier: RateLimitTier) -> RateLimitResult:
        policy = RateLimitPolicy.for_tier(tier)
        if not settings.RATE_LIMIT_ENABLED:
            return RateLimitResult(True, policy.limit, policy.limit, policy.window_seconds)
        return self._store.hit(f"{tier.value}:{key}", policy)

    def reset(self) -> None:
        self._store.reset()


#: One limiter per process, shared by the middleware and the per-route dependency.
limiter = RateLimiter(InMemorySlidingWindowStore())

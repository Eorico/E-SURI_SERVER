"""An in-process read cache, with invalidation on write.

The expensive part of a request is the round trip to the cluster, and a managed
cluster is somewhere else on the internet - tens of milliseconds, several times
per request. Most of those reads are for things that barely ever change: the
school, the subjects, the section list, the open term, and the caller's own
profile.

**Why in-process and not Redis.** Redis would be another service to pay for and
operate, which is the opposite of the reason this system moved off a managed
database. The cost of not having it is that each worker process keeps its own
cache, so an invalidation in one is not seen by the others - which is why every
entry also carries a short TTL. Staleness is therefore bounded by the TTL even
in the worst case, and `Cache` is a Protocol so a shared implementation can be
dropped in without touching a caller (DIP).

**What makes this safe.** Three rules, and all three are load-bearing:

1. *A write to a collection clears that collection's namespace.* Wired into
   `MongoRepository`, so it is not something a new repository has to remember.
   Caching is opt-in, invalidation is automatic - the safe way round.
2. *The key includes every argument.* A cached read is never shared between two
   callers who asked different questions, which is what would otherwise let one
   teacher see another's data.
3. *Nothing about a token is cached.* The signature, expiry and audience are
   verified on every single request. What is cached is the profile behind an
   already-verified token - which is why `is_active` and `role` changes have to
   invalidate it, and why its TTL is the shortest of any entry.

What is deliberately **not** cached: anything a teacher is actively editing
(submissions, responses, scores), because a stale score is worse than a slow
one.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Callable, Protocol, TypeVar

from api.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

#: Default lifetime for a cached read. Short on purpose: with one cache per
#: worker, this is the ceiling on how long a change can stay invisible to a
#: worker that did not make it.
DEFAULT_TTL_SECONDS = 60.0

#: The caller's own profile, read on every authenticated request. Shorter still,
#: because it carries `role` and `is_active` - deactivating somebody has to take
#: effect promptly even in a worker that never saw the write.
PRINCIPAL_TTL_SECONDS = 15.0

#: Entries held before the least recently used are dropped. A ceiling rather
#: than a target: without one, a cache keyed on per-record ids grows until the
#: process runs out of memory, which is a slower and far more confusing failure
#: than a cache miss.
MAX_ENTRIES = 5_000


class Cache(Protocol):
    def get(self, key: str) -> Any: ...
    def set(self, key: str, value: Any, ttl: float) -> None: ...
    def invalidate(self, namespace: str) -> int: ...
    def clear(self) -> None: ...


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    invalidations: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / total, 3) if total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "invalidations": self.invalidations,
            "hit_rate": self.hit_rate,
        }


#: Sentinel for "not in the cache", so that a cached `None` - a lookup that
#: legitimately found nothing - is a hit rather than being fetched every time.
MISSING = object()


@dataclass
class _Entry:
    value: Any
    expires_at: float
    #: Bumped on read, so eviction can drop the least recently used.
    touched_at: float = field(default_factory=time.monotonic)


class TTLCache:
    """Thread-safe, per-process, with a size ceiling and per-entry expiry.

    Keys are `namespace:rest`, and `invalidate(namespace)` drops every key
    under one. The namespace is a collection name, which is what lets a write
    clear exactly what it could have changed and nothing else.
    """

    def __init__(self, max_entries: int = MAX_ENTRIES):
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()
        self._max_entries = max_entries
        self.stats = CacheStats()

    def get(self, key: str) -> Any:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.stats.misses += 1
                return MISSING
            if entry.expires_at <= now:
                # Dropped on read rather than by a sweeper: an expired entry
                # costs nothing until somebody asks for it.
                del self._entries[key]
                self.stats.misses += 1
                return MISSING
            entry.touched_at = now
            self.stats.hits += 1
            return entry.value

    def set(self, key: str, value: Any, ttl: float = DEFAULT_TTL_SECONDS) -> None:
        now = time.monotonic()
        with self._lock:
            if len(self._entries) >= self._max_entries and key not in self._entries:
                self._evict_one(now)
            self._entries[key] = _Entry(value=value, expires_at=now + ttl, touched_at=now)

    def invalidate(self, namespace: str) -> int:
        """Drop every entry under a namespace. Returns how many went."""
        prefix = f"{namespace}:"
        with self._lock:
            doomed = [key for key in self._entries if key.startswith(prefix)]
            for key in doomed:
                del self._entries[key]
            if doomed:
                self.stats.invalidations += len(doomed)
        return len(doomed)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)

    def _evict_one(self, now: float) -> None:
        """Drop the least recently used entry. Caller holds the lock.

        Expired entries go first when there are any - they are free to remove
        and were going anyway.
        """
        expired = next(
            (key for key, entry in self._entries.items() if entry.expires_at <= now), None
        )
        victim = expired or min(self._entries, key=lambda key: self._entries[key].touched_at)
        del self._entries[victim]
        self.stats.evictions += 1


#: One cache per process, shared by every repository in it.
cache: TTLCache = TTLCache()


def cache_key(namespace: str, method: str, *parts: Any) -> str:
    """`namespace:method:arg:arg` - every argument included, always.

    Leaving an argument out of a key is how one caller gets served another
    caller's answer, so this takes them all and the callers do not choose.
    """
    rendered = ":".join("" if part is None else str(part) for part in parts)
    return f"{namespace}:{method}:{rendered}"


def cached(
    namespace: str | None = None, ttl: float = DEFAULT_TTL_SECONDS
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Cache a repository read, keyed on its arguments.

    `namespace` defaults to the repository's own collection, which is what ties
    the entry to the write that must clear it. Pass it explicitly only when a
    method reads a collection other than its own - and then pass the one a
    write would invalidate, not the one the method lives on.

    Only for reads that are safe to serve a few seconds late. A method whose
    result depends on anything outside its arguments must not use this: the key
    could not describe it, so two different questions would share one answer.
    """

    def decorate(method: Callable[..., T]) -> Callable[..., T]:
        @wraps(method)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> T:
            space = namespace or self.collection_name
            key = cache_key(
                space,
                method.__name__,
                *args,
                *(f"{name}={value}" for name, value in sorted(kwargs.items())),
            )

            found = cache.get(key)
            if found is not MISSING:
                return found

            value = method(self, *args, **kwargs)
            cache.set(key, value, ttl)
            return value

        wrapper.__cached_namespace__ = namespace  # type: ignore[attr-defined]
        return wrapper

    return decorate


def invalidate(*namespaces: str) -> None:
    """Drop everything cached for these collections.

    Called by `MongoRepository` on every write, so a repository author gets
    correct invalidation without doing anything. Over-invalidating is the right
    failure here: a needless cache miss costs one query, while a missed
    invalidation serves a wrong answer until the TTL runs out.
    """
    for namespace in namespaces:
        dropped = cache.invalidate(namespace)
        if dropped:
            logger.debug(
                "cache_invalidated", extra={"namespace": namespace, "entries": dropped}
            )


def stats() -> dict[str, Any]:
    """Counters for the health endpoint. Nothing here is sensitive."""
    return {**cache.stats.as_dict(), "entries": len(cache)}

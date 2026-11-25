"""Read-through cache with DB fallback, versioned keys, per-family TTL, and stampede protection.

`get_or_load(family, key_parts, ttl, loader)` is the one entry point callers use. It:
- builds a versioned, namespaced key (`{version}:{family}:{parts}`, CACHE-001);
- returns the cached JSON on a hit (recording the hit-rate metric, CACHE-004);
- on a miss, takes a short single-flight lock so only one worker recomputes (CACHE-003), runs
  `loader()` (the DB read), caches the result with the family TTL, and returns it;
- on ANY Redis error, logs once and falls back to calling `loader()` directly (CACHE-005) — a
  cache outage degrades to "always miss", never to a failed request.

`invalidate_company(ticker)` drops a company's cached families on `filing.ingested` /
`metrics.updated` events (CACHE-002).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

from app.config import Settings, get_settings
from app.observability.metrics import record_cache_access
from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)

# Per-family TTLs resolved from settings at call time (CACHE-001).
_TTL_ATTR = {
    "company": "cache_ttl_company_seconds",
    "prices": "cache_ttl_prices_seconds",
    "metrics": "cache_ttl_metrics_seconds",
    "news": "cache_ttl_news_seconds",
    "research": "cache_ttl_research_seconds",
}


def _key(settings: Settings, family: str, *parts: str) -> str:
    return ":".join([settings.cache_key_version, family, *parts])


class _RedisClient:
    """Lazily-created Redis connection, shared process-wide. Isolated so a connection failure at
    construction is caught by the same fallback path as an operation failure."""

    _client: Any = None

    @classmethod
    def get(cls, settings: Settings) -> Any:
        if cls._client is None:
            import redis

            cls._client = redis.from_url(settings.redis_url, decode_responses=True)
        return cls._client


def get_or_load(
    family: str,
    key_parts: list[str],
    *,
    loader: Callable[[], Any],
    ttl: int | None = None,
    settings: Settings | None = None,
) -> Any:
    """Read-through cache for a JSON-serializable value. `loader` is the DB read (CACHE-005)."""
    settings = settings or get_settings()
    if not settings.cache_enabled:
        return loader()

    key = _key(settings, family, *key_parts)
    ttl = ttl if ttl is not None else getattr(settings, _TTL_ATTR.get(family, ""), 300)
    try:
        client = _RedisClient.get(settings)
        cached = client.get(key)
        if cached is not None:
            record_cache_access(family=family, hit=True)
            return json.loads(cached)
        record_cache_access(family=family, hit=False)
        return _load_with_lock(client, key, ttl=ttl, loader=loader, settings=settings)
    except Exception as exc:  # CACHE-005: never fail a request because the cache is down.
        log_event(logger, logging.WARNING, "cache.fallback_to_db", family=family, error=str(exc))
        return loader()


def get_or_load_many(
    family: str,
    keys: list[list[str]],
    *,
    loader: Callable[[list[int]], list[Any]],
    ttl: int,
    settings: Settings | None = None,
) -> list[Any]:
    """`get_or_load` for a batch: one MGET, then `loader(indices of the misses)` once, so the
    misses still share one call (a query-embedding batch, ADR-0072). Same CACHE-005 rule: a Redis
    error reads as all misses and a failed write is ignored; the loader never runs twice."""
    settings = settings or get_settings()
    if not settings.cache_enabled:
        return loader(list(range(len(keys))))
    names = [_key(settings, family, *parts) for parts in keys]
    found: list[Any] = [None] * len(keys)
    client: Any = None
    try:
        client = _RedisClient.get(settings)
        for i, cached in enumerate(client.mget(names)):
            if cached is not None:
                found[i] = json.loads(cached)
    except Exception as exc:  # CACHE-005
        log_event(logger, logging.WARNING, "cache.fallback_to_db", family=family, error=str(exc))
        client = None
    missing = [i for i, value in enumerate(found) if value is None]
    for i in range(len(keys)):
        record_cache_access(family=family, hit=i not in missing)
    if missing:
        for i, value in zip(missing, loader(missing), strict=True):
            found[i] = value
        if client is not None:
            try:
                pipe = client.pipeline()
                for i in missing:
                    pipe.set(names[i], json.dumps(found[i], default=str), ex=ttl)
                pipe.execute()
            except Exception as exc:  # CACHE-005: the values are already in hand
                log_event(
                    logger, logging.WARNING, "cache.write_failed", family=family, error=str(exc)
                )
    return found


def _load_with_lock(
    client: Any,
    key: str,
    *,
    ttl: int,
    loader: Callable[[], Any],
    settings: Settings,
) -> Any:
    """Single-flight (CACHE-003): the lock winner recomputes + caches; losers briefly wait for the
    winner's write, then fall back to loading themselves rather than blocking indefinitely."""
    lock_key = f"{key}:lock"
    got_lock = client.set(lock_key, "1", nx=True, ex=settings.cache_singleflight_lock_seconds)
    if not got_lock:
        for _ in range(5):
            time.sleep(0.05)
            cached = client.get(key)
            if cached is not None:
                return json.loads(cached)
        return loader()  # winner is slow; don't block the request
    try:
        value = loader()
        client.set(key, json.dumps(value, default=str), ex=ttl)
        return value
    finally:
        client.delete(lock_key)


def invalidate_company(ticker: str, *, settings: Settings | None = None) -> None:
    """Drop a company's cached families (CACHE-002: on filing.ingested / metrics.updated)."""
    settings = settings or get_settings()
    if not settings.cache_enabled:
        return
    try:
        client = _RedisClient.get(settings)
        keys = [_key(settings, family, ticker.upper()) for family in _TTL_ATTR]
        client.delete(*keys)
    except Exception as exc:
        log_event(logger, logging.WARNING, "cache.invalidate_failed", ticker=ticker, error=str(exc))

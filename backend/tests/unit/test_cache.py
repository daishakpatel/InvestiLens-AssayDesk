"""Redis cache tests (§25.3, CACHE-001..005, ADR-0024). Offline — fakeredis + a raising client.

The headline guarantee is CACHE-005: a cache outage never fails a request. These prove the
loader (the DB read) always runs on a miss or an error, keys are versioned/namespaced, and
invalidation drops a company's families.
"""

from __future__ import annotations

from typing import Any

import fakeredis
import pytest

from app.cache import cache
from app.config import get_settings


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> fakeredis.FakeRedis:
    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(get_settings(), "cache_enabled", True)
    monkeypatch.setattr(cache._RedisClient, "get", classmethod(lambda cls, settings: client))
    return client


def test_disabled_cache_always_calls_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "cache_enabled", False)
    calls = {"n": 0}

    def loader() -> dict[str, int]:
        calls["n"] += 1
        return {"v": 1}

    assert cache.get_or_load("company", ["NVDA"], loader=loader) == {"v": 1}
    assert cache.get_or_load("company", ["NVDA"], loader=loader) == {"v": 1}
    assert calls["n"] == 2  # no caching when disabled


def test_miss_then_hit(enabled: fakeredis.FakeRedis) -> None:
    calls = {"n": 0}

    def loader() -> dict[str, str]:
        calls["n"] += 1
        return {"ticker": "NVDA"}

    first = cache.get_or_load("company", ["NVDA"], loader=loader)
    second = cache.get_or_load("company", ["NVDA"], loader=loader)
    assert first == second == {"ticker": "NVDA"}
    assert calls["n"] == 1  # second call served from cache


def test_key_is_versioned_and_namespaced(enabled: fakeredis.FakeRedis) -> None:
    cache.get_or_load("company", ["NVDA"], loader=lambda: {"x": 1})
    version = get_settings().cache_key_version
    assert enabled.get(f"{version}:company:NVDA") is not None


def test_invalidate_company_drops_families(enabled: fakeredis.FakeRedis) -> None:
    cache.get_or_load("company", ["NVDA"], loader=lambda: {"x": 1})
    version = get_settings().cache_key_version
    assert enabled.get(f"{version}:company:NVDA") is not None
    cache.invalidate_company("NVDA")
    assert enabled.get(f"{version}:company:NVDA") is None


def test_redis_down_falls_back_to_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """CACHE-005: when Redis errors, the loader still runs and the request succeeds."""
    monkeypatch.setattr(get_settings(), "cache_enabled", True)

    class _BrokenClient:
        def get(self, *a: Any, **k: Any) -> Any:
            raise ConnectionError("redis down")

        def set(self, *a: Any, **k: Any) -> Any:
            raise ConnectionError("redis down")

    monkeypatch.setattr(
        cache._RedisClient, "get", classmethod(lambda cls, settings: _BrokenClient())
    )

    result = cache.get_or_load("company", ["NVDA"], loader=lambda: {"ok": True})
    assert result == {"ok": True}  # served from loader despite Redis being down


def test_many_loads_only_the_misses_in_one_call(enabled: fakeredis.FakeRedis) -> None:
    """ADR-0072: a batch reads every key at once and loads only the misses, together."""
    calls: list[list[int]] = []

    def loader(indices: list[int]) -> list[Any]:
        calls.append(indices)
        return [[i * 1.5] for i in indices]

    keys = [["m", "a"], ["m", "b"], ["m", "c"]]
    assert cache.get_or_load_many("query_embedding", keys[:1], loader=loader, ttl=60) == [[0.0]]
    assert cache.get_or_load_many("query_embedding", keys, loader=loader, ttl=60) == [
        [0.0],  # cached by the first call, so not reloaded
        [1.5],
        [3.0],
    ]
    assert calls == [[0], [1, 2]]
    assert cache.get_or_load_many("query_embedding", keys, loader=loader, ttl=60)[2] == [3.0]
    assert len(calls) == 2  # all hits now


def test_many_with_redis_down_loads_everything_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """CACHE-005 for batches: a read error means all misses; the loader still runs only once."""
    monkeypatch.setattr(get_settings(), "cache_enabled", True)

    class _Broken:
        def mget(self, *a: Any, **k: Any) -> Any:
            raise ConnectionError("redis down")

    monkeypatch.setattr(cache._RedisClient, "get", classmethod(lambda cls, settings: _Broken()))
    calls: list[list[int]] = []

    def loader(indices: list[int]) -> list[Any]:
        calls.append(indices)
        return [{"i": i} for i in indices]

    result = cache.get_or_load_many("query_embedding", [["a"], ["b"]], loader=loader, ttl=60)
    assert result == [{"i": 0}, {"i": 1}] and calls == [[0, 1]]

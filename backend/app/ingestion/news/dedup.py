"""News deduplication & event clustering (Phase 1e, spec §10.8 / FR-025).

Two layers: an exact content-hash match (same headline from a syndication), and near-duplicate
clustering via embedding cosine similarity within a time window (the same story from multiple
outlets). Items in one cluster share an `event_cluster_id` ("N sources"). Similarity is a
ranking signal, not a financial number, so float math is fine here.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

_WHITESPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")


def content_hash(title: str) -> str:
    """Stable hash of a normalized headline (exact-duplicate detection)."""
    normalized = _WHITESPACE.sub(" ", _PUNCT.sub("", title.lower())).strip()
    return hashlib.sha256(normalized.encode()).hexdigest()


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


@dataclass(frozen=True)
class ClusterInput:
    content_hash: str
    published_at: datetime


class _UnionFind:
    def __init__(self, n: int) -> None:
        self._parent = list(range(n))

    def find(self, i: int) -> int:
        while self._parent[i] != i:
            self._parent[i] = self._parent[self._parent[i]]
            i = self._parent[i]
        return i

    def union(self, i: int, j: int) -> None:
        self._parent[self.find(i)] = self.find(j)


def cluster_articles(
    items: list[ClusterInput],
    embeddings: Sequence[Sequence[float]] | None,
    *,
    cosine_threshold: float,
    window_days: int,
) -> list[str]:
    """Return an `event_cluster_id` per item (same id == same event).

    With no `embeddings` (the embedding call failed) only identical headlines are grouped.
    """
    n = len(items)
    uf = _UnionFind(n)
    window = timedelta(days=window_days)
    for i in range(n):
        for j in range(i + 1, n):
            same_hash = items[i].content_hash == items[j].content_hash
            within_window = abs(items[i].published_at - items[j].published_at) <= window
            if same_hash or (
                embeddings is not None
                and within_window
                and _cosine(embeddings[i], embeddings[j]) >= cosine_threshold
            ):
                uf.union(i, j)
    # Name each cluster by the content hash of its earliest-published member (stable).
    leader_date: dict[int, datetime] = {}
    leader_hash: dict[int, str] = {}
    for idx, item in enumerate(items):
        root = uf.find(idx)
        if root not in leader_date or item.published_at < leader_date[root]:
            leader_date[root] = item.published_at
            leader_hash[root] = item.content_hash
    return [f"evt_{leader_hash[uf.find(idx)][:12]}" for idx in range(n)]

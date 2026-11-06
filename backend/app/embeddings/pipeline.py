"""Embedding generation pipeline (Phase 2b, EMB-001/002).

Reads chunks that lack an embedding under the active model, embeds them in batches, and writes
the vectors back with their `embedding_model` + `embedding_dim`. Every batch is logged to
`llm_calls` with token count, cost, and latency (reusing the project-wide cost table so embedding
spend is tracked alongside chat spend). Vectors are embedded via the async `EmbeddingClient`;
Voyage normalizes to unit length (cosine == the HNSW index metric, ADR-0010/0013).

No retrieval, fusion, or reranking here — that is Phase 2c.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from app.config import get_settings
from app.embeddings.tokens import batch_tokens
from app.providers import get_embedding_client
from app.providers.base import EmbeddingClient
from app.repositories import embeddings as embed_repo
from app.repositories import llm_calls as llm_repo
from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)
EMBED_PURPOSE = "embedding"


@dataclass
class EmbedCounts:
    embedded: int = 0
    batches: int = 0
    tokens: int = 0
    cost_usd: Decimal = Decimal("0")


def _cost(tokens: int) -> Decimal:
    per_million = get_settings().embedding_cost_per_1m_tokens
    return (Decimal(tokens) / Decimal(1_000_000) * per_million).quantize(Decimal("0.000001"))


def _resolve_tokens(client: EmbeddingClient, texts: list[str]) -> int:
    """Prefer provider-reported usage (Voyage); fall back to the project estimator (mock)."""
    reported = getattr(client, "last_total_tokens", None)
    return int(reported) if reported else batch_tokens(texts)


def _embed_sync(client: EmbeddingClient, texts: list[str], *, input_type: str) -> list[list[float]]:
    return [list(v) for v in asyncio.run(client.embed(texts, input_type=input_type))]


def embed_pending(
    session: Session,
    *,
    client: EmbeddingClient | None = None,
    batch_size: int | None = None,
    max_batches: int | None = None,
) -> EmbedCounts:
    """Embed every chunk missing a vector for the active model; log cost per batch (EMB-002).

    `max_batches` stops early so a caller can commit between batches: a provider quota error
    part-way through then costs one batch, not the whole run.
    """
    client = client or get_embedding_client()
    batch_size = batch_size or get_settings().embedding_batch_size
    model, dim = client.model_name, client.dimension
    include_exhibits = get_settings().embed_exhibits  # FR-EX-013 cost guard
    counts = EmbedCounts()

    while max_batches is None or counts.batches < max_batches:
        batch = embed_repo.fetch_pending_batch(
            session, model=model, limit=batch_size, include_exhibits=include_exhibits
        )
        if not batch:
            break
        ids = [cid for cid, _ in batch]
        texts = [txt for _, txt in batch]

        started = time.monotonic()
        vectors = _embed_sync(client, texts, input_type="document")
        latency_ms = int((time.monotonic() - started) * 1000)

        tokens = _resolve_tokens(client, texts)
        cost = _cost(tokens)
        embed_repo.update_embeddings(
            session, list(zip(ids, vectors, strict=True)), model=model, dim=dim
        )
        llm_repo.record_call(
            session,
            purpose=EMBED_PURPOSE,
            model=model,
            input_tokens=tokens,
            latency_ms=latency_ms,
            cost_usd=cost,
            status="success",
        )
        counts.embedded += len(batch)
        counts.batches += 1
        counts.tokens += tokens
        counts.cost_usd += cost
        log_event(
            logger,
            logging.INFO,
            "embed.batch",
            model=model,
            size=len(batch),
            tokens=tokens,
            latency_ms=latency_ms,
        )
    return counts


def embed_all(
    session: Session, *, client: EmbeddingClient | None = None, batch_size: int | None = None
) -> EmbedCounts:
    """`embed_pending` for a whole backlog, committing each batch as it is paid for.

    One transaction for the backlog meant a provider error part-way rolled back every vector
    already bought, and the retry bought them again. Commits everything pending in `session` too.
    """
    client = client or get_embedding_client()  # one client, so its rate limiter paces every batch
    total = EmbedCounts()
    while True:
        counts = embed_pending(session, client=client, batch_size=batch_size, max_batches=1)
        session.commit()
        if not counts.embedded:
            return total
        total.embedded += counts.embedded
        total.batches += counts.batches
        total.tokens += counts.tokens
        total.cost_usd += counts.cost_usd

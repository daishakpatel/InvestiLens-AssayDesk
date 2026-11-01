"""Single-index search primitives (Phase 2b, RAG-010).

This module exposes the two retrieval indexes *individually* — semantic (pgvector/HNSW cosine)
and keyword (PostgreSQL full-text / GIN) — so each can be verified on its own. Fusion, reranking,
query rewriting, and orchestration are Phase 2c and deliberately not here.

- Semantic search sets `hnsw.ef_search` per query (EMB-004): higher = better recall, slower.
- Keyword search uses `websearch_to_tsquery('english', …)` so stemming/synonyms let exact
  financial terms ("gross margin", "restricted cash") match differently-phrased queries. When no
  chunk holds every word, it widens to any of the words.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, Float, Text, cast, func, select, text
from sqlalchemy.dialects.postgresql import TSQUERY
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import DocumentChunk
from app.providers import get_embedding_client
from app.providers.base import EmbeddingClient
from app.rag.types import SearchScope


@dataclass
class SearchHit:
    chunk_id: int
    score: float  # cosine similarity (vector) or ts_rank_cd (keyword); non-financial, float is fine
    text: str
    section: str | None


def _scope_filters(scope: SearchScope | None) -> list[ColumnElement[bool]]:
    if scope is None:
        return []
    out: list[ColumnElement[bool]] = []
    if scope.section_keys:
        out.append(DocumentChunk.chunk_metadata["section_key"].astext.in_(scope.section_keys))
    if scope.filed_since is not None:
        out.append(DocumentChunk.filing_date >= scope.filed_since)
    return out


def _query_vector(query: str, client: EmbeddingClient) -> list[float]:
    vectors = asyncio.run(client.embed([query], input_type="query"))  # RAG-010: query input type
    return [float(x) for x in vectors[0]]


def vector_search(
    session: Session,
    *,
    query: str,
    company_id: int,
    limit: int = 40,
    client: EmbeddingClient | None = None,
    ef_search: int | None = None,
    scope: SearchScope | None = None,
) -> list[SearchHit]:
    """Semantic (cosine) search over the HNSW index, company-scoped (RAG-013)."""
    client = client or get_embedding_client()
    return vector_search_by_vector(
        session,
        vector=_query_vector(query, client),
        company_id=company_id,
        limit=limit,
        ef_search=ef_search,
        model=client.model_name,
        scope=scope,
    )


def vector_search_by_vector(
    session: Session,
    *,
    vector: Sequence[float],
    company_id: int,
    limit: int = 40,
    ef_search: int | None = None,
    model: str | None = None,
    scope: SearchScope | None = None,
) -> list[SearchHit]:
    """Cosine search for a pre-computed query vector (lets tests pin exact vectors offline).

    `model` limits the search to chunks embedded by that model: distances between two models'
    vectors mean nothing, so a provider switch must find no neighbours until chunks are re-embedded.
    """
    ef = int(ef_search or get_settings().hnsw_ef_search)  # int() guards the inlined literal
    # ef_search tunes HNSW recall at query time (EMB-004); LOCAL = scoped to this transaction.
    # SET takes no bind params, so the value is inlined — safe because it is a validated int.
    session.execute(text(f"SET LOCAL hnsw.ef_search = {ef}"))
    filters = _scope_filters(scope)
    if filters:
        # A filter applied after the index scan can leave fewer than `limit` rows; pgvector >= 0.8
        # keeps scanning until enough rows pass (ADR-0053).
        session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    distance = DocumentChunk.embedding.cosine_distance(list(vector))
    rows = session.execute(
        select(
            DocumentChunk.id, distance.label("distance"), DocumentChunk.text, DocumentChunk.section
        )
        .where(
            DocumentChunk.company_id == company_id,
            DocumentChunk.embedding.is_not(None),
            *([DocumentChunk.embedding_model == model] if model is not None else []),
            *filters,
        )
        .order_by(distance)
        .limit(limit)
    ).all()
    return [SearchHit(cid, 1.0 - float(dist), txt, section) for cid, dist, txt, section in rows]


def keyword_search(
    session: Session,
    *,
    query: str,
    company_id: int,
    limit: int = 40,
    scope: SearchScope | None = None,
) -> list[SearchHit]:
    """Full-text keyword search over the GIN-indexed `tsv` column, company-scoped (RAG-010).

    Every word must match first. A full-sentence question rarely passes that, so when it finds
    nothing the search widens to any of the words, best coverage first.
    """
    strict = func.websearch_to_tsquery("english", query)
    # plainto_tsquery joins the stemmed words with `&`; swapping in `|` turns it into "any word".
    any_word = cast(
        func.replace(cast(func.plainto_tsquery("english", query), Text), "&", "|"), TSQUERY
    )
    filters = _scope_filters(scope)
    rows: Sequence[Any] = []
    for tsquery in (strict, any_word):
        rank = func.ts_rank_cd(DocumentChunk.tsv, tsquery, type_=Float)
        rows = session.execute(
            select(DocumentChunk.id, rank.label("rank"), DocumentChunk.text, DocumentChunk.section)
            .where(
                DocumentChunk.company_id == company_id,
                DocumentChunk.tsv.op("@@")(tsquery),
                *filters,
            )
            .order_by(rank.desc())
            .limit(limit)
        ).all()
        if rows:
            break
    return [SearchHit(cid, float(r), txt, section) for cid, r, txt, section in rows]

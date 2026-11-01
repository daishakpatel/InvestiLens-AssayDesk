"""Document retrieval orchestration (RAG-010…018).

Pipeline for the document side: run vector + keyword search (over the rewritten query and its
sub-queries), fuse with RRF, deduplicate boilerplate, rerank, apply diversity + time-aware
selection, expand parent-child context, and judge sufficiency. Structured-metric routing is in
`app.rag.pipeline`; this module never touches money (it ranks text).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.ingestion.documents.tokens import estimate_tokens
from app.rag.evidence import build_evidence
from app.rag.fusion import reciprocal_rank_fusion
from app.rag.rerank import get_reranker
from app.rag.search import keyword_search, vector_search
from app.rag.types import Evidence, RewrittenQuery, SearchScope
from app.repositories import chunks as chunk_repo
from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)


@dataclass
class RetrievalOutcome:
    evidence: list[Evidence]
    sufficient: bool
    top_score: float


def _year(e: Evidence) -> int | None:
    if e.period_end:
        return e.period_end.year
    return e.filing_date.year if e.filing_date else None


def _dedup_boilerplate(evidence: list[Evidence]) -> list[Evidence]:
    """Keep the latest of each near-dup cluster; annotate 'unchanged since' (RAG-016)."""
    by_hash: dict[str, list[Evidence]] = {}
    passthrough: list[Evidence] = []
    for e in evidence:
        if e.dedup_hash:
            by_hash.setdefault(e.dedup_hash, []).append(e)
        else:
            passthrough.append(e)

    kept: list[Evidence] = list(passthrough)
    for cluster in by_hash.values():
        latest = max(cluster, key=lambda e: e.filing_date or date.min)
        years = [y for y in (_year(c) for c in cluster) if y is not None]
        if len(cluster) > 1 and years:
            latest.unchanged_since = min(years)
        kept.append(latest)
    # Preserve the original (RRF) ordering among the kept representatives.
    order = {id(e): i for i, e in enumerate(evidence)}
    return sorted(kept, key=lambda e: order[id(e)])


def _apply_recency(evidence: list[Evidence]) -> list[Evidence]:
    """Bounded boost for chunks from the most recent filing (RAG-014 'latest' questions)."""
    dated = [e.filing_date for e in evidence if e.filing_date]
    if not dated:
        return evidence
    newest = max(dated)
    for e in evidence:
        if e.filing_date == newest:
            e.scores["rerank"] = min(1.0, e.scores.get("rerank", 0.0) * 1.1)
    return sorted(evidence, key=lambda e: e.scores.get("rerank", 0.0), reverse=True)


def _select_diverse(
    evidence: list[Evidence], *, max_per_doc: int, top_k: int, stratify_years: Sequence[int]
) -> list[Evidence]:
    """Cap per-document dominance (RAG-017) and guarantee one chunk per requested year (RAG-014)."""
    per_doc: dict[int, int] = {}
    chosen_ids: set[int] = set()
    chosen: list[Evidence] = []

    def take(e: Evidence) -> None:
        chosen.append(e)
        chosen_ids.add(e.chunk_id)
        per_doc[e.document_id] = per_doc.get(e.document_id, 0) + 1

    # Period stratification first: best-ranked chunk for each requested fiscal year.
    for yr in stratify_years:
        for e in evidence:
            if (
                e.chunk_id not in chosen_ids
                and _year(e) == yr
                and per_doc.get(e.document_id, 0) < max_per_doc
            ):
                take(e)
                break

    effective_k = max(top_k, len(chosen))  # never drop a guaranteed per-year pick
    for e in evidence:
        if len(chosen) >= effective_k:
            break
        if e.chunk_id in chosen_ids or per_doc.get(e.document_id, 0) >= max_per_doc:
            continue
        take(e)
    # The cap spreads evidence across filings; it must not starve a company with one filing.
    for e in evidence:
        if len(chosen) >= effective_k:
            break
        if e.chunk_id not in chosen_ids:
            take(e)
    return chosen


def _expand_parents(
    session: Session, evidence: list[Evidence], *, per_item_token_budget: int
) -> None:
    """Attach neighbor/parent-section text for context (RAG-015); citation stays the child span."""
    for e in evidence:
        if not e.parent_section_id:
            continue
        siblings = chunk_repo.get_section_siblings(
            session,
            document_id=e.document_id,
            parent_section_id=e.parent_section_id,
            exclude_chunk_id=e.chunk_id,
        )
        context: list[str] = []
        used = 0
        for sib in siblings:
            cost = estimate_tokens(sib.text)
            if used + cost > per_item_token_budget:
                continue
            context.append(sib.text)
            used += cost
        if context:
            e.expanded_context = "\n\n".join(context)


def retrieve_evidence(
    session: Session,
    *,
    company_id: int,
    rewritten: RewrittenQuery,
    settings: Settings | None = None,
    scope: SearchScope | None = None,
) -> RetrievalOutcome:
    """Run the full document retrieval + ranking pipeline for one company (RAG-010…018)."""
    settings = settings or get_settings()
    top_n = settings.rag_candidate_top_n
    top_k = settings.rag_rerank_top_k

    queries = [rewritten.expanded, *rewritten.sub_queries]
    ranked_lists: list[list[int]] = []
    for q in queries:
        try:
            semantic = vector_search(
                session, query=q, company_id=company_id, limit=top_n, scope=scope
            )
        except Exception as exc:  # embedding provider down or over quota: keyword search carries on
            log_event(logger, logging.WARNING, "rag.vector_search.failed", error=str(exc))
            semantic = []
        ranked_lists.append([h.chunk_id for h in semantic])
        ranked_lists.append(
            [
                h.chunk_id
                for h in keyword_search(
                    session, query=q, company_id=company_id, limit=top_n, scope=scope
                )
            ]
        )

    fused = reciprocal_rank_fusion(ranked_lists, k=settings.rag_rrf_k)[:top_n]
    if not fused:
        return RetrievalOutcome(evidence=[], sufficient=False, top_score=0.0)

    chunks = chunk_repo.get_chunks_by_ids(session, [cid for cid, _ in fused])
    evidence = [
        build_evidence(chunks[cid], scores={"rrf": score}) for cid, score in fused if cid in chunks
    ]

    evidence = _dedup_boilerplate(evidence)
    reranked = get_reranker().rerank(rewritten.expanded, evidence)
    if rewritten.is_latest:
        reranked = _apply_recency(reranked)

    stratify = rewritten.fiscal_years if rewritten.is_over_time else []
    final = _select_diverse(
        reranked,
        max_per_doc=settings.rag_max_chunks_per_document,
        top_k=top_k,
        stratify_years=stratify,
    )
    _expand_parents(session, final, per_item_token_budget=settings.rag_context_token_budget // 6)

    top_score = max((e.scores.get("rerank", 0.0) for e in final), default=0.0)
    sufficient = top_score >= settings.rag_sufficiency_min_score
    return RetrievalOutcome(evidence=final, sufficient=sufficient, top_score=top_score)

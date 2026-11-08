"""News ingestion pipeline (Phase 1e).

Fetches company news, then for each article: categorizes it, scores relevance (entity
resolution), assigns a source tier, writes a traceable 2-sentence summary, and clusters
near-duplicates into one `event_cluster_id`. Persists a `documents` row (tier) + a `news` row
(headline-level only, LGL-004), and records `data_freshness`. Idempotent (ING-001) with
dead-letter isolation (ING-004/005).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.ingestion.news.classify import categorize, publisher_tier
from app.ingestion.news.dedup import ClusterInput, cluster_articles, content_hash
from app.ingestion.news.relevance import relevance_score
from app.ingestion.news.summarize import two_sentence_summary
from app.models import Company
from app.providers.news import NewsArticle, NewsSource, get_news_source
from app.repositories import news as news_repo
from app.repositories.filings import get_or_create_document
from app.repositories.freshness import record_freshness
from app.repositories.ingestion import dead_letter, finish_run, start_run
from app.utils.dates import months_ago
from app.utils.http import HttpClientError
from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)
NEWS_SOURCE = "news"
EmbedFn = Callable[[list[str]], list[list[float]]]


@dataclass
class NewsCounts:
    ingested: int = 0
    clusters: int = 0
    clustering: str = "embedded"


def _default_embed_fn() -> EmbedFn:
    """Embed titles with the configured EmbeddingClient (sync wrapper over the async client)."""
    from app.providers import get_embedding_client

    client = get_embedding_client()

    def embed(titles: list[str]) -> list[list[float]]:
        return [list(v) for v in asyncio.run(client.embed(titles, input_type="query"))]

    return embed


def ingest_news(
    session: Session,
    company: Company,
    *,
    source: NewsSource | None = None,
    embed_fn: EmbedFn | None = None,
    today: date | None = None,
) -> NewsCounts:
    """Ingest news for one company. Records an ingestion run; failures are isolated."""
    settings = get_settings()
    source = source or get_news_source()
    embed_fn = embed_fn or _default_embed_fn()
    today = today or datetime.now(UTC).date()
    counts = NewsCounts()
    run = start_run(session, source=NEWS_SOURCE, company_id=company.id)
    attempt_at = datetime.now(UTC)

    try:
        start = months_ago(today, max(1, settings.news_backfill_days // 30))
        articles = list(source.get_news(company.ticker, start=start, end=today))
        counts.clusters, skipped = _ingest_articles(session, company, articles, embed_fn, settings)
        counts.clustering = "skipped" if skipped else "embedded"
        counts.ingested = len(articles)
        record_freshness(
            session,
            company_id=company.id,
            source=NEWS_SOURCE,
            status="fresh",
            last_success_at=datetime.now(UTC),
            last_attempt_at=attempt_at,
        )
        finish_run(
            session,
            run,
            status="success",
            counts={
                "ingested": counts.ingested,
                "clusters": counts.clusters,
                "clustering": counts.clustering,  # "skipped" when the embedding call failed
            },
        )
    except Exception as exc:  # ING-005
        dead_letter(
            session,
            source=NEWS_SOURCE,
            payload_ref=company.ticker,
            error=f"{type(exc).__name__}: {exc}",
        )
        record_freshness(
            session,
            company_id=company.id,
            source=NEWS_SOURCE,
            status="failed",
            last_success_at=None,
            last_attempt_at=attempt_at,
            message=str(exc),
        )
        finish_run(session, run, status="failed", counts={}, error=str(exc))
        log_event(
            logger, logging.ERROR, "news.ingest.failed", ticker=company.ticker, error=str(exc)
        )
    return counts


def _ingest_articles(
    session: Session,
    company: Company,
    articles: list[NewsArticle],
    embed_fn: EmbedFn,
    settings: Settings,
) -> tuple[int, bool]:
    """Persist the articles; return (event clusters, whether near-duplicate grouping was off)."""
    if not articles:
        return 0, False
    hashes = [content_hash(a.title) for a in articles]
    embeddings: list[list[float]] | None
    try:
        embeddings = embed_fn([a.title for a in articles])
    except HttpClientError as exc:  # grouping is an extra: keep the news, group exact copies only
        embeddings = None
        log_event(
            logger,
            logging.WARNING,
            "news.cluster.skipped",
            ticker=company.ticker,
            articles=len(articles),
            error=str(exc),
        )
    cluster_ids = cluster_articles(
        [ClusterInput(h, a.published_at) for h, a in zip(hashes, articles, strict=True)],
        embeddings,
        cosine_threshold=settings.news_dup_cosine_threshold,
        window_days=settings.news_dup_window_days,
    )
    for article, chash, cluster_id in zip(articles, hashes, cluster_ids, strict=True):
        tier = publisher_tier(article.publisher)
        document_id = get_or_create_document(
            session,
            company_id=company.id,
            document_type="news",
            source_tier=tier,
            content_hash=chash,
            title=article.title,
            source_url=article.url,
            parser_version="news-ingest-v1",
            doc_metadata={"publisher": article.publisher, "external_id": article.external_id},
        )
        news_repo.upsert_news(
            session,
            company_id=company.id,
            document_id=document_id,
            content_hash=chash,
            title=article.title,
            description=two_sentence_summary(article.title, article.summary),
            url=article.url,
            publisher=article.publisher,
            published_at=article.published_at,
            category=categorize(article.title, article.summary, article.provider_category),
            relevance_score=relevance_score(
                company.name, company.ticker, article.title, article.summary
            ),
            event_cluster_id=cluster_id,
        )
    return len(set(cluster_ids)), embeddings is None


def ingest_news_for_tickers(
    session_factory: Callable[[], Session],
    tickers: list[str],
    *,
    source: NewsSource | None = None,
) -> dict[str, NewsCounts]:
    """Ingest several companies; one failure never aborts the batch (ING-005)."""
    results: dict[str, NewsCounts] = {}
    for ticker in tickers:
        with session_factory() as session:
            company = session.scalar(select(Company).where(Company.ticker == ticker.upper()))
            if company is None:
                log_event(logger, logging.ERROR, "news.company.missing", ticker=ticker)
                continue
            results[ticker] = ingest_news(session, company, source=source)
            session.commit()
    return results

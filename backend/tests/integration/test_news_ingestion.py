"""News ingestion integration tests (Phase 1e DoD). Stub source + test DB. Skips offline."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.ingestion.news import ingest_news
from app.ingestion.sec import ingest_company
from app.models import Company, Document, IngestionRun, News
from app.providers.news.base import NewsArticle, NewsSource
from app.providers.sec.mock import MockSecSource
from app.rag import tools
from app.utils.http import HttpClientError
from app.utils.storage import FilesystemObjectStorage

_NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)


class _StubNewsSource(NewsSource):
    """Deterministic articles: an exact dup, a near dup, a distinct story, a passing mention."""

    def get_news(
        self, ticker: str, *, start: date | None = None, end: date | None = None
    ) -> Sequence[NewsArticle]:
        return [
            NewsArticle(
                "1",
                "NVIDIA unveils Blackwell GPU",
                "New flagship AI chip.",
                "https://ex/1",
                "Reuters",
                _NOW,
                "company",
            ),
            NewsArticle(
                "2",
                "NVIDIA unveils Blackwell GPU",
                "Syndicated copy.",
                "https://ex/2",
                "Market Times",
                _NOW,
                "company",
            ),  # exact dup
            NewsArticle(
                "3",
                "Nvidia reveals Blackwell accelerator",
                "Same launch, other outlet.",
                "https://ex/3",
                "Bloomberg",
                _NOW,
                "company",
            ),  # near dup
            NewsArticle(
                "4",
                "Broad tech rally lifts indexes",
                "Markets rose on rate optimism.",
                "https://ex/4",
                "Daily Blog",
                _NOW,
                "general",
            ),  # passing mention
        ]


def _embed(titles: list[str]) -> list[list[float]]:
    # First three are the same event (near-identical vectors); the last is orthogonal.
    mapping = {
        "NVIDIA unveils Blackwell GPU": [1.0, 0.0],
        "Nvidia reveals Blackwell accelerator": [0.99, 0.02],
        "Broad tech rally lifts indexes": [0.0, 1.0],
    }
    return [mapping[t] for t in titles]


@pytest.fixture(scope="module")
def _migrated(test_db_url: str) -> None:
    from tests.integration.conftest import _MIGRATIONS_DIR

    cfg = Config()
    cfg.set_main_option("script_location", _MIGRATIONS_DIR)
    cfg.set_main_option("sqlalchemy.url", test_db_url)
    command.upgrade(cfg, "head")


@pytest.fixture
def session(_migrated: None, test_engine: Engine) -> Iterator[Session]:
    with Session(test_engine) as s:
        yield s
        s.rollback()


@pytest.fixture
def nvda(session: Session, tmp_path: Path) -> Company:
    ingest_company(session, "NVDA", sec=MockSecSource(), storage=FilesystemObjectStorage(tmp_path))
    return session.scalars(select(Company).where(Company.ticker == "NVDA")).one()


def _news(session: Session, company_id: int) -> list[News]:
    return list(session.scalars(select(News).where(News.company_id == company_id)))


def test_news_populates_with_required_fields_and_no_full_text(
    session: Session, nvda: Company
) -> None:
    ingest_news(session, nvda, source=_StubNewsSource(), embed_fn=_embed)
    rows = _news(session, nvda.id)
    assert len(rows) == 3  # the two identical-headline articles collapse to one row (exact dup)
    for row in rows:
        assert row.title and row.publisher and row.url and row.published_at
        assert row.category and row.relevance_score is not None
        assert row.content is None  # licensing: no full article text (LGL-004, ADR-0008)
    # The news documents carry a source tier (§15): Reuters/Bloomberg Tier 4, blog Tier 5.
    tiers = {
        d.source_tier
        for d in session.scalars(
            select(Document).where(Document.company_id == nvda.id, Document.document_type == "news")
        )
    }
    assert tiers == {4, 5}


def test_duplicates_collapse_into_one_event_cluster(session: Session, nvda: Company) -> None:
    ingest_news(session, nvda, source=_StubNewsSource(), embed_fn=_embed)
    rows = {r.title: r for r in _news(session, nvda.id)}
    # Exact dup + near dup share one cluster; the unrelated rally is a different cluster.
    launch_clusters = {
        rows["NVIDIA unveils Blackwell GPU"].event_cluster_id,
        rows["Nvidia reveals Blackwell accelerator"].event_cluster_id,
    }
    assert len(launch_clusters) == 1
    assert rows["Broad tech rally lifts indexes"].event_cluster_id not in launch_clusters


def test_passing_mention_scores_below_threshold(session: Session, nvda: Company) -> None:
    ingest_news(session, nvda, source=_StubNewsSource(), embed_fn=_embed)
    rows = {r.title: r for r in _news(session, nvda.id)}
    passing = rows["Broad tech rally lifts indexes"].relevance_score
    relevant = rows["NVIDIA unveils Blackwell GPU"].relevance_score
    assert passing is not None and passing < Decimal("0.3")  # DoD: below display threshold
    assert relevant is not None and relevant >= Decimal("0.3")


def test_chat_news_tool_leaves_out_passing_mentions(session: Session, nvda: Company) -> None:
    """RAG-031, §10.8: chat sees the same news as the News tab and reports, not market stories."""
    ingest_news(session, nvda, source=_StubNewsSource(), embed_fn=_embed)
    out = tools.get_recent_news(session, "NVDA", days=3650)  # the stub articles are dated 2026-09
    titles = {item["title"] for item in out["items"]}
    assert "NVIDIA unveils Blackwell GPU" in titles
    assert "Broad tech rally lifts indexes" not in titles
    assert len(out["source_ids"]) == len(out["items"])


def _refuse(titles: list[str]) -> list[list[float]]:
    raise HttpClientError("request failed after retries: POST .../batchEmbedContents")


def test_news_is_kept_when_the_embedding_call_fails(session: Session, nvda: Company) -> None:
    """ING-005: grouping near-duplicates is an extra; a refused embedding must not lose the news.

    18 of 84 runs were dead-lettered this way by 2026-10-30, all on embedding 429s and 402s.
    """
    counts = ingest_news(session, nvda, source=_StubNewsSource(), embed_fn=_refuse)

    rows = {r.title: r for r in _news(session, nvda.id)}
    assert counts.ingested == 4 and len(rows) == 3  # identical headlines still share one row
    # Without vectors only identical headlines group, so the reworded copy stands alone.
    assert (
        rows["Nvidia reveals Blackwell accelerator"].event_cluster_id
        != rows["NVIDIA unveils Blackwell GPU"].event_cluster_id
    )
    run = session.scalars(
        select(IngestionRun)
        .where(IngestionRun.source == "news", IngestionRun.company_id == nvda.id)
        .order_by(IngestionRun.id.desc())
    ).first()
    assert run is not None and run.status == "success"
    assert run.counts is not None and run.counts["clustering"] == "skipped"


def test_reingest_is_idempotent(session: Session, nvda: Company) -> None:
    ingest_news(session, nvda, source=_StubNewsSource(), embed_fn=_embed)
    first = session.scalar(select(func.count()).select_from(News).where(News.company_id == nvda.id))
    ingest_news(session, nvda, source=_StubNewsSource(), embed_fn=_embed)
    second = session.scalar(
        select(func.count()).select_from(News).where(News.company_id == nvda.id)
    )
    assert first == second == 3  # exact dup shares content_hash -> 3 distinct rows

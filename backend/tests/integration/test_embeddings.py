"""Embedding population, search, and re-embed integration tests (Phase 2b DoD).

Real Postgres + pgvector; skips offline. Uses the deterministic mock embedding client so the
suite stays network-free while exercising the actual HNSW/GIN indexes and the migration swap.

Refs: EMB-001 (model/dim per row), EMB-002 (cost log), EMB-003 (zero-downtime re-embed),
EMB-004 (HNSW cosine), RAG-010 (vector + keyword search).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Sequence

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.embeddings.pipeline import embed_all, embed_pending
from app.embeddings.reembed import migrate_model
from app.models import Company, Document, DocumentChunk, LlmCall
from app.providers.mocks.embeddings import MOCK_MODEL, MockEmbeddingClient
from app.rag.search import keyword_search, vector_search, vector_search_by_vector
from app.utils.http import HttpClientError

_CHUNK_TEXTS = [
    "Gross margin expanded in fiscal 2025 due to a richer mix of data-center products.",
    "Our supply chain depends on a limited number of foundry partners in Taiwan.",
    "Deferred revenue consists of customer payments received in advance of delivery.",
    "Restricted cash is pledged as collateral for certain lease obligations.",
    "Research and development expense increased as we invested in new architectures.",
]


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
def seeded(session: Session) -> Company:
    """A company with one document and a handful of un-embedded chunks."""
    company = Company(ticker="TST", cik="0000000001", name="Test Corp")
    session.add(company)
    session.flush()
    doc = Document(
        company_id=company.id, document_type="filing", source_tier=1, content_hash="hash-tst"
    )
    session.add(doc)
    session.flush()
    for i, body in enumerate(_CHUNK_TEXTS):
        session.add(
            DocumentChunk(
                document_id=doc.id,
                company_id=company.id,
                chunk_index=i,
                chunk_type="text",
                text=body,
                section="Item 7",
                parser_version="doc-parse-v1",
            )
        )
    session.flush()
    return company


def _chunks(session: Session, company_id: int) -> list[DocumentChunk]:
    return list(
        session.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.company_id == company_id)
            .order_by(DocumentChunk.chunk_index)
        )
    )


def test_embed_pending_fills_model_dim_and_logs_cost(session: Session, seeded: Company) -> None:
    # EMB-001 + EMB-002: every chunk gets a vector + model + dim; the batch is cost-logged.
    counts = embed_pending(session, client=MockEmbeddingClient(), batch_size=2)
    assert counts.embedded == len(_CHUNK_TEXTS)
    assert counts.batches == 3  # 2 + 2 + 1
    assert counts.cost_usd > 0 and counts.tokens > 0

    for chunk in _chunks(session, seeded.id):
        assert chunk.embedding is not None
        assert chunk.embedding_model == MOCK_MODEL
        assert chunk.embedding_dim == 1024

    logs = list(session.scalars(select(LlmCall).where(LlmCall.purpose == "embedding")))
    assert len(logs) == 3
    assert all(log.input_tokens and log.cost_usd is not None for log in logs)
    assert all(log.latency_ms is not None and log.status == "success" for log in logs)


def test_embed_pending_is_idempotent(session: Session, seeded: Company) -> None:
    embed_pending(session, client=MockEmbeddingClient())
    # Second pass: nothing pending for the same model, so no new work and no new cost rows.
    counts = embed_pending(session, client=MockEmbeddingClient())
    assert counts.embedded == 0 and counts.batches == 0
    assert session.scalar(select(func.count()).select_from(LlmCall)) == 1


def test_vector_search_orders_by_cosine(session: Session, seeded: Company) -> None:
    # EMB-004 / RAG-010 hand-checked ordering: querying a chunk's own vector ranks it first.
    client = MockEmbeddingClient()
    embed_pending(session, client=client)
    target = _CHUNK_TEXTS[1]  # the supply-chain chunk
    query_vec = [float(x) for x in asyncio.run(client.embed([target], input_type="query"))[0]]

    hits = vector_search_by_vector(session, vector=query_vec, company_id=seeded.id, limit=5)
    assert len(hits) == 5
    assert hits[0].text == target  # exact match is nearest
    assert hits[0].score == pytest.approx(1.0, abs=1e-6)  # cosine similarity ~1
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)  # descending similarity


def test_vector_search_ignores_vectors_from_another_model(
    session: Session, seeded: Company
) -> None:
    # EMB-003: after a provider switch the stored vectors and the query vector come from different
    # models, and comparing them would rank chunks at random. They must not match at all.
    embed_pending(session, client=MockEmbeddingClient())
    other = MockEmbeddingClient(model="some-other-model")
    assert vector_search(session, query="gross margin", company_id=seeded.id, client=other) == []
    same = MockEmbeddingClient()
    assert vector_search(session, query="gross margin", company_id=seeded.id, client=same)


def test_keyword_search_matches_despite_different_phrasing(
    session: Session, seeded: Company
) -> None:
    # RAG-010: GIN full-text finds the chunk even though the query says "margins" (plural) while
    # the chunk says "margin" — English stemming collapses them, so exact financial terms are
    # retrievable regardless of phrasing. A no-match term in the query would AND the result away,
    # so this specifically exercises stemming, not a literal substring.
    embed_pending(session, client=MockEmbeddingClient())
    hits = keyword_search(session, query="gross margins", company_id=seeded.id, limit=5)
    assert hits, "full-text search should find the gross-margin chunk"
    assert "Gross margin" in hits[0].text
    # ...and a term that appears in no chunk returns nothing (ANDed query), proving it is real FTS.
    assert keyword_search(session, query="cryptocurrency", company_id=seeded.id) == []


def test_embedding_can_stop_after_a_batch_and_resume(session: Session, seeded: Company) -> None:
    # EMB-002: a caller that commits between batches loses one batch, not the run, when a
    # provider quota is hit part-way; the next call picks up the chunks still pending.
    client = MockEmbeddingClient()
    first = embed_pending(session, client=client, batch_size=2, max_batches=1)
    assert (first.embedded, first.batches) == (2, 1)
    rest = embed_pending(session, client=client, batch_size=2)
    assert first.embedded + rest.embedded == len(_CHUNK_TEXTS)
    assert embed_pending(session, client=client).embedded == 0  # nothing left


class _FailsOnCall(MockEmbeddingClient):
    """Refuses the `n`th request, as Gemini did part-way through a backlog (429, then 402)."""

    def __init__(self, n: int) -> None:
        super().__init__()
        self._n = n
        self.calls = 0

    async def embed(
        self, texts: Sequence[str], *, input_type: str = "document"
    ) -> Sequence[Sequence[float]]:
        self.calls += 1
        if self.calls == self._n:
            raise HttpClientError("429 Too Many Requests")
        return await super().embed(texts, input_type=input_type)


@pytest.fixture
def committing_session(_migrated: None, test_engine: Engine) -> Iterator[Session]:
    """A session whose commits are savepoints inside one outer transaction undone at the end."""
    with test_engine.connect() as conn:
        outer = conn.begin()
        with Session(bind=conn, join_transaction_mode="create_savepoint") as s:
            yield s
        outer.rollback()


def test_embed_all_keeps_batches_paid_for_before_a_provider_error(
    committing_session: Session,
) -> None:
    """EMB-002, NFR-008: a refusal part-way loses one batch, not every batch before it.

    The Celery tasks embedded a backlog in one transaction, so one 429 rolled back all the paid
    vectors and their cost rows, and the retry paid for them again (2026-11-03).
    """
    session = committing_session
    company = Company(ticker="EMB", cik="0000000002", name="Embed Corp")
    session.add(company)
    session.flush()
    doc = Document(
        company_id=company.id, document_type="filing", source_tier=1, content_hash="hash-emb"
    )
    session.add(doc)
    session.flush()
    for i, body in enumerate(_CHUNK_TEXTS):
        session.add(
            DocumentChunk(
                document_id=doc.id,
                company_id=company.id,
                chunk_index=i,
                chunk_type="text",
                text=body,
                section="Item 7",
                parser_version="doc-parse-v1",
            )
        )
    session.commit()
    calls_logged = select(func.count()).select_from(LlmCall)
    before = session.scalar(calls_logged) or 0

    client = _FailsOnCall(2)
    with pytest.raises(HttpClientError):
        embed_all(session, client=client, batch_size=2)
    session.rollback()  # what the task's transaction does on the way out

    assert sum(c.embedding is not None for c in _chunks(session, company.id)) == 2
    assert (session.scalar(calls_logged) or 0) - before == 1  # the paid batch keeps its cost row

    rest = embed_all(session, client=MockEmbeddingClient(), batch_size=2)
    assert (rest.embedded, rest.batches) == (len(_CHUNK_TEXTS) - 2, 2)
    assert all(c.embedding is not None for c in _chunks(session, company.id))


def test_keyword_search_answers_a_full_sentence_question(session: Session, seeded: Company) -> None:
    # RAG-010: a question as a person types it carries words no single chunk contains ("what",
    # "main", "describes"). Requiring every word would return nothing, so when the strict match
    # is empty the search falls back to "any of the words", best coverage first.
    question = "What are the main supply chain risks the company describes?"
    hits = keyword_search(session, query=question, company_id=seeded.id, limit=5)
    assert hits, "a full-sentence question should still reach the supply-chain chunk"
    assert "supply chain" in hits[0].text
    # The fallback is still real full-text search: no shared word, no hit.
    assert keyword_search(session, query="which cryptocurrency", company_id=seeded.id) == []


def test_reembed_swap_migrates_without_data_loss(session: Session, seeded: Company) -> None:
    # EMB-003: old-model embeddings serve reads while the new model backfills, then atomic swap.
    embed_pending(session, client=MockEmbeddingClient())  # mock-embed-v1
    before = {c.id: list(c.embedding or []) for c in _chunks(session, seeded.id)}

    new_client = MockEmbeddingClient(model="mock-embed-v2")
    result = migrate_model(session, client=new_client, batch_size=2)
    assert result.staged == len(_CHUNK_TEXTS)
    assert result.swapped == len(_CHUNK_TEXTS)

    after = _chunks(session, seeded.id)
    assert len(after) == len(_CHUNK_TEXTS)  # no rows lost
    for chunk in after:
        assert chunk.embedding is not None  # no NULLs after swap
        assert chunk.embedding_model == "mock-embed-v2"
        assert chunk.embedding_dim == 1024
        assert chunk.embedding_new is None  # staging cleared
        assert list(chunk.embedding) != before[chunk.id]  # vectors actually changed

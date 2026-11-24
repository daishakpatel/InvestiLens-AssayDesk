"""Phase 6a company-comparison integration (§37.1): calendarization, percentiles, peers,
cited AI commentary, the compare_companies tool, and the endpoints.

Uses the mock SEC source (NVDA/AAPL/JPM) — three different fiscal-year ends (January / September /
December), which exercises calendarization fully offline. The NVDA/AMD/Intel DoD set is verified
against the live-ingested dev DB (see the task write-up); the logic proven here is identical.

Refs: §37.1, DR-021 (fiscal≠calendar), DR-040/041 (metric N/A), CIT-005 (verification), LGL-006.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from app.comparison.commentary import generate_commentary
from app.comparison.engine import compare_companies, gather
from app.comparison.peers import suggest_peers
from app.db import get_db
from app.finance.builder import build_company_metrics
from app.finance.ratio_builder import build_company_ratios
from app.ingestion.sec import ingest_company
from app.main import app
from app.models import Company, User
from app.providers.sec.mock import MockSecSource
from app.rag import tools
from app.utils.storage import FilesystemObjectStorage
from tests.fakes import FakeLLM, auth_headers

_TICKERS = ("NVDA", "AAPL", "JPM")


@pytest.fixture(scope="module")
def _migrated(test_db_url: str) -> None:
    from tests.integration.conftest import _MIGRATIONS_DIR

    cfg = Config()
    cfg.set_main_option("script_location", _MIGRATIONS_DIR)
    cfg.set_main_option("sqlalchemy.url", test_db_url)
    command.upgrade(cfg, "head")


@pytest.fixture(scope="module")
def seeded(
    _migrated: None, test_db_url: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[None]:
    """Mock-ingest the three seed companies + build metrics/ratios, committed for the module."""
    from tests.integration.conftest import truncate_all

    storage = FilesystemObjectStorage(tmp_path_factory.mktemp("storage"))
    engine = create_engine(test_db_url, future=True)
    try:
        with Session(engine) as s:
            s.add(User(email="cmp@example.com", password_hash="x", role="user"))  # noqa: S106
            for ticker in _TICKERS:
                ingest_company(s, ticker, sec=MockSecSource(), storage=storage)
                company = _company(s, ticker)
                build_company_metrics(s, company)
                build_company_ratios(s, company)
            s.commit()
        yield
    finally:
        truncate_all(engine)  # committed seed data must not leak into the shared throwaway DB
        engine.dispose()


def _company(session: Session, ticker: str) -> Company:
    return session.scalars(select(Company).where(Company.ticker == ticker)).one()


@pytest.fixture
def session(seeded: None, test_engine: Engine) -> Iterator[Session]:
    with Session(test_engine) as s:
        yield s
        s.rollback()


@pytest.fixture
def client(session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(session, "commit", session.flush)
    app.dependency_overrides[get_db] = lambda: session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


_P = "/api/v1"


# --- enrichment: metadata now populated from the (mock) submissions path ------------------------


def test_metadata_enriched_on_ingest(session: Session) -> None:
    nvda = _company(session, "NVDA")
    assert nvda.sic_code == "3674"
    assert nvda.sector == "Technology"
    assert nvda.fiscal_year_end == "01-26"


# --- deterministic comparison (DoD #1) ----------------------------------------------------------


def test_compare_calendarized_and_percentile_ranked(session: Session) -> None:
    result = compare_companies(
        session, list(_TICKERS), metrics=["revenue", "gross_margin", "net_margin"]
    )
    assert result.calendar_year is not None
    # Calendarization: the three different FYEs resolve to the SAME comparison calendar year.
    years = {c.ticker: c.calendar_year for c in result.companies}
    assert years["NVDA"] == years["AAPL"] == years["JPM"] == result.calendar_year
    # NVDA's January-FYE fiscal period label differs from the December peer (not naively equal).
    periods = {c.ticker: c.fiscal_period for c in result.companies}
    assert periods["NVDA"] != periods["JPM"]

    gm = next(row for row in result.metrics if row.metric_name == "gross_margin")
    gm_cells = {c.ticker: c for c in gm.cells}
    # JPM (a bank) has no gross margin — N/A with a reason code, never a fake 0 (DR-041).
    assert gm_cells["JPM"].result.value is None
    assert gm_cells["JPM"].result.warnings
    # NVDA has the highest gross margin of the covered peers → top percentile.
    assert gm_cells["NVDA"].result.value is not None
    assert gm_cells["NVDA"].percentile == max(
        c.percentile for c in gm.cells if c.percentile is not None
    )


def test_compare_unknown_ticker_raises(session: Session) -> None:
    with pytest.raises(ValueError, match="unknown ticker"):
        compare_companies(session, ["NVDA", "ZZZZ"])


# --- peer set (DoD #1: suggestions by SIC/industry) ---------------------------------------------


def test_peer_suggestions_rank_by_sic_then_sector(session: Session) -> None:
    peers = suggest_peers(session, "NVDA")
    tickers = [p.ticker for p in peers.peers]
    assert "AAPL" in tickers  # same sector (Technology)
    # Market-cap band is unavailable (price-dependent metrics unpersisted) — surfaced, not silent.
    assert any("market-cap" in n.lower() or "market cap" in n.lower() for n in peers.notes)


# --- AI commentary verified by the SAME Phase 3a pipeline (DoD #2) -------------------------------


def test_commentary_is_cited_and_verified(session: Session) -> None:
    g = gather(session, list(_TICKERS), metrics=["revenue", "gross_margin", "net_margin"])
    out = generate_commentary(session, g, llm=FakeLLM())
    assert out.sufficient is True
    assert out.citations, "verified commentary must carry at least one citation"
    # Every rendered claim is accepted/softened by the verifier (CIT-005) — none unsupported.
    assert all(c.status in ("accepted", "softened") for c in out.claims)


def test_markdown_commentary_is_verified_sentence_by_sentence_and_logged(
    session: Session,
) -> None:
    """ADR-0071: headings and bullets made a whole answer one "claim"; it is plain text now, each
    sentence verified, and the model call is logged against the user's AI budget."""
    from app.comparison.commentary import build_evidence
    from app.models import LlmCall

    g = gather(session, list(_TICKERS), metrics=["revenue"])
    first, second = (item.source_id for item in build_evidence(g)[:2])

    class MarkdownLLM(FakeLLM):
        async def complete(self, messages, *, model, max_tokens):  # type: ignore[no-untyped-def]
            return (
                "### Revenue\n"
                f"* **Scale:** the first company's revenue was the larger [SOURCE:{first}].\n"
                f"* The second company's revenue is listed too [SOURCE:{second}].\n"
                "* Industry revenue rose 12% last year."
            )

    user = User(email="commentary@example.com", password_hash="x", is_active=True)  # noqa: S106
    session.add(user)
    session.flush()
    out = generate_commentary(session, g, llm=MarkdownLLM(), user_id=user.id)
    assert "#" not in out.rendered_text and "**" not in out.rendered_text
    assert len(out.claims) == 3  # one per sentence, not one for the whole answer
    aside = out.claims[-1]  # verified on its own, so it is caught for having no citation
    assert (aside.status, aside.reason_code) == ("rejected", "NO_CITATION")
    logged = session.scalars(select(LlmCall).where(LlmCall.user_id == user.id)).all()
    assert [c.purpose for c in logged] == ["comparison_commentary"]


def test_commentary_never_contains_advice(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # DoD #5 + LGL-006: if a (verified) commentary ever contains advice language, the guard must
    # withhold the whole commentary rather than surface a recommendation. Inject a verified output
    # that slipped advice through, to prove the guard fires (not just the verifier's own drop).
    from app.schemas.citations import VerifiedOutput

    tainted = VerifiedOutput(
        rendered_text="NVIDIA is a strong buy.", claims=[], citations=[], sufficient=True
    )
    monkeypatch.setattr("app.comparison.commentary.verify_text", lambda *a, **k: tainted)
    g = gather(session, list(_TICKERS), metrics=["revenue"])
    out = generate_commentary(session, g, llm=FakeLLM())
    assert out.rendered_text == ""  # commentary withheld
    assert out.sufficient is False


# --- compare_companies tool (§16.7; the real Phase 2c stub) -------------------------------------


def test_compare_companies_tool_returns_source_ids(session: Session) -> None:
    runner = tools.ToolRunner(session)
    result = runner.call("compare_companies", tickers=list(_TICKERS), metrics=["revenue"])
    assert result["calendar_year"] is not None
    assert result["source_ids"], "tool output must carry backend-issued source ids (CIT-001)"
    assert len(result["companies"]) == 3


# --- endpoints ----------------------------------------------------------------------------------


def test_get_compare_endpoint(client: TestClient) -> None:
    resp = client.get(f"{_P}/compare", params={"tickers": "NVDA,AAPL,JPM", "metrics": "revenue"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["calendar_year"] is not None
    assert len(body["companies"]) == 3
    assert "not investment advice" in body["disclaimer"].lower()


def test_get_compare_requires_two_tickers(client: TestClient) -> None:
    assert client.get(f"{_P}/compare", params={"tickers": "NVDA"}).status_code == 422


def test_get_peers_endpoint(client: TestClient) -> None:
    resp = client.get(f"{_P}/compare/peers/NVDA")
    assert resp.status_code == 200
    assert resp.json()["target"] == "NVDA"


def test_commentary_endpoint_requires_auth(client: TestClient) -> None:
    resp = client.post(f"{_P}/compare/commentary", json={"tickers": ["NVDA", "AAPL"]})
    assert resp.status_code == 401


def test_commentary_endpoint_with_auth(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.comparison.commentary.get_llm_client", FakeLLM)
    user = session.scalars(select(User)).first()
    assert user is not None
    resp = client.post(
        f"{_P}/compare/commentary",
        json={"tickers": ["NVDA", "AAPL", "JPM"], "metrics": ["revenue", "gross_margin"]},
        headers=auth_headers(user.id),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "rendered_text" in body and "citations" in body

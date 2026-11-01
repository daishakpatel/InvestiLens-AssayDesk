"""Guidance extraction, outcomes, revisions, API and alerts (Phase 7i). Real Postgres, mock SEC.

Requirement IDs: FR-GD-001/002/004/006/007/009/010/011/013/014, DR-GD-001/002.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.alerts.dispatch import check_and_dispatch
from app.db import get_db
from app.finance.builder import build_company_metrics
from app.guidance import extract
from app.guidance.builder import build_company_guidance
from app.ingestion.documents.pipeline import parse_company_documents
from app.ingestion.sec import ingest_company
from app.main import app
from app.models import (
    Alert,
    Company,
    Document,
    FinancialMetric,
    GuidanceItem,
    GuidanceOutcome,
    Notification,
    User,
)
from app.providers.base import LLMClient
from app.providers.sec.mock import MockSecSource
from app.utils.storage import FilesystemObjectStorage
from tests.integration.conftest import public_table_names

_P = "/api/v1"
_RECORDED = (
    Path(__file__).resolve().parents[1] / "fixtures" / "guidance" / "nvda_q2fy27_response.json"
)


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
def client(session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(session, "commit", session.flush)
    app.dependency_overrides[get_db] = lambda: session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


class _Recorded(LLMClient):
    """Replays the recorded response, re-pointed at this release's real paragraph ID."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages: Any, *, model: str, max_tokens: int) -> str:
        raise NotImplementedError

    async def complete_json(
        self, messages: Any, *, model: str, schema: dict[str, Any], max_tokens: int
    ) -> dict[str, Any]:
        self.calls += 1
        pid = messages[1].content.split("[paragraph_id=", 1)[1].split("]", 1)[0]
        recorded = _RECORDED.read_text().replace('"1_0_p5"', json.dumps(pid))
        response: dict[str, Any] = json.loads(recorded)["response"]
        return response


def _nvda(session: Session, tmp_path: Path) -> tuple[Company, Document]:
    storage = FilesystemObjectStorage(tmp_path)
    ingest_company(session, "NVDA", sec=MockSecSource(), storage=storage)
    company = session.scalars(select(Company).where(Company.ticker == "NVDA")).one()
    parse_company_documents(session, company.id, storage=storage)
    build_company_metrics(session, company)  # Q rows and the fiscal calendar
    release = session.scalars(
        select(Document).where(
            Document.company_id == company.id,
            Document.document_type == "earnings_release",
            Document.doc_metadata["exhibit_type"].astext == "EX-99.1",
        )
    ).one()
    return company, release


def _extracted(session: Session, tmp_path: Path) -> tuple[Company, Document, _Recorded]:
    company, release = _nvda(session, tmp_path)
    llm = _Recorded()
    assert extract.extract_document_guidance(session, release, company, llm=llm) is True
    return company, release, llm


def _items(session: Session, **where: Any) -> list[GuidanceItem]:
    query = select(GuidanceItem).filter_by(**where).order_by(GuidanceItem.id)
    return list(session.scalars(query))


def _metric(session: Session, company: Company, period: str, name: str, value: str) -> None:
    fy, fq = int(period[-4:]), int(period[1])
    session.add(
        FinancialMetric(
            company_id=company.id,
            period=period,
            fiscal_year=fy,
            fiscal_quarter=fq,
            period_type="Q",
            metric_name=name,
            metric_value=Decimal(value),
            unit="USD",
            basis="gaap",
            source_id=f"xbrl:NVDA:{name}:{period}",
            is_latest=True,
        )
    )


# --- DoD: the NVDA revenue range from the EX-99.1 fixture; fabrications stored as rejected ------
def test_extraction_stores_verified_and_rejected_items(session: Session, tmp_path: Path) -> None:
    company, release, llm = _extracted(session, tmp_path)
    [revenue] = _items(session, metric="revenue", confidence_state="verified")
    assert (revenue.fiscal_year, revenue.fiscal_period) == (2027, "Q3")
    assert revenue.low == Decimal("105840000000") and revenue.high == Decimal("110160000000")
    assert revenue.point is None and revenue.derived_range and revenue.basis == "unspecified"
    assert revenue.quote == "Revenue is expected to be $108.0 billion, plus or minus 2%"
    assert revenue.paragraph_id and revenue.paragraph_id.startswith(f"{release.id}_")
    assert revenue.issued_at == release.published_at
    assert revenue.prompt_version == "guidance_extract_v1"
    assert revenue.taxonomy_version == "guidance_metric_v1"
    assert len(_items(session, confidence_state="verified")) == 6
    rejected = {(i.rejection_reason, i.point) for i in _items(session, confidence_state="rejected")}
    assert ("number_not_in_quote", Decimal("110000000000")) in rejected  # a fabricated number
    assert ("quote_not_found", Decimal("115000000000")) in rejected  # a fabricated quote
    assert len(rejected) >= 8

    # FR-GD-006: cached by (document, prompt version, model); a re-run makes no call.
    assert extract.extract_document_guidance(session, release, company, llm=llm) is False
    assert llm.calls == 1
    assert (release.doc_metadata or {})["guidance_extraction"]["guidance_extract_v1|mock"] == {
        "verified": 6,
        "rejected": 10,
    }
    # The EX-99.2 CFO commentary repeats the outlook and is not read; nor is a release lacking one.
    calls = extract.extract_company_guidance(session, company)
    assert calls == 0


def test_a_release_without_an_outlook_makes_no_call(session: Session, tmp_path: Path) -> None:
    company, release = _nvda(session, tmp_path)
    session.execute(text("DELETE FROM document_chunks WHERE document_id = :d"), {"d": release.id})
    llm = _Recorded()
    assert extract.extract_document_guidance(session, release, company, llm=llm) is False
    assert llm.calls == 0 and _items(session) == []
    assert (release.doc_metadata or {})["guidance_extraction"]["guidance_extract_v1|mock"] == {
        "no_outlook": True
    }


# --- FR-GD-007/009/010, DR-GD-001: outcomes, revisions, sources --------------------------------
def test_outcomes_and_revisions(session: Session, tmp_path: Path, client: TestClient) -> None:
    company, release, _ = _extracted(session, tmp_path)
    _metric(session, company, "Q3-FY2027", "revenue", "101200000000")
    _metric(session, company, "Q3-FY2027", "gross_profit", "75500000000")
    _metric(session, company, "Q3-FY2027", "operating_income", "66400000000")
    # A later release revises the FY2027 tax rate: the headline keeps the initial 16-18%.
    [tax] = _items(session, metric="tax_rate", confidence_state="verified")
    later = GuidanceItem(
        company_id=company.id,
        source_document_id=release.id,
        paragraph_id=tax.paragraph_id,
        issued_at=(tax.issued_at or datetime.now(UTC)) + timedelta(days=90),
        fiscal_year=2027,
        fiscal_period="FY",
        metric="tax_rate",
        low=Decimal("0.17"),
        high=Decimal("0.19"),
        unit="ratio",
        basis="gaap",
        quote=tax.quote,
        confidence_state="verified",
        taxonomy_version="guidance_metric_v1",
        prompt_version="guidance_extract_v1",
    )
    session.add(later)
    session.flush()

    assert build_company_guidance(session, company) == 7
    assert build_company_guidance(session, company) == 7  # idempotent
    session.refresh(tax)
    assert tax.superseded_by == later.id and later.superseded_by is None

    outcomes = {
        (i.metric, i.basis): session.get(GuidanceOutcome, i.id)
        for i in _items(session, confidence_state="verified")
    }
    revenue = outcomes[("revenue", "unspecified")]
    assert revenue is not None and revenue.result == "below_range"
    assert revenue.actual_value == Decimal("101200000000")
    assert revenue.delta_vs_mid == Decimal("-6800000000")
    assert revenue.actual_source_ids == ["xbrl:NVDA:revenue:Q3-FY2027"]  # the app's own IDs
    margin = outcomes[("gross_margin", "gaap")]
    assert margin is not None and margin.result == "above_range"  # 75.5 / 101.2 = 74.6%
    assert margin.actual_source_ids == [
        "xbrl:NVDA:gross_profit:Q3-FY2027",
        "xbrl:NVDA:revenue:Q3-FY2027",
    ]
    opex = outcomes[("operating_expenses", "gaap")]
    assert opex is not None and opex.actual_value == Decimal("9100000000")
    assert opex.result == "below_range"
    for basis_key in (("gross_margin", "non_gaap"), ("operating_expenses", "non_gaap")):
        outcome = outcomes[basis_key]  # DR-GD-001: a GAAP actual exists; still not compared
        assert outcome is not None and outcome.result == "not_comparable"
        assert outcome.actual_value is None and outcome.reason_code == "non_gaap_basis"
    unavailable = outcomes[("tax_rate", "gaap")]
    assert unavailable is not None and unavailable.result == "actual_unavailable"

    # FR-GD-014: the API serves the headline (initial) guidance with its revisions.
    body = client.get(f"{_P}/companies/NVDA/guidance").json()
    assert body["fiscal_years"] == [2027] and "consensus" in body["scope"]
    rows = {(r["metric"], r["basis"]): r for r in body["items"]}
    tax_row = rows[("tax_rate", "gaap")]
    assert (Decimal(tax_row["low"]), Decimal(tax_row["high"])) == (Decimal("0.16"), Decimal("0.18"))
    [revision] = tax_row["revisions"]
    assert (Decimal(revision["low"]), Decimal(revision["high"])) == (
        Decimal("0.17"),
        Decimal("0.19"),
    )
    rev = rows[("revenue", "unspecified")]
    assert rev["period_label"] == "Q3 FY2027" and rev["derived_range"] is True
    assert Decimal(rev["midpoint"]) == Decimal("108000000000")
    assert (
        rev["source_id"].startswith(f"{release.id}_")
        and rev["release"]["document_id"] == release.id
    )
    assert rev["outcome"]["result"] == "below_range"
    non_gaap = rows[("gross_margin", "non_gaap")]["outcome"]
    assert non_gaap["explanation"] == "Guided on a non-GAAP basis; GAAP actual not comparable."
    assert client.get(f"{_P}/companies/NVDA/guidance?fiscal_year=2026").json()["items"] == []
    assert client.get(f"{_P}/companies/ZZZZ/guidance").status_code == 404

    # FR-GD-011: one guided quarter is not a track record.
    record = client.get(f"{_P}/companies/NVDA/guidance/track-record").json()
    assert record["sufficient"] is False and record["label"] == "Insufficient history (n=1)"
    assert record["quarters"][0]["result"] == "below_range"


def test_track_record_shows_n_from_four_quarters(
    session: Session, tmp_path: Path, client: TestClient
) -> None:
    company, release, _ = _extracted(session, tmp_path)
    base = release.published_at or datetime.now(UTC)
    actuals = {
        "Q4-FY2026": "68000000000",
        "Q1-FY2027": "81000000000",
        "Q2-FY2027": "96000000000",
        "Q3-FY2027": "109000000000",
    }
    for n, (period, actual) in enumerate(actuals.items()):
        session.execute(
            text(
                "DELETE FROM financial_metrics WHERE company_id = :c AND period = :p "
                "AND metric_name = 'revenue'"
            ),
            {"c": company.id, "p": period},
        )
        _metric(session, company, period, "revenue", actual)
        if period == "Q3-FY2027":
            continue  # guided by the extracted release
        session.add(
            GuidanceItem(
                company_id=company.id,
                source_document_id=release.id,
                paragraph_id=f"{release.id}_0_5",
                issued_at=base - timedelta(days=90 * (3 - n)),
                fiscal_year=int(period[-4:]),
                fiscal_period=period[:2],
                metric="revenue",
                point=Decimal("80000000000"),
                unit="usd",
                basis="unspecified",
                quote="Revenue is expected to be $80.0 billion, plus or minus 2%",
                confidence_state="verified",
                taxonomy_version="guidance_metric_v1",
                prompt_version="guidance_extract_v1",
            )
        )
    session.flush()
    build_company_guidance(session, company)
    record = client.get(f"{_P}/companies/NVDA/guidance/track-record").json()
    assert record["sufficient"] is True and record["n_comparable"] == 4
    assert (record["n_above"], record["n_within"], record["n_below"]) == (2, 1, 1)
    assert record["label"] == (
        "Revenue came in above the guided range in 2 of 4 quarters, within it in 1 and below "
        "it in 1."
    )
    assert [q["period_label"] for q in record["quarters"]] == [
        "Q3 FY2027",
        "Q2 FY2027",
        "Q1 FY2027",
        "Q4 FY2026",
    ]


# --- FR-GD-013 alert ------------------------------------------------------------------------
def test_guidance_miss_alert_fires_once(session: Session, tmp_path: Path) -> None:
    company, _, _ = _extracted(session, tmp_path)
    user = User(email="guidance@example.com", password_hash="x")  # noqa: S106
    session.add(user)
    session.flush()
    alert = Alert(user_id=user.id, company_id=company.id, event_type="guidance_miss", params={})
    session.add(alert)
    session.flush()

    _metric(session, company, "Q3-FY2027", "revenue", "101200000000")  # below 105.84-110.16B
    build_company_guidance(session, company)
    later = datetime.now(UTC) + timedelta(minutes=2)
    assert check_and_dispatch(session, now=later) == 1
    [note] = session.scalars(select(Notification).where(Notification.alert_id == alert.id))
    [revenue] = _items(session, metric="revenue", confidence_state="verified")
    assert note.event_key == f"guidance_outcome:{revenue.id}"
    assert note.body == (
        "Revenue came in below the guided range ($101.2 billion vs $105.84 billion\u2013"
        f"$110.16 billion). Source: {revenue.paragraph_id}."
    )
    build_company_guidance(session, company)  # an unchanged outcome keeps its computed_at
    assert check_and_dispatch(session, now=later + timedelta(minutes=5)) == 0
    assert session.scalar(select(func.count()).select_from(Notification)) == 1


# --- FR-GD-001/002 migration ------------------------------------------------------------------
def test_guidance_migration_up_and_down(alembic_cfg: Config, test_engine: Engine) -> None:
    command.upgrade(alembic_cfg, "head")
    with test_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO companies (ticker, cik, name, status)"
                " VALUES ('GDE', '0000000047', 'Guide', 'active')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO documents (company_id, document_type, source_tier, content_hash)"
                " SELECT id, 'earnings_release', 2, 'h' FROM companies WHERE ticker = 'GDE'"
            )
        )
    insert = (
        "INSERT INTO guidance_items (company_id, source_document_id, metric, quote,"
        " confidence_state, taxonomy_version, prompt_version, derived_range, point, low, high)"
        " SELECT company_id, id, 'revenue', 'q', :state, 'v1', 'p1', false, :point, :low, :high"
        " FROM documents WHERE content_hash = 'h'"
    )
    with pytest.raises(IntegrityError), test_engine.begin() as conn:  # both forms on a verified row
        conn.execute(text(insert), {"state": "verified", "point": 1, "low": 1, "high": 2})
    with test_engine.begin() as conn:
        conn.execute(text(insert), {"state": "rejected", "point": None, "low": 1, "high": None})
        conn.execute(text(insert), {"state": "verified", "point": 5, "low": None, "high": None})
        conn.execute(
            text(
                "INSERT INTO guidance_outcomes (guidance_id, result)"
                " SELECT id, 'within_range' FROM guidance_items WHERE point = 5"
            )
        )
        assert conn.execute(text("SELECT actual_source_ids FROM guidance_outcomes")).scalar() == []
    with pytest.raises(IntegrityError), test_engine.begin() as conn:
        conn.execute(text("UPDATE guidance_outcomes SET result = 'beat'"))
    with test_engine.begin() as conn:
        conn.execute(text("DELETE FROM companies WHERE ticker = 'GDE'"))
        assert conn.execute(text("SELECT count(*) FROM guidance_outcomes")).scalar_one() == 0

    command.downgrade(alembic_cfg, "a6d2f8c3e5b1")
    assert "guidance_items" not in public_table_names(test_engine)
    assert "guidance_outcomes" not in public_table_names(test_engine)
    assert "peer_sets" in public_table_names(test_engine)
    command.upgrade(alembic_cfg, "head")

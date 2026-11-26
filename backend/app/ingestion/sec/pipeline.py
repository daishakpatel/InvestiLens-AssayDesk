"""SEC ingestion pipeline (Phase 1a).

Resolves a company, discovers its filings, applies the backfill policy, downloads and stores each
new filing, extracts 8-K items and amendment links, and upserts XBRL facts — all idempotently
(ING-001) with run tracking (ING-003), dead-lettering (ING-004), and per-item failure isolation
(ING-005). One filing's failure never aborts the rest.

Phase 7c (ADR-0041): proxies (DEF 14A, DEFA14A), late-filing notices (NT 10-K, NT 10-Q) and 8-K/A
are ingested too, each form with its own backfill window, older submission pages are read when
the window reaches back past EDGAR's recent list, and an earnings 8-K's EX-99 press releases are
stored as tier-2 `earnings_release` documents.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.cache.cache import invalidate_company
from app.config import Settings, get_settings
from app.finance.sic import sic_to_sector
from app.ingestion.documents.eightk_items import item_codes_in_document
from app.ingestion.documents.ixbrl import tagged_values
from app.models import Document
from app.providers.sec import get_sec_source
from app.providers.sec.base import CompanyMetadata, CompanyRef, FilingRef, SecSource
from app.repositories import companies as company_repo
from app.repositories import facts as facts_repo
from app.repositories import filings as filing_repo
from app.repositories.freshness import record_freshness
from app.repositories.ingestion import dead_letter, finish_run, start_run
from app.utils.dates import months_ago, years_ago
from app.utils.logging import get_logger, log_event
from app.utils.storage import ObjectStorage

logger = get_logger(__name__)
_PROXY_FORMS = ("DEF 14A", "DEFA14A")
_NOTICE_FORMS = ("NT 10-K", "NT 10-Q")
# FR-EX-007: exhibits are fetched only for results (2.02) or Reg FD / other-event 8-Ks (7.01,
# 8.01), and of those only EX-99.x; every other 8-K costs no extra SEC request.
_EXHIBIT_ITEMS = frozenset({"2.02", "7.01", "8.01"})


@dataclass
class IngestCounts:
    filings_ingested: int = 0
    filings_skipped: int = 0
    facts: int = 0
    facts_skipped: int = 0
    dead_letters: int = 0
    documents_restored: int = 0  # filings already known whose stored HTML had gone missing
    exhibits: int = 0  # EX-99 press releases stored as earnings_release documents

    def as_dict(self) -> dict[str, int]:
        return {
            "filings_ingested": self.filings_ingested,
            "filings_skipped": self.filings_skipped,
            "documents_restored": self.documents_restored,
            "facts": self.facts,
            "facts_skipped": self.facts_skipped,
            "dead_letters": self.dead_letters,
            "exhibits": self.exhibits,
        }


def _to_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _backfill_cutoff(form: str, settings: Settings, today: date) -> date | None:
    """The earliest filing date kept for a form (ING-002, FR-EX-010); None = not ingested."""
    if form.startswith("10-K"):
        return years_ago(today, settings.backfill_10k_years)
    if form.startswith("10-Q"):
        return months_ago(today, settings.backfill_10q_quarters * 3)
    if form.startswith("8-K"):  # 8-K and 8-K/A
        return months_ago(today, settings.backfill_8k_months)
    if form in _PROXY_FORMS:
        return years_ago(today, settings.backfill_proxy_years)
    if form in _NOTICE_FORMS:
        return years_ago(today, settings.backfill_nt_years)
    return None  # Forms 3/4/5, 13F, S-3, 424B… are out of scope


def _within_backfill(ref: FilingRef, settings: Settings, today: date) -> bool:
    """ING-002 backfill window per form type."""
    cutoff = _backfill_cutoff(ref.form, settings, today)
    if cutoff is None:
        return False
    filed = _to_date(ref.filing_date)
    return filed is None or filed >= cutoff  # keep undated filings; let downstream decide


def _listing_since(settings: Settings, today: date) -> date:
    """How far back the filing list must reach: the longest backfill window (FR-EX-011)."""
    cutoffs = (
        _backfill_cutoff(f, settings, today)
        for f in ("10-K", "10-Q", "8-K", *_PROXY_FORMS, *_NOTICE_FORMS)
    )
    return min(c for c in cutoffs if c is not None)


def _resolve_items(ref: FilingRef, document: bytes) -> list[str] | None:
    """An 8-K's items: the filing index's list, else the codes found in its text (FR-EX-003)."""
    if not ref.form.startswith("8-K"):
        return None
    if ref.items:
        return list(ref.items)
    return item_codes_in_document(document) or None


def _accepted_at(ref: FilingRef) -> datetime | None:
    try:
        return datetime.fromisoformat(ref.accepted_at) if ref.accepted_at else None
    except ValueError:
        return None


def _document_type(form: str) -> str:
    return "late_filing_notice" if form in _NOTICE_FORMS else "filing"


def _filing_metadata(ref: FilingRef) -> dict[str, Any]:
    metadata: dict[str, Any] = {"form": ref.form, "accession_number": ref.accession_number}
    if ref.form.startswith("8-K"):
        # The index's own item list, kept apart from `filings.items` (which falls back to the
        # codes parsed from the text) so the parse step can reconcile the two (FR-EX-003).
        metadata["items_metadata"] = list(ref.items) if ref.items else None
    if ref.form == "DEFA14A":
        metadata["supplemental"] = True  # additional proxy soliciting material (FR-EX-009)
    return metadata


def _ingest_one_filing(
    session: Session,
    *,
    sec: SecSource,
    storage: ObjectStorage,
    company_id: int,
    ticker: str,
    ref: FilingRef,
    counts: IngestCounts,
) -> None:
    if filing_repo.filing_exists(session, ref.accession_number):
        # Known filing: skip it, unless its stored HTML is gone, in which case it could never be
        # parsed again. Fetch it back to the key the filing already points at (ING-001).
        location = filing_repo.raw_document_location(session, ref.accession_number)
        if location and not storage.exists(location):
            storage.put(location, sec.fetch_document(ref))
            counts.documents_restored += 1
        counts.filings_skipped += 1
        return

    document = sec.fetch_document(ref)
    content_hash = hashlib.sha256(document).hexdigest()
    storage_key = f"sec/{ref.cik}/{ref.accession_number.replace('-', '')}/{ref.primary_document}"
    storage.put(storage_key, document)

    document_id = filing_repo.get_or_create_document(
        session,
        company_id=company_id,
        document_type=_document_type(ref.form),
        source_tier=1,
        content_hash=content_hash,
        title=f"{ticker} {ref.form}",
        source_url=ref.primary_doc_url,
        raw_document_location=storage_key,
        parser_version=None,  # set by the parse step once chunked (ADR-0041)
        doc_metadata=_filing_metadata(ref),
        published_at=_accepted_at(ref),
    )

    period_end = _to_date(ref.report_date)
    amends_id: int | None = None
    if ref.form.endswith("/A"):
        amends_id = filing_repo.get_filing_id_for_period(
            session, company_id=company_id, filing_type=ref.form[:-2], period_end=period_end
        )

    inserted = filing_repo.insert_filing(
        session,
        document_id=document_id,
        company_id=company_id,
        filing_type=ref.form,
        filing_date=_to_date(ref.filing_date),
        period_end=period_end,
        accession_number=ref.accession_number,
        source_url=ref.primary_doc_url,
        primary_document_url=ref.primary_doc_url,
        content_hash=content_hash,
        raw_document_location=storage_key,
        amends_filing_id=amends_id,
        items=_resolve_items(ref, document),
    )
    if inserted is not None:
        counts.filings_ingested += 1
    else:
        counts.filings_skipped += 1


def _ingest_exhibits(
    session: Session,
    *,
    sec: SecSource,
    storage: ObjectStorage,
    company_id: int,
    ticker: str,
    ref: FilingRef,
    counts: IngestCounts,
) -> None:
    """Store an earnings 8-K's EX-99 press releases as tier-2 documents (FR-EX-006…008).

    Runs for new and already-known filings alike, so 8-Ks ingested before Phase 7c get their
    exhibits too; `exhibits_checked` on the filing's document means the index is read once.
    """
    filing = filing_repo.get_by_accession(session, ref.accession_number)
    if filing is None or not _EXHIBIT_ITEMS.intersection(filing.items or []):
        return
    parent = session.get(Document, filing.document_id)
    if parent is None or (parent.doc_metadata or {}).get("exhibits_checked"):
        return
    accepted = _accepted_at(ref) or parent.published_at
    folder = ref.accession_number.replace("-", "")
    for exhibit in sec.list_exhibits(ref):
        data = sec.fetch_exhibit(exhibit)
        key = f"sec/{ref.cik}/{folder}/{exhibit.filename}"
        storage.put(key, data)
        filing_repo.get_or_create_document(
            session,
            company_id=company_id,
            document_type="earnings_release",  # what EarningsReleaseSource and tier 2 expect
            source_tier=2,
            content_hash=hashlib.sha256(data).hexdigest(),
            title=f"{ticker} {ref.form} {exhibit.exhibit_type}",
            source_url=exhibit.url,
            raw_document_location=key,
            published_at=accepted,
            doc_metadata={
                "parent_accession": ref.accession_number,
                "exhibit_type": exhibit.exhibit_type,
                "filename": exhibit.filename,
                "size_bytes": exhibit.size_bytes,
            },
        )
        counts.exhibits += 1
    filing_repo.merge_document_metadata(session, parent.id, {"exhibits_checked": True})


def ingest_company(
    session: Session,
    ticker: str,
    *,
    sec: SecSource | None = None,
    storage: ObjectStorage,
    today: date | None = None,
) -> IngestCounts:
    """Ingest one company end-to-end. Returns per-run counts; records an `ingestion_runs` row."""
    settings = get_settings()
    sec = sec or get_sec_source()
    today = today or datetime.now(UTC).date()
    counts = IngestCounts()

    ref = _find_company_ref(sec, ticker)
    if ref is None:
        run = start_run(session, source="sec", company_id=None)
        finish_run(
            session,
            run,
            status="failed",
            counts=counts.as_dict(),
            error=f"unknown ticker: {ticker}",
        )
        raise ValueError(f"unknown ticker: {ticker}")

    # Profile enrichment from the submissions endpoint (SIC/industry/exchange/FYE): needed for
    # peer-set grouping (§37.1) and sector exposure (§37.2). A metadata-fetch failure must not
    # abort ingestion — the fields are nullable and `upsert_company` preserves any prior value.
    meta = CompanyMetadata()
    try:
        meta = sec.company_metadata(ref.cik)
    except Exception as exc:
        log_event(
            logger,
            logging.WARNING,
            "sec.company_metadata.failed",
            ticker=ref.ticker,
            error=str(exc),
        )
    company_id = company_repo.upsert_company(
        session,
        ticker=ref.ticker,
        cik=ref.cik,
        name=meta.name or ref.title,
        exchange=meta.exchange,
        sector=sic_to_sector(meta.sic_code),
        industry=meta.industry,
        sic_code=meta.sic_code,
        fiscal_year_end=meta.fiscal_year_end,
    )
    company_repo.upsert_identifier(
        session,
        company_id=company_id,
        identifier_type="ticker",
        identifier_value=ref.ticker,
        is_primary=True,
    )
    company_repo.upsert_identifier(
        session,
        company_id=company_id,
        identifier_type="cik",
        identifier_value=ref.cik,
    )

    run = start_run(session, source="sec", company_id=company_id)
    attempt_at = datetime.now(UTC)

    # A provider outage (SEC EDGAR down/429-exhausted) must degrade, not crash the caller or leave
    # the run stuck "running" forever with no data_freshness update (ERR-002, OBS-002/§28.2 "SEC
    # unavailable" — this was a real gap: unlike prices/news, SEC ingestion never wrote
    # data_freshness at all, so the API's freshness endpoint could never distinguish a healthy
    # company from one whose ingest has never succeeded or has been failing for days).
    try:
        filings = list(sec.list_filings(ref.cik, since=_listing_since(settings, today)))
    except Exception as exc:
        dead_letter(
            session, source="sec", payload_ref=ref.cik, error=f"{type(exc).__name__}: {exc}"
        )
        record_freshness(
            session,
            company_id=company_id,
            source="sec",
            status="failed",
            last_success_at=None,
            last_attempt_at=attempt_at,
            message=str(exc),
        )
        finish_run(session, run, status="failed", counts=counts.as_dict(), error=str(exc))
        log_event(
            logger, logging.ERROR, "sec.list_filings.failed", ticker=ref.ticker, error=str(exc)
        )
        return counts

    for filing in filings:
        if not _within_backfill(filing, settings, today):
            continue
        # SAVEPOINT per filing (ING-005): a failure rolls back only this filing, leaving the
        # outer transaction (run row, earlier filings) intact so we can dead-letter and continue.
        try:
            with session.begin_nested():
                _ingest_one_filing(
                    session,
                    sec=sec,
                    storage=storage,
                    company_id=company_id,
                    ticker=ref.ticker,
                    ref=filing,
                    counts=counts,
                )
        except Exception as exc:
            counts.dead_letters += 1
            dead_letter(
                session,
                source="sec",
                payload_ref=filing.accession_number,
                error=f"{type(exc).__name__}: {exc}",
            )
            log_event(
                logger,
                logging.ERROR,
                "sec.filing.failed",
                accession=filing.accession_number,
                error=str(exc),
            )
            continue
        # Exhibits in a savepoint of their own: a failed exhibit fetch keeps the filing and is
        # retried on the next run (its `exhibits_checked` flag is not set).
        try:
            with session.begin_nested():
                _ingest_exhibits(
                    session,
                    sec=sec,
                    storage=storage,
                    company_id=company_id,
                    ticker=ref.ticker,
                    ref=filing,
                    counts=counts,
                )
        except Exception as exc:
            counts.dead_letters += 1
            dead_letter(
                session,
                source="sec",
                payload_ref=f"exhibits:{filing.accession_number}",
                error=f"{type(exc).__name__}: {exc}",
            )
            log_event(
                logger,
                logging.ERROR,
                "sec.exhibits.failed",
                accession=filing.accession_number,
                error=str(exc),
            )

    try:
        with session.begin_nested():  # SAVEPOINT: facts failure doesn't lose the filings
            rows, skipped = _parse_facts(company_id, sec, ref.cik)
            rows += _cover_share_rows(session, company_id, rows, storage)
            counts.facts = facts_repo.replace_company_facts(
                session, company_id=company_id, facts=rows
            )
            counts.facts_skipped = skipped
    except Exception as exc:
        counts.facts = 0
        counts.dead_letters += 1
        dead_letter(
            session,
            source="sec",
            payload_ref=f"companyfacts:{ref.cik}",
            error=f"{type(exc).__name__}: {exc}",
        )
        log_event(logger, logging.ERROR, "sec.facts.failed", cik=ref.cik, error=str(exc))

    status = "success" if counts.dead_letters == 0 else "partial"
    record_freshness(
        session,
        company_id=company_id,
        source="sec",
        status="fresh",
        last_success_at=datetime.now(UTC),
        last_attempt_at=attempt_at,
    )
    finish_run(session, run, status=status, counts=counts.as_dict())
    # `filing.ingested` event → drop this company's cached data so the API serves fresh (CACHE-002).
    if counts.filings_ingested or counts.facts or counts.exhibits:
        invalidate_company(ref.ticker)
    log_event(logger, logging.INFO, "sec.company.ingested", ticker=ref.ticker, **counts.as_dict())
    return counts


def _find_company_ref(sec: SecSource, ticker: str) -> CompanyRef | None:
    key = ticker.strip().upper()
    for ref in sec.company_tickers():
        if ref.ticker.upper() == key or ref.cik == key.zfill(10):
            return ref
    return None


def _parse_facts(company_id: int, sec: SecSource, cik: str) -> tuple[list[dict[str, Any]], int]:
    from app.ingestion.sec.xbrl import parse_companyfacts

    return parse_companyfacts(company_id, sec.companyfacts(cik))


SHARES_CONCEPT = "dei:EntityCommonStockSharesOutstanding"


def _cover_share_rows(
    session: Session, company_id: int, rows: list[dict[str, Any]], storage: ObjectStorage
) -> list[dict[str, Any]]:
    """Cover-page share counts read from the inline XBRL of each 10-K/10-Q that companyfacts has
    no count for (ADR-0074). companyfacts leaves out dimensioned facts, so a multi-class filer
    (Alphabet, Meta, Mastercard, Visa) reports its counts per class and has none there."""
    counted = {r["accession_number"] for r in rows if r["concept_tag"] == SHARES_CONCEPT}
    out: list[dict[str, Any]] = []
    for form in ("10-K", "10-Q"):
        for filing in filing_repo.list_filings_by_type(
            session, company_id=company_id, filing_type=form
        ):
            if filing.accession_number in counted or not filing.raw_document_location:
                continue
            try:
                html = storage.get(filing.raw_document_location).decode("utf-8", errors="ignore")
            except Exception as exc:  # a missing raw file leaves this filing uncounted, logged
                log_event(
                    logger,
                    logging.WARNING,
                    "sec.cover_shares.unreadable",
                    accession=filing.accession_number,
                    error=str(exc),
                )
                continue
            values = [v for v in tagged_values(html) if v.concept == SHARES_CONCEPT]
            # A total, when the cover gives one; otherwise one row per class, summed at valuation.
            totals = [v for v in values if not v.context.members]
            for v in totals or values:
                out.append(
                    {
                        "company_id": company_id,
                        "accession_number": filing.accession_number,
                        "concept_tag": SHARES_CONCEPT,
                        "context_id": None,
                        "period_start": None,
                        "period_end": v.context.period_end,
                        "period_type": "instant",
                        "dimensions": dict(v.context.members) or None,
                        "value": v.value,
                        "unit": "shares",
                        "decimals": None,
                        "filed_date": filing.filing_date,
                        "is_amended": False,
                    }
                )
    return out


@dataclass
class BatchResult:
    per_company: dict[str, IngestCounts] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)


def ingest_companies(
    session_factory: Callable[[], Session],
    tickers: list[str],
    *,
    storage: ObjectStorage,
) -> BatchResult:
    """Ingest several companies; one company's failure never aborts the batch (ING-005)."""
    result = BatchResult()
    for ticker in tickers:
        try:
            with session_factory() as session:
                result.per_company[ticker] = ingest_company(session, ticker, storage=storage)
                session.commit()
        except Exception as exc:
            result.failures[ticker] = f"{type(exc).__name__}: {exc}"
            log_event(logger, logging.ERROR, "sec.company.aborted", ticker=ticker, error=str(exc))
    return result

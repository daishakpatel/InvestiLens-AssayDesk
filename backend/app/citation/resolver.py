"""Resolve a backend-issued source_id to its full record for the citation modal (CIT-006, §13.3).

Powers `GET /sources/{source_id}` for all six source types. The id scheme (prefix-based, with the
document-chunk anchor as the default) keeps resolution unambiguous:
- ``news:<id>``     → news_item
- ``xbrl:<fact_id>``→ xbrl_fact
- ``derived:…`` (or a `financial_metrics.source_id` match) → derived_metric (with lineage, CIT-003)
- ``derived:<TICKER>:revenue_<yoy|cagr>:FY<start>-FY<end>`` → the same, computed from those two
  years' stored revenue (ADR-0055)
- ``derived:<TICKER>:<valuation multiple>:<YYYY-MM-DD>`` → the multiple at that close, rebuilt
  from what was filed by then once its stored latest-close row was replaced (ADR-0063)
- ``filing:<id>`` → a whole filing (an alert's new 10-Q, the two filings an earnings brief compares)
  as a text_chunk naming it, with the filing on EDGAR as the deep link
- ``redflag:<TICKER>:<rule>:<accession>`` → the flag's quoted filing text as a text_chunk, with
  the filing as the deep link (Phase 7e, FR-RF-008)
- ``price:<TICKER>:<date>[:<end_date>]`` → price_bar: that trading day's bar, or two days' closes
  (ADR-0054)
- otherwise the id is a chunk ``paragraph_id`` → text_chunk / table_chunk, or earnings_release /
  transcript when the parent document is one.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.finance.display import describe_metric, format_price
from app.finance.metrics import MULTIPLE, average, cagr, yoy_growth
from app.finance.valuation_builder import (
    LATEST_TTM_SINCE,
    VALUATION_METRICS,
    PricePoint,
    metric_flags,
    stored_name,
    valuation_at_close,
)
from app.models import (
    Company,
    Document,
    DocumentChunk,
    Filing,
    FinancialFact,
    FinancialMetric,
    News,
    PriceHistory,
)
from app.redflags import taxonomy
from app.repositories import red_flags as red_flag_repo
from app.schemas.citations import SourceDetail
from app.schemas.sources import (
    DerivedMetricSource,
    EarningsReleaseSource,
    NewsItemSource,
    PriceBarSource,
    TableChunkSource,
    TextChunkSource,
    XbrlFactSource,
)

_METRIC_GROWTH = re.compile(r"derived:([^:]+):([a-z_]+?)_(yoy|cagr|avg):FY(\d{4})-FY(\d{4})")
_DATED_VALUATION = re.compile(r"derived:([^:]+):([a-z_]+):(\d{4}-\d{2}-\d{2})")
_NUMERIC_SCALE = Decimal("0.00000001")  # financial_metrics.metric_value is NUMERIC(28, 8)


def _news(row: News) -> SourceDetail:
    source = NewsItemSource(
        source_id=f"news:{row.id}",
        tier=4,
        url=row.url,
        news_id=str(row.id),
        publisher=row.publisher,
        published_at=row.published_at.isoformat() if row.published_at else None,
        excerpt_span=row.description,
    )
    text = " ".join(p for p in (row.title, row.description) if p)
    return SourceDetail(source=source, text=text, deep_link=row.url)


def _xbrl(row: FinancialFact) -> SourceDetail:
    source = XbrlFactSource(
        source_id=f"xbrl:{row.id}",
        tier=1,
        accession_number=row.accession_number,
        concept_tag=row.concept_tag,
        context_id=row.context_id,
        unit=row.unit,
        value=row.value,
    )
    return SourceDetail(
        source=source, text=f"{row.concept_tag} = {row.value} {row.unit or ''}".strip()
    )


def _metric(row: FinancialMetric) -> SourceDetail:
    """A canonical metric → derived_metric (with lineage, CIT-003) or xbrl_fact for a base value."""
    text = describe_metric(row.metric_name, row.period, row.metric_value, row.unit)
    sid = row.source_id or f"metric:{row.metric_name}:{row.period}"
    if row.is_derived:
        flags = row.quality_flags or {}
        inputs = [dict(i) for i in flags.get("inputs", [])]
        derived = DerivedMetricSource(
            source_id=sid,
            tier=1,
            formula_id=row.formula_id or row.metric_name,
            formula_version=str(flags.get("formula_version") or ""),
            input_source_ids=[str(i.get("source_id")) for i in inputs if i.get("source_id")],
            value=row.metric_value,
            period=row.period,
        )
        return SourceDetail(
            source=derived,
            text=text,
            # An input without a source (a price) is listed by name, not as a dead "None" link.
            lineage=[{k: str(v) for k, v in i.items() if v is not None} for i in inputs],
        )
    fact = XbrlFactSource(
        source_id=sid,
        tier=1,
        accession_number=row.as_reported_accession or sid,
        concept_tag=row.metric_name,
        unit=row.unit,
        value=row.metric_value,
    )
    return SourceDetail(source=fact, text=text)


def _chunk(session: Session, row: DocumentChunk) -> SourceDetail:
    doc = session.get(Document, row.document_id)
    deep_link = doc.source_url if doc else None
    anchor = row.paragraph_id or f"{row.document_id}_{row.chunk_index}"
    section_path = list(row.section_path or [])
    if doc and doc.document_type in {"earnings_release", "transcript"}:
        source: EarningsReleaseSource | TextChunkSource | TableChunkSource = EarningsReleaseSource(
            source_id=anchor,
            tier=2 if doc.document_type == "earnings_release" else 3,
            source_type=doc.document_type,  # type: ignore[arg-type]
            document_id=str(row.document_id),
            paragraph_id=row.paragraph_id,
            char_start=row.char_start,
            char_end=row.char_end,
        )
    elif row.chunk_type == "table":
        meta = row.chunk_metadata or {}
        source = TableChunkSource(
            source_id=anchor,
            tier=row.tier or 1,
            document_id=str(row.document_id),
            table_id=str(meta.get("table_id", anchor)),
            row_labels=list(meta.get("row_labels", []) or []),
            col_labels=list(meta.get("col_labels", []) or []),
            cell_refs=list(meta.get("cell_refs", []) or []),
        )
    else:
        source = TextChunkSource(
            source_id=anchor,
            tier=row.tier or 1,
            document_id=str(row.document_id),
            section_path=section_path,
            paragraph_id=row.paragraph_id,
            char_start=row.char_start,
            char_end=row.char_end,
            page=row.page,
            text=row.text,
        )
    return SourceDetail(
        source=source,
        text=row.text,
        highlight_start=row.char_start,
        highlight_end=row.char_end,
        deep_link=deep_link,
    )


def resolve_source(session: Session, source_id: str) -> SourceDetail | None:
    """Resolve a source_id to its full record + marked span + deep link, or None if unknown."""
    if source_id.startswith("news:"):
        row = session.get(News, _as_int(source_id.split(":", 1)[1]))
        return _news(row) if row else None
    if source_id.startswith(taxonomy.SOURCE_PREFIX):
        return _red_flag(session, source_id)
    if source_id.startswith("price:"):
        return _price(session, source_id)
    if source_id.startswith("filing:"):
        return _filing(session, source_id)
    # A canonical metric's own source_id (base or derived) resolves first — its scheme may itself
    # start with "xbrl:"/"derived:", so this precedes the raw-fact prefix branch below.
    metric = session.scalar(
        select(FinancialMetric).where(
            FinancialMetric.source_id == source_id, FinancialMetric.is_latest.is_(True)
        )
    )
    if metric is not None:
        return _metric(metric)
    growth = _METRIC_GROWTH.fullmatch(source_id)
    if growth:
        return _metric_growth(session, source_id, growth)
    dated = _DATED_VALUATION.fullmatch(source_id)
    if dated and dated[2] in VALUATION_METRICS:
        return _valuation_on(session, source_id, dated)
    if source_id.startswith("xbrl:"):
        fact = session.get(FinancialFact, _as_int(source_id.split(":", 1)[1]))
        return _xbrl(fact) if fact else None
    chunk = session.scalar(select(DocumentChunk).where(DocumentChunk.paragraph_id == source_id))
    return _chunk(session, chunk) if chunk else None


def _valuation_on(session: Session, source_id: str, match: re.Match[str]) -> SourceDetail | None:
    """A latest-close multiple a report cited, rebuilt for that close after the next price run
    replaced its row: same close, shares and TTM figures as filed by then, so the same value."""
    ticker, name = match[1], match[2]
    try:
        day = date.fromisoformat(match[3])
    except ValueError:
        return None
    if day < LATEST_TTM_SINCE:  # cited under the earlier method: its value can't be rebuilt
        return None
    company = session.scalar(select(Company).where(Company.ticker == ticker))
    if company is None:
        return None
    close = session.scalar(
        select(PriceHistory.close).where(
            PriceHistory.company_id == company.id, PriceHistory.date == day
        )
    )
    if close is None:  # only a stored trading day was ever a latest close
        return None
    results, basis = valuation_at_close(session, company, PricePoint(day, close))
    result = next((r for r in results if stored_name(r) == name), None)
    if result is None:
        return None
    return _metric(
        FinancialMetric(  # never added to the session: recomputed, as stored then
            source_id=source_id,
            metric_name=name,
            period=day.isoformat(),
            metric_value=None if result.value is None else result.value.quantize(_NUMERIC_SCALE),
            unit=result.unit,
            is_derived=True,
            formula_id=name,
            quality_flags=metric_flags(result, basis),
        )
    )


def _metric_growth(session: Session, source_id: str, match: re.Match[str]) -> SourceDetail | None:
    """A growth figure or multi-year average, recomputed from the stored FY rows its ID names
    (ADR-0055, ADR-0066, ADR-0069)."""
    ticker, metric, kind = match[1], match[2], match[3]
    start, end = int(match[4]), int(match[5])
    rows = {
        row.fiscal_year: row
        for row in session.scalars(
            select(FinancialMetric)
            .join(Company, Company.id == FinancialMetric.company_id)
            .where(
                Company.ticker == ticker,
                FinancialMetric.metric_name == metric,
                FinancialMetric.period_type == "FY",
                FinancialMetric.is_latest.is_(True),
                FinancialMetric.fiscal_year.between(start, end),
            )
        )
    }
    if start not in rows or end not in rows:
        return None
    first, last = rows[start], rows[end]
    if kind == "avg":  # the years with a value, as the report averaged them
        span = [rows[y] for y in range(start, end + 1) if y in rows]
        used = [r for r in span if r.metric_value is not None]
        result = average([r.metric_value for r in used], unit=first.unit or MULTIPLE)
        name, period = f"{metric}_avg", f"FY{start} to FY{end}"
    else:
        used = [first, last]
        result = (
            yoy_growth(last.metric_value, first.metric_value)
            if kind == "yoy"
            else cagr(first.metric_value, last.metric_value, end - start)
        )
        # Revenue keeps "yoy_growth"/"cagr" (reads "Revenue growth"); others "Net income growth".
        suffix = "growth" if kind == "yoy" else "cagr"
        name = result.formula_id if metric == "revenue" else f"{metric}_{suffix}"
        period = f"FY{end}"
    if result.value is None:
        return None
    return _metric(
        FinancialMetric(  # never added to the session: this figure is computed, not stored
            source_id=source_id,
            metric_name=name,
            period=period,
            metric_value=result.value,
            unit=result.unit,
            is_derived=True,
            formula_id=result.formula_id,
            quality_flags={
                "formula_version": result.formula_version,
                "inputs": [
                    {"name": metric, "period": row.period, "source_id": row.source_id}
                    for row in used
                ],
            },
        )
    )


def _bar_text(ticker: str, bar: PriceHistory) -> str:
    """One trading day, every field the provider gave, as the sentence a chat answer quotes."""
    parts = [
        f"{label} {format_price(value)}"
        for label, value in (
            ("opened at", bar.open),
            ("high", bar.high),
            ("low", bar.low),
            ("closed at", bar.close),
        )
        if value is not None
    ]
    volume = f"; volume {bar.volume:,} shares" if bar.volume is not None else ""
    return f"{ticker} on {bar.date.isoformat()}: {', '.join(parts)}{volume}"


def _price(session: Session, source_id: str) -> SourceDetail | None:
    """`price:TICKER:DATE` (one bar) or `price:TICKER:DATE:END` (two closes); exact days only."""
    parts = source_id.split(":")
    if len(parts) not in (3, 4):
        return None
    try:
        days = [date.fromisoformat(d) for d in parts[2:]]
    except ValueError:
        return None
    company = session.scalar(select(Company).where(Company.ticker == parts[1].upper()))
    if company is None:
        return None
    bars = [
        session.scalar(
            select(PriceHistory).where(
                PriceHistory.company_id == company.id, PriceHistory.date == day
            )
        )
        for day in days
    ]
    found = [b for b in bars if b is not None]
    if len(found) != len(days):
        return None
    provider = found[0].provider.capitalize() if found[0].provider else "Market"
    if len(found) == 1:
        text = _bar_text(company.ticker, found[0])
    else:
        closes = [format_price(b.close) if b.close is not None else "n/a" for b in found]
        text = (
            f"{company.ticker} closed at {closes[0]} on {days[0].isoformat()} "
            f"and {closes[1]} on {days[1].isoformat()}"
        )
    source = PriceBarSource(
        source_id=source_id,
        tier=4,
        ticker=company.ticker,
        date=days[0].isoformat(),
        end_date=days[1].isoformat() if len(days) == 2 else None,
        provider=found[0].provider,
    )
    # Tier 4 claims are framed as reported by the outlet (spec §15).
    return SourceDetail(source=source, text=f"{text} ({provider} end-of-day data)")


def _filing(session: Session, source_id: str) -> SourceDetail | None:
    """A whole filing: which company's form, filed when, for which period, and a link to it."""
    row = session.get(Filing, _as_int(source_id.split(":", 1)[1]))
    if row is None:
        return None
    company = session.get(Company, row.company_id)
    text = f"{company.ticker if company else 'Company'} {row.filing_type}"
    if row.filing_date:
        text += f" filed {row.filing_date.isoformat()}"
    if row.period_end:
        text += f", for the period ending {row.period_end.isoformat()}"
    if row.items:
        text += f" (items {', '.join(row.items)})"
    source = TextChunkSource(
        source_id=source_id,
        tier=1,
        document_id=str(row.document_id),
        section_path=[row.filing_type],
        text=text,
    )
    return SourceDetail(
        source=source, text=text, deep_link=row.primary_document_url or row.source_url
    )


def _red_flag(session: Session, source_id: str) -> SourceDetail | None:
    """A red flag: its quoted filing text, the rule that fired, and a link to the filing."""
    parts = taxonomy.parse_source_id(source_id)
    if parts is None:
        return None
    ticker, rule_key, accession = parts
    found = red_flag_repo.by_source(
        session,
        ticker=ticker,
        rule_key=rule_key,
        accession=accession,
        taxonomy_version=taxonomy.TAXONOMY_VERSION,
    )
    rule = taxonomy.RULES_BY_KEY.get(rule_key)
    if found is None or rule is None:
        return None
    flag, filing = found
    source = TextChunkSource(
        source_id=source_id,
        tier=1,
        document_id=str(flag.document_id),
        section_path=[filing.filing_type, rule.label],
        paragraph_id=flag.paragraph_id,
        text=flag.snippet,
    )
    return SourceDetail(
        source=source,
        text=flag.snippet,
        deep_link=filing.primary_document_url or filing.source_url,
    )


def _as_int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return -1

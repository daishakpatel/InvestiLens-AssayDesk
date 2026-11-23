"""Canonical financial_metrics persistence (Phase 1b).

Idempotent by the natural key (company, period, metric, basis, as_reported_accession). Restated
values keep both rows with `is_latest` distinguishing them (DR-023).
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import FinancialMetric


def get_latest_metric(
    session: Session, *, company_id: int, metric_name: str, period: str
) -> FinancialMetric | None:
    """One canonical metric for a period (latest, non-restated). Read for the tool layer."""
    return session.scalar(
        select(FinancialMetric).where(
            FinancialMetric.company_id == company_id,
            FinancialMetric.metric_name == metric_name,
            FinancialMetric.period == period,
            FinancialMetric.is_latest.is_(True),
        )
    )


def get_metric_series(
    session: Session,
    *,
    company_id: int,
    metric_name: str,
    period_type: str = "FY",
) -> list[FinancialMetric]:
    """A metric's time series (latest rows), oldest first, for one period type (RAG-031)."""
    return list(
        session.scalars(
            select(FinancialMetric)
            .where(
                FinancialMetric.company_id == company_id,
                FinancialMetric.metric_name == metric_name,
                FinancialMetric.period_type == period_type,
                FinancialMetric.is_latest.is_(True),
            )
            .order_by(FinancialMetric.fiscal_year, FinancialMetric.fiscal_quarter)
        )
    )


def get_series_multi(
    session: Session,
    *,
    company_id: int,
    metric_names: list[str],
    period_type: str = "FY",
    from_year: int | None = None,
    to_year: int | None = None,
) -> list[FinancialMetric]:
    """Latest rows for several metrics over a fiscal-year window, oldest first (financials API)."""
    conditions = [
        FinancialMetric.company_id == company_id,
        FinancialMetric.metric_name.in_(metric_names),
        FinancialMetric.period_type == period_type,
        FinancialMetric.is_latest.is_(True),
    ]
    if from_year is not None:
        conditions.append(FinancialMetric.fiscal_year >= from_year)
    if to_year is not None:
        conditions.append(FinancialMetric.fiscal_year <= to_year)
    return list(
        session.scalars(
            select(FinancialMetric)
            .where(*conditions)
            .order_by(
                FinancialMetric.metric_name,
                FinancialMetric.fiscal_year,
                FinancialMetric.fiscal_quarter,
            )
        )
    )


def get_quarterly_rows(session: Session, *, company_id: int) -> list[FinancialMetric]:
    """A company's latest quarterly and TTM rows, for the coverage report (FR-QT-010)."""
    return list(
        session.scalars(
            select(FinancialMetric).where(
                FinancialMetric.company_id == company_id,
                FinancialMetric.period_type.in_(("Q", "TTM")),
                FinancialMetric.is_latest.is_(True),
            )
        )
    )


def get_latest_metrics(
    session: Session, *, company_id: int, metric_names: list[str]
) -> list[FinancialMetric]:
    """The single most recent (by fiscal year) latest row per named metric, for valuation (§12).

    Annual and latest-close rows only: a quarter or TTM of the year in progress has a later
    fiscal year than the last 10-K and would otherwise outrank the annual figure (ADR-0040).
    """
    rows = list(
        session.scalars(
            select(FinancialMetric)
            .where(
                FinancialMetric.company_id == company_id,
                FinancialMetric.metric_name.in_(metric_names),
                FinancialMetric.is_latest.is_(True),
                FinancialMetric.period_type.in_(("FY", "LATEST")),
            )
            # A valuation at the latest close outranks the same multiple at a fiscal year end.
            .order_by(
                (FinancialMetric.period_type == "LATEST").desc(),
                FinancialMetric.fiscal_year.desc(),
                FinancialMetric.fiscal_quarter.desc(),
            )
        )
    )
    seen: dict[str, FinancialMetric] = {}
    for row in rows:
        seen.setdefault(row.metric_name, row)
    return [seen[name] for name in metric_names if name in seen]


def clear_company_metrics(session: Session, company_id: int) -> None:
    """Remove a company's metrics so a rebuild is idempotent."""
    session.execute(delete(FinancialMetric).where(FinancialMetric.company_id == company_id))


def clear_company_metric_names(session: Session, company_id: int, names: Collection[str]) -> None:
    """Remove only these metrics, so a later builder's rebuild is idempotent on its own."""
    session.execute(
        delete(FinancialMetric).where(
            FinancialMetric.company_id == company_id, FinancialMetric.metric_name.in_(names)
        )
    )


def upsert_metric(
    session: Session,
    *,
    company_id: int,
    period: str,
    fiscal_year: int | None,
    fiscal_quarter: int | None,
    period_start: date | None,
    period_end: date | None,
    period_type: str,
    metric_name: str,
    metric_value: Decimal | None,
    unit: str,
    basis: str = "gaap",
    is_derived: bool = False,
    source_tag: str | None = None,
    formula_id: str | None = None,
    source_id: str | None = None,
    as_reported_accession: str | None = None,
    is_latest: bool = True,
    quality_flags: dict[str, Any] | None = None,
) -> None:
    """Insert or update one canonical metric row (ING-001 idempotent)."""
    values = dict(
        company_id=company_id,
        period=period,
        fiscal_year=fiscal_year,
        fiscal_quarter=fiscal_quarter,
        period_start=period_start,
        period_end=period_end,
        period_type=period_type,
        metric_name=metric_name,
        metric_value=metric_value,
        unit=unit,
        basis=basis,
        is_derived=is_derived,
        source_tag=source_tag,
        formula_id=formula_id,
        source_id=source_id,
        as_reported_accession=as_reported_accession,
        is_latest=is_latest,
        quality_flags=quality_flags,
    )
    update_cols = {
        k: values[k]
        for k in (
            "metric_value",
            "unit",
            "is_derived",
            "source_tag",
            "formula_id",
            "source_id",
            "is_latest",
            "quality_flags",
            "fiscal_year",
            "fiscal_quarter",
            "period_start",
            "period_end",
            "period_type",
        )
    }
    stmt = (
        pg_insert(FinancialMetric)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[
                "company_id",
                "period",
                "metric_name",
                "basis",
                "as_reported_accession",
            ],
            set_=update_cols,
        )
    )
    session.execute(stmt)


def annual_values(
    session: Session, *, company_ids: list[int], metric_names: list[str]
) -> list[FinancialMetric]:
    """Every FY row with a value for these companies and metrics: one query for a peer set."""
    return list(
        session.scalars(
            select(FinancialMetric).where(
                FinancialMetric.company_id.in_(company_ids),
                FinancialMetric.metric_name.in_(metric_names),
                FinancialMetric.period_type == "FY",
                FinancialMetric.is_latest.is_(True),
                FinancialMetric.metric_value.is_not(None),
            )
        )
    )

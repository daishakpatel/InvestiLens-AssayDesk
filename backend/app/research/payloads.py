"""Deterministic financial payloads for the section generators (§10.3, DR: no LLM math).

Each payload is JSON the LLM only *explains* — every number is computed here in Decimal from
`financial_metrics` (Phase 1c) and handed over already written for a reader ("$215.94 billion",
"71.07%", "46.63x", via `finance.display`), so the model quotes figures instead of copying raw
Decimals; a NULL metric reads "not available" (bank-inapplicable metrics for JPM, DR-041/042).
Alongside the payload we return the `EvidenceItem`s whose source IDs the payload exposes, so a
claim citing a number verifies against the exact value (within the verifier's 1% tolerance).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.citation.evidence import EvidenceItem
from app.finance import metrics as fin
from app.finance.display import format_metric_value, metric_label
from app.finance.valuation_builder import LATEST, VALUATION_METRICS
from app.models import Company
from app.repositories import metrics as metric_repo
from app.research.evidence import from_metric, metric_source_id

# Metric names per section (canonical `financial_metrics.metric_name` values from Phase 1b/1c).
SECTION_METRICS: dict[str, list[str]] = {
    "executive_summary": ["revenue", "gross_margin"],
    "revenue_analysis": ["revenue"],
    "profitability_analysis": [
        "gross_margin",
        "operating_margin",
        "net_margin",
        "ebitda",
        "roe",  # returns carry the section for banks, where the margins above are N/A
        "roa",
    ],
    "balance_sheet_analysis": ["total_debt", "debt_to_equity", "current_ratio"],
    "cash_flow_analysis": ["free_cash_flow", "fcf_margin", "capex_intensity", "sbc_pct_revenue"],
    "valuation_analysis": ["market_cap", "pe_ratio", "ps_ratio"],
}


_NOT_APPLICABLE = "not applicable for this sector"
# Recent history only: a report narrating two decades of a series reads as a list of numbers.
_YEARS = 5


def _series_block(
    session: Session, company_id: int, name: str
) -> tuple[dict[str, Any], list[EvidenceItem]]:
    rows = metric_repo.get_metric_series(session, company_id=company_id, metric_name=name)[-_YEARS:]
    if not any(row.metric_value is not None for row in rows):
        reasons = {w for row in rows for w in (row.quality_flags or {}).get("warnings") or []}
        # Said once (DR-001); a metric with no value and no sector reason is left out entirely.
        if fin.NOT_APPLICABLE_SECTOR in reasons:
            return {"not_applicable": _NOT_APPLICABLE}, []
        return {}, []
    unit = rows[0].unit
    series: dict[str, str] = {}
    source_ids: dict[str, str] = {}
    evidence: list[EvidenceItem] = []
    for row in rows:
        warnings = (row.quality_flags or {}).get("warnings") or []
        series[row.period] = (
            _NOT_APPLICABLE  # DR-001: say why, never a zero or a blank
            if row.metric_value is None and fin.NOT_APPLICABLE_SECTOR in warnings
            else format_metric_value(row.metric_value, unit, metric=name)
        )
        sid = metric_source_id(row)
        source_ids[row.period] = sid
        evidence.append(from_metric(row))
    block: dict[str, Any] = {"unit": unit, "series": series, "source_ids": source_ids}
    if name in VALUATION_METRICS:  # the multiple at the latest close, next to its history
        latest = metric_repo.get_latest_metrics(session, company_id=company_id, metric_names=[name])
        if latest and latest[0].period_type == LATEST:
            row = latest[0]
            block["latest_close"] = {
                "as_of": row.period,
                "value": format_metric_value(row.metric_value, row.unit, metric=name),
                "source_id": metric_source_id(row),
            }
            evidence.append(from_metric(row))

    # Deterministic growth (backend math, never the LLM): YoY of the last pair + full-span CAGR.
    valued = [
        (r.fiscal_year, r.metric_value, metric_source_id(r))
        for r in rows
        if r.metric_value is not None
    ]
    if name == "revenue" and len(valued) >= 2:
        company = session.get(Company, company_id)
        calc, calc_ev = _growth_block(valued, company.ticker if company else "")
        if calc:
            block["calculated"] = calc
            evidence.extend(calc_ev)
    return block, evidence


def _growth_block(
    valued: list[tuple[int | None, Decimal, str]], ticker: str
) -> tuple[dict[str, Any], list[EvidenceItem]]:
    (prior_fy, prior, prior_sid), (last_fy, last, last_sid) = valued[-2], valued[-1]
    (first_fy, first, first_sid) = valued[0]
    years = (last_fy or 0) - (first_fy or 0)
    yoy = fin.yoy_growth(last, prior)
    out: dict[str, Any] = {}
    evidence: list[EvidenceItem] = []
    if yoy.value is not None:
        out["yoy"] = {
            "value": format_metric_value(yoy.value, "ratio"),
            "source_ids": [last_sid, prior_sid],
        }
        evidence.append(
            _derived_ev(
                f"derived:{ticker}:revenue_yoy:FY{prior_fy}-FY{last_fy}",
                "yoy_growth",
                yoy.value,
                period=f"FY{last_fy}",
                inputs=[prior_sid, last_sid],
            )
        )
    if years >= 2:
        cagr = fin.cagr(first, last, years)
        if cagr.value is not None:
            out["cagr"] = {
                "value": format_metric_value(cagr.value, "ratio"),
                "years": years,
                "source_ids": [first_sid, last_sid],
            }
            evidence.append(
                _derived_ev(
                    f"derived:{ticker}:revenue_cagr:FY{first_fy}-FY{last_fy}",
                    "cagr",
                    cagr.value,
                    period=f"FY{last_fy}",
                    inputs=[first_sid, last_sid],
                )
            )
    return out, evidence


def _derived_ev(
    source_id: str, formula_id: str, value: Decimal, *, period: str, inputs: list[str]
) -> EvidenceItem:
    from app.schemas.sources import DerivedMetricSource

    return EvidenceItem(
        source=DerivedMetricSource(
            source_id=source_id,
            tier=1,
            formula_id=formula_id,
            formula_version="v1",
            input_source_ids=inputs,
            value=value,
            period=period,
        ),
        text=f"{metric_label(formula_id)} was {format_metric_value(value, 'ratio')}",
        retrieval_score=0.9,
    )


def build_payload(
    session: Session, company_id: int, section: str
) -> tuple[dict[str, Any], list[EvidenceItem]]:
    """Return (financial JSON payload, metric evidence items) for a section. Empty if no metrics."""
    names = SECTION_METRICS.get(section, [])
    metrics: dict[str, Any] = {}
    evidence: list[EvidenceItem] = []
    for name in names:
        block, ev = _series_block(session, company_id, name)
        if block:
            metrics[name] = block
            evidence.extend(ev)
    return ({"metrics": metrics} if metrics else {}), evidence

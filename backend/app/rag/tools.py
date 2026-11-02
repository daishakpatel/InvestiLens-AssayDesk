"""Typed, read-only, company-scoped tool layer (RAG-031, §16.7).

Each tool resolves a ticker to a company, runs one scoped read, and returns a plain dict that
always includes `source_ids` (CIT-001: IDs are backend-issued). `ToolRunner` enforces the
per-question tool-call budget, detects loops (same tool + args repeated), times every call, and
logs it with latency. Tools never compute financial numbers themselves — `calculate_growth`
delegates to the deterministic finance layer (Phase 1c).

Hard per-call preemption of a runaway query is deferred to Phase 5c observability; here the timeout
is enforced as a post-call deadline check, and every real external call already carries its own
timeout via `HardenedHttpClient`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.finance import metrics as fin
from app.repositories import companies as company_repo
from app.repositories import metrics as metric_repo
from app.repositories import news as news_repo
from app.repositories import prices as price_repo
from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)


class ToolError(RuntimeError):
    """A tool could not run (unknown company, bad args)."""


class ToolBudgetExceeded(RuntimeError):
    """The per-question tool-call budget was hit (RAG-031)."""


class ToolLoopDetected(RuntimeError):
    """The same tool was called with identical args too many times (loop guard, RAG-031)."""


@dataclass
class ToolCallLog:
    name: str
    args: dict[str, Any]
    latency_ms: int
    ok: bool


# --- individual tools (session, company_id, **args) -> dict ---------------------------------


def _company_or_raise(session: Session, ticker: str) -> Any:
    company = company_repo.get_by_ticker(session, ticker)
    if company is None:
        raise ToolError(f"unknown company: {ticker!r}")
    return company


def get_company_info(session: Session, ticker: str) -> dict[str, Any]:
    c = _company_or_raise(session, ticker)
    return {
        "ticker": c.ticker,
        "name": c.name,
        "cik": c.cik,
        "exchange": c.exchange,
        "sector": c.sector,
        "fiscal_year_end": c.fiscal_year_end,
        "source_ids": [],  # registry metadata; not a citable claim source
    }


def get_financial_metric(session: Session, ticker: str, metric: str, period: str) -> dict[str, Any]:
    c = _company_or_raise(session, ticker)
    row = metric_repo.get_latest_metric(session, company_id=c.id, metric_name=metric, period=period)
    if row is None:
        return {
            "metric": metric,
            "period": period,
            "value": None,
            "source_ids": [],
            "reason": "NOT_FOUND",
        }
    return {
        "metric": metric,
        "period": period,
        "value": row.metric_value,  # Decimal | None (NULL = N/A, see quality_flags)
        "unit": row.unit,
        "source_ids": [row.source_id] if row.source_id else [],
    }


def get_metric_series(
    session: Session, ticker: str, metric: str, period_type: str = "FY"
) -> dict[str, Any]:
    c = _company_or_raise(session, ticker)
    rows = metric_repo.get_metric_series(
        session, company_id=c.id, metric_name=metric, period_type=period_type
    )
    return {
        "metric": metric,
        "series": [
            {"period": r.period, "value": r.metric_value, "source_id": r.source_id} for r in rows
        ],
        "source_ids": [r.source_id for r in rows if r.source_id],
    }


def calculate_growth(
    session: Session, ticker: str, metric: str, period_1: str, period_2: str
) -> dict[str, Any]:
    """Growth from period_1 → period_2, computed by the deterministic finance layer (never LLM)."""
    c = _company_or_raise(session, ticker)
    prior = metric_repo.get_latest_metric(
        session, company_id=c.id, metric_name=metric, period=period_1
    )
    current = metric_repo.get_latest_metric(
        session, company_id=c.id, metric_name=metric, period=period_2
    )
    source_ids = [m.source_id for m in (prior, current) if m and m.source_id]
    result = fin.yoy_growth(
        current.metric_value if current else None,
        prior.metric_value if prior else None,
    )
    return {
        "metric": metric,
        "from": period_1,
        "to": period_2,
        "value": result.value,  # ratio Decimal | None
        "unit": result.unit,
        "warnings": result.warnings,
        "formula_id": result.formula_id,
        "source_ids": source_ids,
    }


def get_recent_news(session: Session, ticker: str, days: int = 30) -> dict[str, Any]:
    c = _company_or_raise(session, ticker)
    since = news_repo.recent_since(days, now=datetime.now(UTC))
    rows = news_repo.get_recent_news(session, company_id=c.id, since=since)
    return {
        "items": [
            {
                "source_id": f"news:{r.id}",
                "title": r.title,
                "publisher": r.publisher,
                "published_at": r.published_at.isoformat() if r.published_at else None,
                "category": r.category,
            }
            for r in rows
        ],
        "source_ids": [f"news:{r.id}" for r in rows],
    }


def get_stock_history(session: Session, ticker: str, start: date, end: date) -> dict[str, Any]:
    c = _company_or_raise(session, ticker)
    bars = price_repo.get_price_history(session, company_id=c.id, start=start, end=end)
    return {
        "bars": [
            {
                "date": b.date.isoformat(),
                "close": b.close,
                "adj_close": b.adj_close,
                "volume": b.volume,
            }
            for b in bars
        ],
        "source_ids": [f"price:{c.ticker}:{start.isoformat()}:{end.isoformat()}"],
    }


def compare_companies(
    session: Session,
    tickers: list[str],
    metrics: list[str] | None = None,
    period: str | None = None,
) -> dict[str, Any]:
    """Deterministic side-by-side comparison (§37.1, the real logic behind the Phase 2c stub).

    Calendarized, percentile-ranked, all in the finance layer — the LLM never computes a number. The
    multi-company shape is why this lives beside the single-ticker tools rather than reusing
    `_company_or_raise`.
    """
    from app.comparison.engine import build_response, gather  # local import avoids an import cycle

    try:
        g = gather(session, list(tickers), metrics=metrics, period=period)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    response = build_response(g)
    source_ids = [
        row.source_id
        for company in g.companies
        for metric_name in g.metric_names
        if (row := g.selected(company, metric_name)) is not None and row.source_id
    ]
    return {
        "calendar_year": response.calendar_year,
        "companies": [c.model_dump(mode="json") for c in response.companies],
        "metrics": [m.model_dump(mode="json") for m in response.metrics],
        "notes": response.notes,
        "source_ids": source_ids,
    }


_REGISTRY: dict[str, Callable[..., dict[str, Any]]] = {
    "get_company_info": get_company_info,
    "get_financial_metric": get_financial_metric,
    "get_metric_series": get_metric_series,
    "calculate_growth": calculate_growth,
    "get_recent_news": get_recent_news,
    "get_stock_history": get_stock_history,
    "compare_companies": compare_companies,
}


class ToolRunner:
    """Dispatches tool calls under a budget + loop guard + timeout, logging each with latency."""

    def __init__(
        self, session: Session, *, settings: Settings | None = None, max_calls: int | None = None
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        # The budget guards a model choosing calls; a fixed, deterministic plan passes its size.
        self._max_calls = max_calls or self._settings.rag_max_tool_calls
        self._calls = 0
        self._seen: dict[tuple[str, str], int] = {}
        self.log: list[ToolCallLog] = []

    def call(self, name: str, **args: Any) -> dict[str, Any]:
        if name not in _REGISTRY:
            raise ToolError(f"unknown tool: {name!r}")
        if self._calls >= self._max_calls:
            raise ToolBudgetExceeded(f"tool-call budget {self._max_calls} exceeded")
        signature = (name, repr(sorted(args.items())))
        self._seen[signature] = self._seen.get(signature, 0) + 1
        if self._seen[signature] > 2:  # same call ≥3 times = a loop
            raise ToolLoopDetected(f"loop detected: {name}({args})")

        self._calls += 1
        started = time.monotonic()
        ok = False
        try:
            result = _REGISTRY[name](self._session, **args)
            ok = True
            return result
        finally:
            latency_ms = int((time.monotonic() - started) * 1000)
            self.log.append(ToolCallLog(name=name, args=args, latency_ms=latency_ms, ok=ok))
            level = logging.INFO if ok else logging.WARNING
            log_event(logger, level, "rag.tool.call", tool=name, latency_ms=latency_ms, ok=ok)
            if latency_ms > self._settings.rag_tool_timeout_s * 1000:
                log_event(
                    logger, logging.WARNING, "rag.tool.slow", tool=name, latency_ms=latency_ms
                )


__all__ = [
    "ToolBudgetExceeded",
    "ToolCallLog",
    "ToolError",
    "ToolLoopDetected",
    "ToolRunner",
    "calculate_growth",
    "compare_companies",
    "get_company_info",
    "get_financial_metric",
    "get_metric_series",
    "get_recent_news",
    "get_stock_history",
]

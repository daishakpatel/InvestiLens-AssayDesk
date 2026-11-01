"""Report-section retrieval scope (ADR-0053; RAG-013 metadata filtering, RAG-014 recency)."""

from __future__ import annotations

from typing import Any

import pytest

from app.config import get_settings
from app.rag.retrieval import RetrievalOutcome
from app.rag.types import Evidence
from app.research import report
from app.schemas.sources import TextChunkSource


def _ev(text: str) -> Evidence:
    source = TextChunkSource(source_id=f"1_7_p{len(text)}", tier=1, document_id="1")
    return Evidence(1, 1, source, text, "Item 7", 1, "10-K", None, None, None)


def test_section_scope_with_unscoped_fallback_and_no_disclaimers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []

    def fake(_session: object, **kw: Any) -> RetrievalOutcome:
        calls.append(kw.get("scope"))
        if kw.get("scope") is not None:
            return RetrievalOutcome([], False, 0.0)  # sections not labelled: nothing in scope
        found = [
            _ev("This report contains forward-looking statements within the meaning of the Act."),
            _ev("Revenue rose 62% on data-center demand."),
        ]
        return RetrievalOutcome(found, True, 0.9)

    monkeypatch.setattr(report, "retrieve_evidence", fake)
    items = report._retrieve(None, 1, "risks", settings=get_settings())  # type: ignore[arg-type]
    assert calls[0].section_keys == {"10k_item1a", "10q_part2_item1a"} and calls[1] is None
    assert [i.text for i in items] == ["Revenue rose 62% on data-center demand."]


def test_every_report_section_has_a_scope() -> None:
    assert set(report._SECTION_SCOPE) == set(report._SECTION_QUERY)

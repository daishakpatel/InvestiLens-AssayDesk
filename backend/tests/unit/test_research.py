"""Research section-generator + payload unit tests (Phase 3b). Offline, fake LLM.

Refs: §10, §17.2, CIT-005 (verification gates every claim), FR-020 (every claim cited).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.citation.evidence import EvidenceItem
from app.providers.base import LLMMessage
from app.research.generator import accepted_by_index, generate_section
from app.schemas.sources import TextChunkSource
from tests.fakes import FakeLLM


def _ev(sid: str, text: str, *, tier: int = 1) -> EvidenceItem:
    return EvidenceItem(
        source=TextChunkSource(source_id=sid, tier=tier, document_id="d"), text=text
    )


def test_section_generates_verified_claims() -> None:
    ev = [_ev("c1", "Revenue grew on strong data center demand during the period.")]
    gen = generate_section(FakeLLM(), section="revenue_analysis", payload={}, evidence=ev)
    assert not gen.insufficient
    accepted = accepted_by_index(gen.verified)
    assert accepted
    # Every accepted claim carries a verified citation (FR-020).
    assert all(vc.source_ids == ["c1"] for vc in accepted.values())
    assert all(vc.status in ("accepted", "softened") for vc in accepted.values())


def test_claim_citing_absent_evidence_is_dropped() -> None:
    # No evidence → fake returns {} → section insufficient, no claims leak through.
    gen = generate_section(FakeLLM(), section="revenue_analysis", payload={}, evidence=[])
    assert gen.insufficient
    assert accepted_by_index(gen.verified) == {}


def test_risk_section_shape() -> None:
    ev = [_ev("r1", "Supply chain depends on a limited number of foundry partners in Taiwan.")]
    gen = generate_section(FakeLLM(), section="risks", payload={}, evidence=ev)
    assert gen.items and gen.items[0].meta.get("category") == "supply_chain"
    assert not gen.insufficient


def test_malformed_section_is_insufficient_not_crash() -> None:
    ev = [_ev("c1", "Revenue grew on strong data center demand.")]
    gen = generate_section(
        FakeLLM(broken={"claims"}), section="revenue_analysis", payload={}, evidence=ev
    )
    assert gen.insufficient  # malformed output → empty, not an exception
    assert accepted_by_index(gen.verified) == {}


class _NewsLLM(FakeLLM):
    def __init__(self, *summaries: dict[str, Any]) -> None:
        super().__init__()
        self._summaries = list(summaries)

    async def complete_json(
        self, messages: Sequence[LLMMessage], *, model: str, schema: dict[str, Any], max_tokens: int
    ) -> dict[str, Any]:
        return {"summaries": self._summaries}


def test_inline_marker_is_not_read_as_a_figure() -> None:
    """CIT-001, CIT-005: 120 of 120 real news claims failed on the ID inside `[news:4009]`."""
    text = "The Treasury market suffered a historic sell-off, offering high yields"
    ev = [_ev("news:4009", f"{text}.", tier=4)]
    llm = _NewsLLM(
        {"news_id": "4009", "summary": f"{text} [news:4009].", "source_ids": ["news:4009"]},
        {"news_id": "4009", "summary": f"{text} [source_id=news:4009].", "source_ids": []},
    )
    gen = generate_section(llm, section="news_summary", payload={}, evidence=ev)
    accepted = accepted_by_index(gen.verified)
    assert sorted(accepted) == [0, 1]
    for claim in accepted.values():
        assert claim.text == f"{text}."
        assert claim.source_ids == ["news:4009"]


def test_inline_marker_for_absent_evidence_is_still_rejected() -> None:
    """CIT-001: a marker is only syntax; its ID is checked like any other."""
    text = "The Treasury market suffered a historic sell-off"
    ev = [_ev("news:4009", f"{text}.", tier=4)]
    llm = _NewsLLM({"news_id": "1", "summary": f"{text} [news:1].", "source_ids": []})
    gen = generate_section(llm, section="news_summary", payload={}, evidence=ev)
    assert gen.insufficient
    assert accepted_by_index(gen.verified) == {}


def test_marker_only_text_keeps_later_claims_aligned() -> None:
    """A dropped empty claim must not shift the index that maps claims back to section items."""
    text = "The Treasury market suffered a historic sell-off"
    ev = [_ev("news:4009", f"{text}.", tier=4)]
    llm = _NewsLLM(
        {"news_id": "empty", "summary": "[news:4009]", "source_ids": []},
        {"news_id": "real", "summary": f"{text} [news:4009].", "source_ids": []},
    )
    gen = generate_section(llm, section="news_summary", payload={}, evidence=ev)
    accepted = accepted_by_index(gen.verified)
    assert list(accepted) == [1]
    assert gen.items[1].meta["news_id"] == "real"


def test_factor_section_groups_claims() -> None:
    ev = [_ev("c1", "Demand for AI infrastructure remained strong across segments.")]
    gen = generate_section(FakeLLM(), section="bull_factors", payload={}, evidence=ev)
    assert gen.items and gen.items[0].meta.get("title") == "Potential upside"
    assert gen.items[0].meta.get("factor_idx") == 0

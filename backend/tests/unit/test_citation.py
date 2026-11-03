"""Citation verification + rendering unit tests (Phase 3a). Offline, deterministic.

Refs: CIT-001/004/005/006, HAL-002, §15 (tiers).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.citation.confidence import score_claim
from app.citation.evidence import EvidenceItem, index_evidence
from app.citation.extract import extract_numbers, has_factual_content, split_claims, strip_markers
from app.citation.pipeline import verify_text
from app.citation.tiers import tier_for_source_type, tier_score
from app.schemas.sources import DerivedMetricSource, TextChunkSource


def _text_ev(sid: str, text: str, *, tier: int = 1, score: float = 0.8) -> EvidenceItem:
    return EvidenceItem(
        source=TextChunkSource(source_id=sid, tier=tier, document_id="doc1", text=text),
        text=text,
        retrieval_score=score,
    )


def _derived_ev(sid: str, value: str) -> EvidenceItem:
    return EvidenceItem(
        source=DerivedMetricSource(
            source_id=sid,
            tier=1,
            formula_id="yoy_growth",
            formula_version="v1",
            value=Decimal(value),
            period="FY2025",
        ),
        text="",
    )


# --- marker parsing & extraction (CIT-001, CIT-004, CIT-005 L2) ---


def test_strip_markers_collects_ids_in_order() -> None:
    cleaned, ids = strip_markers("Revenue rose [SOURCE:a] and margin fell [SOURCE:b].")
    assert ids == ["a", "b"]
    assert "[SOURCE" not in cleaned and cleaned.startswith("Revenue")


@pytest.mark.parametrize(
    ("marker", "ids"),
    [
        ("[news:4009]", ["news:4009"]),
        ("[source_id=news:4009]", ["news:4009"]),
        ("[source_id=1_2_3, source_id=1_2_4]", ["1_2_3", "1_2_4"]),
        ("[cite: a; b]", ["a", "b"]),
        ("[news:1, news:2]", ["news:1", "news:2"]),
    ],
)
def test_strip_markers_reads_the_forms_models_write(marker: str, ids: list[str]) -> None:
    """CIT-001: real report claims carried these inside JSON text (2026-11-01)."""
    cleaned, found = strip_markers(f"Treasuries sold off {marker}.")
    assert found == ids
    assert cleaned == "Treasuries sold off."


def test_strip_markers_leaves_ordinary_brackets() -> None:
    text = "Sales [excluding news: see note] rose in [2026]."
    assert strip_markers(text) == (text, [])


def test_split_claims_per_sentence() -> None:
    claims = split_claims("Revenue grew 10% [SOURCE:a]. Margins were stable [SOURCE:b].")
    assert len(claims) == 2
    assert claims[0].source_ids == ["a"] and claims[1].source_ids == ["b"]


def test_extract_numbers_kinds() -> None:
    kinds = {t.kind for t in extract_numbers("Revenue of $60,922 million rose 114% on 2025-01-31")}
    assert {"money", "percent", "date"} <= kinds
    money = next(t for t in extract_numbers("$60,922 million") if t.kind == "money")
    assert money.value == Decimal("60922000000")


# --- DoD: unknown source id rejected (CIT-A1) ---


def test_unknown_source_rejected() -> None:
    ev = index_evidence([_text_ev("known", "Revenue rose.")])
    out = verify_text("Revenue rose [SOURCE:ghost].", ev)
    assert out.claims[0].status == "rejected"
    assert out.claims[0].reason_code == "UNKNOWN_SOURCE"
    assert out.citations == []  # nothing rendered → no dangling citation


# --- DoD: number not in/derivable from source rejected ---


def test_numeric_mismatch_rejected() -> None:
    ev = index_evidence([_text_ev("a", "Revenue was $60,922 million for fiscal 2025.")])
    out = verify_text("Revenue was $99,999 million [SOURCE:a].", ev)
    assert out.claims[0].reason_code == "NUMERIC_MISMATCH"


def test_numeric_match_accepts() -> None:
    ev = index_evidence([_text_ev("a", "Revenue was $60,922 million for fiscal 2025.")])
    out = verify_text("Revenue reached $60,922 million [SOURCE:a].", ev)
    assert out.claims[0].status in {"accepted", "softened"}


def test_percent_matches_derived_ratio() -> None:
    # "grew 114%" backed by a derived_metric whose value is the ratio 1.14 (CIT-005 L2 + L3).
    ev = index_evidence([_derived_ev("d", "1.14")])
    out = verify_text("Revenue grew 114% [SOURCE:d].", ev)
    assert out.claims[0].status == "accepted"
    assert out.claims[0].numeric_match is True


# --- DoD: unsupported causal statement rejected or downgraded (CIT-005 L4) ---


def test_causal_overreach_softened() -> None:
    # Source has no causal language → causal claim is softened to correlation ("amid").
    ev = index_evidence([_text_ev("a", "Revenue increased. Data center demand was strong.")])
    out = verify_text("Revenue increased due to data center demand [SOURCE:a].", ev)
    claim = out.claims[0]
    assert claim.status == "softened" and claim.reason_code == "OVERREACH"
    assert "due to" not in claim.text and "amid" in claim.text


def test_causal_accepted_when_source_is_causal() -> None:
    ev = index_evidence([_text_ev("a", "Revenue increased primarily due to data center demand.")])
    out = verify_text("Revenue increased due to data center demand [SOURCE:a].", ev)
    assert out.claims[0].status == "accepted"


def test_unsupported_claim_rejected() -> None:
    ev = index_evidence([_text_ev("a", "The company operates three data center regions.")])
    out = verify_text("Litigation reserves rose sharply this quarter [SOURCE:a].", ev)
    assert out.claims[0].reason_code == "UNSUPPORTED"


# --- DoD: coverage — factual sentence with no citation rejected (CIT-005 L5) ---


def test_no_citation_factual_rejected() -> None:
    out = verify_text("Revenue grew 50% last year.", index_evidence([]))
    assert out.claims[0].reason_code == "NO_CITATION"


def test_non_factual_sentence_accepted_without_citation() -> None:
    out = verify_text("This section reviews the company.", index_evidence([]))
    assert out.claims[0].status == "accepted" and out.claims[0].reason_code == "OK"


# --- DoD: rendering — sequential numbers + reference list (CIT-006) ---


def test_rendering_numbers_in_first_appearance_order() -> None:
    ev = index_evidence(
        [_text_ev("a", "Revenue was strong."), _text_ev("b", "Margins expanded nicely.")]
    )
    out = verify_text("Revenue was strong [SOURCE:a]. Margins expanded [SOURCE:b].", ev)
    assert "[1]" in out.rendered_text and "[2]" in out.rendered_text
    assert [c.number for c in out.citations] == [1, 2]
    assert [c.source_id for c in out.citations] == ["a", "b"]


def test_rejected_claims_excluded_from_render() -> None:
    ev = index_evidence([_text_ev("a", "Revenue was strong.")])
    out = verify_text("Revenue was strong [SOURCE:a]. Profit tripled [SOURCE:ghost].", ev)
    assert out.rendered_text.count("[1]") == 1
    assert len(out.citations) == 1  # ghost claim dropped


def test_all_factual_rejected_is_insufficient() -> None:
    out = verify_text("Revenue grew 50% [SOURCE:ghost].", index_evidence([]))
    assert out.sufficient is False


# --- tiers (§15) ---


def test_tier_lookup_and_score() -> None:
    assert tier_for_source_type("text_chunk") == 1
    assert tier_for_source_type("news_item") == 4
    assert tier_for_source_type("mystery") == 5
    assert tier_score(1) == 1.0 and tier_score(5) == 0.2


# --- confidence calibration (HAL-002 spirit) ---


def test_confidence_strong_vs_limited() -> None:
    strong = [
        _text_ev("a", "Revenue grew on data center demand", tier=1, score=0.9),
        _text_ev("b", "Data center revenue grew strongly", tier=1, score=0.9),
    ]
    _, strong_label = score_claim(strong, "supported")
    assert strong_label == "strongly_supported"

    weak = [EvidenceItem(source=TextChunkSource(source_id="n", tier=5, document_id="d"), text="x")]
    _, weak_label = score_claim(weak, "partially")
    assert weak_label == "limited_evidence"


def test_plural_finance_terms_make_a_sentence_factual() -> None:  # CIT-005 L5
    # "risks" and "margins" are as factual as "risk" and "margin". Missing the plural let a cited
    # sentence count as connective, so an answer whose only other sentence was uncited abstained.
    assert has_factual_content("The excerpts discuss several distinct risks, including cyber.")
    assert has_factual_content("Margins improved across the period.")
    assert not has_factual_content("This section covers the following topics.")

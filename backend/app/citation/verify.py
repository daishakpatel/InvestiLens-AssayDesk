"""The five-layer claim verifier (CIT-005).

For every generated claim, in order: ID validation → numeric match → entailment → scope (causal
over-reach) → coverage. The first failing layer rejects the claim with a reason code; survivors are
scored (HAL-002) and marked accepted (or softened). This module is deterministic and offline (the
entailer is pluggable, ADR-0015); it never calls an LLM itself.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal

from app.citation.advice import accuses_on_screening_score
from app.citation.confidence import score_claim
from app.citation.entailment import Entailer, get_entailer
from app.citation.evidence import EvidenceItem
from app.citation.extract import (
    NumberToken,
    RawClaim,
    extract_numbers,
    has_factual_content,
    is_causal,
    soften_causal,
)
from app.config import Settings, get_settings
from app.schemas.citations import EntailmentLabel, VerifiedClaim

_TABLE_SCALE = re.compile(r"\bin (thousands|millions|billions)\b", re.IGNORECASE)
_TABLE_SCALES = {
    "thousands": Decimal(10) ** 3,
    "millions": Decimal(10) ** 6,
    "billions": Decimal(10) ** 9,
}


def number_matches(token: NumberToken, candidates: Sequence[Decimal], *, rel_tol: float) -> bool:
    """A number matches a candidate within relative tolerance, or (for a percent) a candidate
    ratio times 100. An unparseable token never blocks."""
    if token.value is None:
        return True
    targets = [token.value]
    if token.kind == "percent":  # "grew 114%" may be stored as the ratio 1.14
        targets.append(token.value / Decimal(100))
    for target in targets:
        for cand in candidates:
            if cand == target:
                return True
            scale = max(abs(cand), abs(target), Decimal(1))
            if abs(cand - target) <= scale * Decimal(str(rel_tol)):
                return True
    return False


def _number_supported(token: NumberToken, cited: Sequence[EvidenceItem], *, rel_tol: float) -> bool:
    """A claim number matches if it is within tolerance of a source number, or (for percents)
    of a source ratio times 100 — covering derived_metric/xbrl values (CIT-005 L2).
    """
    if token.kind == "date":
        return any(token.raw in item.searchable_text() for item in cited)
    candidates = [value for item in cited for value in source_values(item)]
    return number_matches(token, candidates, rel_tol=rel_tol)


def source_values(item: EvidenceItem) -> list[Decimal]:
    """Every number a source states: its structured values and the figures in its text, with a
    table's figures also at its stated unit. Shared with the published eval check (NFR-ME-002)."""
    values = list(item.structured_values())
    text = item.searchable_text()
    tokens = [t for t in extract_numbers(text) if t.value is not None]
    values.extend(t.value for t in tokens if t.value is not None)
    # A table "(in millions)" holds 19,827 for a claim's "$19,827 million" or "$19.8 billion".
    for scale in {_TABLE_SCALES[m.group(1).lower()] for m in _TABLE_SCALE.finditer(text)}:
        values.extend(t.value * scale for t in tokens if t.value and t.kind != "percent")
    return values


def _accept(
    claim: RawClaim,
    cited: Sequence[EvidenceItem],
    *,
    text: str,
    status: str,
    entailment: EntailmentLabel,
    numeric_match: bool,
    settings: Settings,
) -> VerifiedClaim:
    score, label = score_claim(cited, entailment, settings=settings)
    return VerifiedClaim(
        claim_id=claim.claim_id,
        text=text,
        source_ids=list(claim.source_ids),
        status=status,  # type: ignore[arg-type]
        reason_code="OVERREACH" if (status == "softened" and text != claim.text) else "OK",
        entailment_label=entailment,
        numeric_match=numeric_match,
        confidence_label=label,
        internal_confidence=score,
    )


def _reject(
    claim: RawClaim, reason: str, *, entailment: EntailmentLabel = "not_checked"
) -> VerifiedClaim:
    return VerifiedClaim(
        claim_id=claim.claim_id,
        text=claim.text,
        source_ids=list(claim.source_ids),
        status="rejected",
        reason_code=reason,  # type: ignore[arg-type]
        entailment_label=entailment,
    )


def verify_claim(
    claim: RawClaim,
    evidence: Mapping[str, EvidenceItem],
    *,
    settings: Settings | None = None,
    entailer: Entailer | None = None,
) -> VerifiedClaim:
    """Run the five layers for one claim and return its verification outcome (CIT-005)."""
    settings = settings or get_settings()
    entailer = entailer or get_entailer(settings)

    # L5 (coverage), checked up front: a factual sentence with no citation is rejected; a
    # non-factual sentence (connective prose) is accepted as-is without a citation.
    if not claim.source_ids:
        if has_factual_content(claim.text):
            return _reject(claim, "NO_CITATION")
        return VerifiedClaim(
            claim_id=claim.claim_id, text=claim.text, status="accepted", reason_code="OK"
        )

    # L1 ID validation: any unknown cited id rejects the whole claim (CIT-A1).
    if any(sid not in evidence for sid in claim.source_ids):
        return _reject(claim, "UNKNOWN_SOURCE")
    cited = [evidence[sid] for sid in claim.source_ids]

    # A screening score is a signal, never a finding: no fraud/manipulation wording on one.
    if accuses_on_screening_score(claim.text, list(claim.source_ids)):
        return _reject(claim, "OVERREACH")

    # L2 numeric match.
    numbers = extract_numbers(claim.text)
    numeric_match = all(
        _number_supported(t, cited, rel_tol=settings.citation_numeric_rel_tolerance)
        for t in numbers
    )
    if not numeric_match:
        return _reject(claim, "NUMERIC_MISMATCH")

    # L3 entailment. A claim backed only by a structured value (xbrl/derived) whose numbers already
    # matched is deterministically supported (HAL-002: calculation confidence = high) — there is no
    # prose to entail. Prose/table sources (or a mix) still go through the entailer.
    has_prose = any((item.text or "").strip() for item in cited)
    has_structured = any(item.structured_values() for item in cited)
    if not has_prose and has_structured:
        label: EntailmentLabel = "supported"
    else:
        label = entailer.check(claim.text, " ".join(item.searchable_text() for item in cited))
    if label == "unsupported":
        return _reject(claim, "UNSUPPORTED", entailment="unsupported")

    # L4 scope: a causal claim needs causal language in a cited source, else over-reach.
    text = claim.text
    status = "accepted"
    if is_causal(claim.text) and not any(is_causal(item.searchable_text()) for item in cited):
        text = soften_causal(claim.text)
        status = "softened"

    # Partial-entailment policy (CIT-005 L3): soften (downgrade) or reject.
    if label == "partially":
        if settings.citation_partial_policy == "reject":
            return _reject(claim, "UNSUPPORTED", entailment="partially")
        status = "softened"

    return _accept(
        claim,
        cited,
        text=text,
        status=status,
        entailment=label,
        numeric_match=numeric_match,
        settings=settings,
    )

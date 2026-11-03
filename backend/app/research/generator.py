"""Section generator core (§17.2): prompt → structured LLM call → Phase 3a verification.

One LLM call per section with only that section's evidence subset. The model returns JSON (provider
structured-output mode); on a parse/shape failure we allow exactly one repair attempt, then the
section is marked insufficient. Whatever comes back is run through the Phase 3a verifier against the
section's evidence set before any claim is kept — the LLM's citations are never trusted as-is.

Every attempt (including the one repair retry) is logged to `llm_calls` (NFR-015, OBS-005) — this
was the one LLM call site in the system that slipped through the Phase 5c observability audit;
chat and embeddings already logged theirs.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.billing.cost import estimate_cost
from app.citation.entailment import Entailer, get_entailer
from app.citation.evidence import EvidenceItem, index_evidence
from app.citation.extract import RawClaim, strip_markers
from app.citation.pipeline import verify_claims
from app.config import Settings, get_settings
from app.ingestion.documents.tokens import estimate_tokens
from app.observability.tracing import span as otel_span
from app.providers.base import LLMClient, LLMMessage
from app.rag.injection import UNTRUSTED_PREAMBLE, wrap_untrusted
from app.repositories import llm_calls as llm_repo
from app.research.prompts import REPORT_PROMPT_VERSION, system_prompt
from app.schemas.citations import VerifiedClaim, VerifiedOutput

_LIST_KEY = {
    "risks": "risks",
    "management_commentary": "statements",
    "news_summary": "summaries",
    "bull_factors": "factors",
    "bear_factors": "factors",
}
_MAX_TOKENS = 1500


@dataclass
class Item:
    """A candidate claim from the LLM: text to verify + cited ids + section metadata."""

    text: str
    source_ids: list[str]
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class SectionGeneration:
    items: list[Item]
    verified: VerifiedOutput
    insufficient: bool


def _context(payload: dict[str, Any], evidence: Sequence[EvidenceItem]) -> str:
    parts = [UNTRUSTED_PREAMBLE]
    if payload:
        parts.append("STRUCTURED METRICS (JSON, already computed — explain, do not recompute):")
        parts.append(json.dumps(payload, default=str))
    parts.append("EVIDENCE (cite by source_id):")
    for item in evidence:
        parts.append(f"[source_id={item.source_id}]\n{wrap_untrusted(item.searchable_text())}")
    return "\n\n".join(parts)


def _schema(section: str) -> dict[str, Any]:
    key = _LIST_KEY.get(section, "claims")
    return {
        "type": "object",
        "properties": {key: {"type": "array", "items": {"type": "object"}}},
        "required": [key],
    }


def _logged_complete_json(
    llm: LLMClient,
    messages: list[LLMMessage],
    *,
    model: str,
    schema: dict[str, Any],
    session: Session | None,
    user_id: int | None,
    section: str,
) -> dict[str, Any]:
    """`llm.complete_json`, logged to `llm_calls` either way (NFR-015) — mirrors chat/embeddings."""
    input_tokens = sum(estimate_tokens(m.content) for m in messages)
    started = time.monotonic()
    status = "success"
    try:
        with otel_span(
            "research.generate_section",
            tracer_name=__name__,
            attributes={"prompt_version": REPORT_PROMPT_VERSION, "section": section},
        ):
            raw = asyncio.run(
                llm.complete_json(messages, model=model, schema=schema, max_tokens=_MAX_TOKENS)
            )
    except Exception:
        status = "error"
        raw = {}
        raise
    finally:
        if session is not None:
            latency_ms = int((time.monotonic() - started) * 1000)
            tokens_in, tokens_out = llm_repo.call_tokens(
                llm,
                input_estimate=input_tokens,
                output_estimate=estimate_tokens(json.dumps(raw)) if status == "success" else None,
            )
            llm_repo.record_call(
                session,
                purpose="report_section",
                model=model,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                latency_ms=latency_ms,
                cost_usd=estimate_cost(
                    model=model, input_tokens=tokens_in, output_tokens=tokens_out or 0
                ),
                status=status,
                prompt_version=REPORT_PROMPT_VERSION,
                user_id=user_id,
            )
    return raw


def _call(
    llm: LLMClient,
    messages: list[LLMMessage],
    schema: dict[str, Any],
    section: str,
    *,
    session: Session | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Call the model; on a shape failure allow exactly one repair attempt (§17.2)."""
    strong = get_settings().llm_model_strong
    try:
        raw = _logged_complete_json(
            llm,
            messages,
            model=strong,
            schema=schema,
            session=session,
            user_id=user_id,
            section=section,
        )
        if _extract(section, raw):
            return raw
    except Exception:  # malformed output is expected and handled by one repair
        raw = {}
    repair = [
        *messages,
        LLMMessage(
            "user",
            f"Your previous reply was invalid. Return ONLY valid "
            f"JSON matching the required shape for {section}.",
        ),
    ]
    try:
        return _logged_complete_json(
            llm,
            repair,
            model=strong,
            schema=schema,
            session=session,
            user_id=user_id,
            section=section,
        )
    except Exception:
        return {}


def _extract(section: str, raw: dict[str, Any]) -> list[Item]:
    """Normalize a section's JSON into uniform items (text + source_ids + metadata)."""
    if not isinstance(raw, dict):
        return []
    if section in ("bull_factors", "bear_factors"):
        items: list[Item] = []
        for fi, factor in enumerate(raw.get("factors", []) or []):
            if not isinstance(factor, dict):
                continue
            meta = {
                "factor_idx": fi,
                "title": factor.get("title", ""),
                "monitoring_indicator": factor.get("monitoring_indicator"),
            }
            for claim in factor.get("claims", []) or []:
                if isinstance(claim, dict) and claim.get("text"):
                    items.append(Item(str(claim["text"]), _ids(claim), dict(meta)))
        return items
    key = _LIST_KEY.get(section, "claims")
    out: list[Item] = []
    for entry in raw.get(key, []) or []:
        if not isinstance(entry, dict):
            continue
        text = entry.get("description") or entry.get("summary") or entry.get("text")
        if not text:
            continue
        out.append(
            Item(
                str(text),
                _ids(entry),
                {
                    k: v
                    for k, v in entry.items()
                    if k not in {"text", "description", "summary", "source_ids"}
                },
            )
        )
    return out


def _ids(entry: dict[str, Any]) -> list[str]:
    raw = entry.get("source_ids", [])
    return [str(s) for s in raw] if isinstance(raw, list) else []


def generate_section(
    llm: LLMClient,
    *,
    section: str,
    payload: dict[str, Any],
    evidence: Sequence[EvidenceItem],
    settings: Settings | None = None,
    entailer: Entailer | None = None,
    session: Session | None = None,
    user_id: int | None = None,
) -> SectionGeneration:
    """Generate + verify one section's items (unmapped). Tolerant of bad LLM output.

    `session`/`user_id`, when given, log every LLM attempt to `llm_calls` (NFR-015) attributed to
    the requesting user (ADR-0022). Both are optional so offline/unit tests that don't need
    persistence (most of this module's existing tests) are unaffected.
    """
    settings = settings or get_settings()
    entailer = entailer or get_entailer(settings)
    ev_index = index_evidence(evidence)

    messages = [
        LLMMessage("system", system_prompt(section)),
        LLMMessage("user", _context(payload, evidence)),
    ]
    raw = _call(llm, messages, _schema(section), section, session=session, user_id=user_id)
    items = _extract(section, raw)

    raw_claims = []
    for i, it in enumerate(items):
        text, marked = strip_markers(it.text)
        if text:  # the index in claim_id maps a claim back to its item, so it is not renumbered
            ids = list(dict.fromkeys([*it.source_ids, *marked]))
            raw_claims.append(RawClaim(claim_id=f"{section}_{i}", text=text, source_ids=ids))
    verified = verify_claims(raw_claims, ev_index, settings=settings, entailer=entailer)
    accepted = [c for c in verified.claims if c.status in ("accepted", "softened")]
    return SectionGeneration(items=items, verified=verified, insufficient=not accepted)


def accepted_by_index(verified: VerifiedOutput) -> dict[int, VerifiedClaim]:
    """Map item index (from the claim_id suffix) → verified claim, for accepted/softened only."""
    out: dict[int, VerifiedClaim] = {}
    for claim in verified.claims:
        if claim.status in ("accepted", "softened"):
            out[int(claim.claim_id.rsplit("_", 1)[1])] = claim
    return out

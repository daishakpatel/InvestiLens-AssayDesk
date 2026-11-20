"""Published evaluation numbers (Phase 7l Part A, NFR-ME-001 … NFR-ME-006).

Turns the eval runner's per-question results into the committed `docs/eval/latest.json` report:
each rate carries its numerator, denominator and a Wilson 95% interval (NFR-ME-003), so a small
sample can never read as a bare "100%". The citation and number checks here run *after* the chat
pipeline, on what a user would see, against the source resolver (`GET /sources`) — they do not
re-run the verifier being measured.

Also owns the CI gate (NFR-ME-004): thresholds from `docs/eval/thresholds.json`, and a freshness
check that the report was produced from the current content of the code that shapes answers.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.citation.evidence import EvidenceItem
from app.citation.extract import extract_numbers
from app.citation.resolver import resolve_source
from app.citation.verify import number_matches, source_values

_Z95 = 1.959963984540054
_MARKER = re.compile(r"\[(\d+)\]")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_QUOTED = re.compile(r"[\"“]([^\"”]{8,})[\"”]")

# Paths whose content shapes an answer or its verification. A report produced from different
# content is stale and fails the gate (NFR-ME-004). Repo-relative; directories are walked.
WATCHED_PATHS = (
    "backend/app/chat",
    "backend/app/citation",
    "backend/app/rag",
    "backend/app/research/prompts.py",
    "backend/app/eval",
    "backend/tests/eval/golden_v0.jsonl",
)

DEFINITIONS = {
    "citation_validity": "Share of citations on answered questions whose source ID resolves "
    "through the source resolver and whose quoted text, if any, is a verbatim substring of it.",
    "number_match": "Share of numbers in answered text that match a value in the sentence's "
    "cited sources within the verifier's relative tolerance.",
    "correct_abstention": "Share of questions that must be declined (advice, forecasts, unknown "
    "companies, injections) that the system declined.",
    "false_abstention": "Share of answerable questions that the system declined (lower is "
    "better). Reported beside correct abstention because either alone hides the trade-off.",
    "retrieval_hit_at_5": "Share of retrieval questions with at least one expected document in "
    "the top 5 retrieved documents.",
    "retrieval_mrr": "Mean reciprocal rank of the first expected document (no interval: not a "
    "proportion).",
    "injection_resistance": "Share of prompt-injection inputs (golden-set injections plus the "
    "security corpus, asked as questions) that were declined or answered without complying.",
}


@dataclass(frozen=True)
class Rate:
    """A proportion with its Wilson 95% interval."""

    successes: int
    n: int

    @property
    def value(self) -> float | None:
        return self.successes / self.n if self.n else None

    def interval(self) -> tuple[float, float] | None:
        return wilson_interval(self.successes, self.n)

    def as_dict(self) -> dict[str, Any]:
        ci = self.interval()
        return {
            "value": _round(self.value),
            "successes": self.successes,
            "n": self.n,
            "ci95": [_round(ci[0]), _round(ci[1])] if ci else None,
        }


def _round(x: float | None) -> float | None:
    return None if x is None else round(x, 4)


def wilson_interval(successes: int, n: int, z: float = _Z95) -> tuple[float, float] | None:
    """Wilson score interval; None when n is 0. Stays inside [0, 1] even at 0/n and n/n."""
    if n <= 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (q in (0, 100]); None for no samples."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


# --- per-answer checks ---------------------------------------------------------------------


def _sentences_with_markers(answer: str) -> list[tuple[str, list[int]]]:
    out: list[tuple[str, list[int]]] = []
    for sentence in _SENTENCE.split(answer.strip()):
        numbers = [int(n) for n in _MARKER.findall(sentence)]
        out.append((_MARKER.sub("", sentence).strip(), numbers))
    return out


def check_answer(
    session: Session,
    answer: str,
    citations: Iterable[tuple[int, str]],
    *,
    rel_tol: float,
) -> dict[str, int]:
    """Citation validity and number match for one rendered answer.

    `citations` are (display number, source_id). A citation is valid when its source resolves
    and every quoted span in a sentence citing it occurs verbatim in the source text. A number
    matches when it is within `rel_tol` of a value in that sentence's cited sources.
    """
    by_number = dict(citations)
    resolved: dict[str, EvidenceItem | None] = {}
    for sid in by_number.values():
        detail = resolve_source(session, sid)
        resolved[sid] = (
            EvidenceItem(source=detail.source, text=detail.text or "") if detail else None
        )

    valid = total = matched = numbers = 0
    for text, marks in _sentences_with_markers(answer):
        items = [resolved.get(by_number[m]) for m in marks if m in by_number]
        quotes = _QUOTED.findall(text)
        for item in items:
            total += 1
            if item is not None and all(q in item.searchable_text() for q in quotes):
                valid += 1
        candidates = [v for item in items if item is not None for v in source_values(item)]
        for token in extract_numbers(text):
            if token.kind == "date":
                continue
            numbers += 1
            if items and number_matches(token, candidates, rel_tol=rel_tol):
                matched += 1
    return {
        "citations_valid": valid,
        "citations_total": total,
        "numbers_matched": matched,
        "numbers_total": numbers,
    }


# --- aggregation ---------------------------------------------------------------------------


def _count(rows: Iterable[dict[str, Any]], key: str) -> int:
    return sum(int(r.get(key, 0)) for r in rows)


def published_metrics(
    per_question: Sequence[dict[str, Any]],
    *,
    injection_outcomes: Sequence[bool],
) -> dict[str, Any]:
    """The NFR-ME-002 metric block from runner results (+ the injection outcomes)."""
    must = [r for r in per_question if r["must_abstain"]]
    answerable = [r for r in per_question if not r["must_abstain"]]
    retrieval = [r["retrieval"] for r in per_question if r.get("retrieval")]
    mrr = [float(x["mrr"]) for x in retrieval]
    return {
        "citation_validity": Rate(
            _count(per_question, "citations_valid"), _count(per_question, "citations_total")
        ).as_dict(),
        "number_match": Rate(
            _count(per_question, "numbers_matched"), _count(per_question, "numbers_total")
        ).as_dict(),
        "correct_abstention": Rate(sum(1 for r in must if not r["answered"]), len(must)).as_dict(),
        "false_abstention": Rate(
            sum(1 for r in answerable if not r["answered"]), len(answerable)
        ).as_dict(),
        "retrieval_hit_at_5": Rate(
            sum(1 for x in retrieval if float(x.get("recall_at_5", 0)) > 0), len(retrieval)
        ).as_dict(),
        "retrieval_mrr": {"value": _round(sum(mrr) / len(mrr)) if mrr else None, "n": len(mrr)},
        "injection_resistance": Rate(
            sum(1 for ok in injection_outcomes if ok), len(injection_outcomes)
        ).as_dict(),
    }


def latency_block(samples_ms: Sequence[float]) -> dict[str, Any]:
    return {
        "p50_ms": percentile(samples_ms, 50),
        "p95_ms": percentile(samples_ms, 95),
        "n": len(samples_ms),
    }


# --- gate (NFR-ME-004) ---------------------------------------------------------------------


def _watched_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for rel in WATCHED_PATHS:
        path = root / rel
        if path.is_dir():
            files.extend(p for p in path.rglob("*") if p.is_file() and p.suffix in {".py", ".json"})
        elif path.is_file():
            files.append(path)
    return sorted(files)


def inputs_hash(root: Path) -> str:
    """SHA-256 over the watched files' paths and bytes. Independent of commit order, so a report
    committed alongside the code it measured stays fresh."""
    digest = hashlib.sha256()
    for path in _watched_files(root):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def gate_failures(report: dict[str, Any], thresholds: dict[str, Any], root: Path) -> list[str]:
    """Reasons the committed report fails CI: a metric under its floor, or a stale report."""
    failures: list[str] = []
    metrics = report.get("metrics", {})
    for name, floor in thresholds.get("min", {}).items():
        value = metrics.get(name, {}).get("value")
        if value is None or value < floor:
            failures.append(f"{name} = {value} is below the threshold {floor}")
    for name, ceiling in thresholds.get("max", {}).items():
        value = metrics.get(name, {}).get("value")
        if value is None or value > ceiling:
            failures.append(f"{name} = {value} is above the threshold {ceiling}")
    if report.get("inputs_hash") != inputs_hash(root):
        failures.append(
            "docs/eval/latest.json is stale: chat, citation, retrieval, prompt or golden-set "
            "code changed since it was generated; run `make eval-report`"
        )
    return failures

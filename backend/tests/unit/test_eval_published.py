"""Published eval numbers and their CI gate (Phase 7l, NFR-ME-002 … NFR-ME-004)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import app.eval.published as published
from app.eval.published import (
    Rate,
    check_answer,
    gate_failures,
    inputs_hash,
    percentile,
    published_metrics,
    wilson_interval,
)
from app.schemas.citations import SourceDetail
from app.schemas.sources import DerivedMetricSource, TextChunkSource

_ROOT = Path(__file__).resolve().parents[3]


# --- NFR-ME-003: Wilson 95% intervals ------------------------------------------------------
def test_wilson_matches_reference_values() -> None:
    low, high = wilson_interval(8, 10) or (0.0, 0.0)
    assert (round(low, 4), round(high, 4)) == (0.4902, 0.9433)  # textbook 8/10 interval
    assert wilson_interval(0, 0) is None


def test_a_perfect_small_sample_never_reads_as_certain() -> None:
    rate = Rate(6, 6).as_dict()
    assert rate["value"] == 1.0
    assert rate["ci95"][0] < 0.65 and rate["ci95"][1] == 1.0  # 6/6 still spans about 61% to 100%
    zero = wilson_interval(0, 5)
    assert zero is not None and zero[0] == 0.0 and round(zero[1], 4) == 0.4345


def test_percentile_is_nearest_rank() -> None:
    samples = [float(x) for x in range(1, 101)]
    assert percentile(samples, 50) == 50.0
    assert percentile(samples, 95) == 95.0
    assert percentile([], 50) is None


# --- NFR-ME-002: both abstention rates, from runner rows -----------------------------------
def _row(*, must: bool, answered: bool, **extra: Any) -> dict[str, Any]:
    return {"must_abstain": must, "answered": answered, **extra}


def test_correct_and_false_abstention_are_both_reported() -> None:
    rows = [
        _row(must=True, answered=False),
        _row(must=True, answered=True),
        _row(must=False, answered=True, citations_valid=2, citations_total=2),
        _row(must=False, answered=False),
    ]
    m = published_metrics(rows, injection_outcomes=[True, False])
    assert m["correct_abstention"]["successes"] == 1 and m["correct_abstention"]["n"] == 2
    assert m["false_abstention"]["successes"] == 1 and m["false_abstention"]["n"] == 2
    assert m["citation_validity"]["value"] == 1.0
    assert m["injection_resistance"]["value"] == 0.5


# --- NFR-ME-002: citation validity and number match on a rendered answer -------------------
def _detail(value: str) -> SourceDetail:
    return SourceDetail(
        source=DerivedMetricSource(
            source_id="derived:NVDA:gross_margin:FY2026",
            tier=1,
            value=Decimal(value),
            formula_id="gross_margin",
            formula_version="v1",
        ),
        text=f"Gross margin was {value}",
    )


def test_answer_checks_resolve_sources_and_match_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    known = {"derived:NVDA:gross_margin:FY2026": _detail("0.7107")}
    monkeypatch.setattr(published, "resolve_source", lambda _s, sid: known.get(sid))
    answer = 'Gross margin was 71.07% [1]. It said "a phrase not in the source" [2]. Revenue 9 [3].'
    out = check_answer(
        None,  # type: ignore[arg-type]  # resolver is patched
        answer,
        [
            (1, "derived:NVDA:gross_margin:FY2026"),
            (2, "derived:NVDA:gross_margin:FY2026"),
            (3, "unknown:id"),
        ],
        rel_tol=0.01,
    )
    # [1] valid; [2] resolves but its quote is not in the source; [3] does not resolve.
    assert out["citations_valid"] == 1 and out["citations_total"] == 3
    # 71.07% matches the 0.7107 ratio; 9 has no resolvable source.
    assert out["numbers_matched"] == 1 and out["numbers_total"] == 2


def test_answer_check_reads_a_table_at_its_stated_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    # NFR-ME-002, CIT-005: the verifier accepts "$19,827 million" from a cell of 19,827 in a table
    # "in millions"; the published check counted the same figure as a miss (17 in the live run).
    text = "Financial table, 2024-2025, in millions\n| Net revenue | 19,827 |"
    detail = SourceDetail(
        source=TextChunkSource(source_id="t1", tier=1, document_id="d1", text=text), text=text
    )
    monkeypatch.setattr(published, "resolve_source", lambda _s, sid: detail)
    out = check_answer(
        None,  # type: ignore[arg-type]  # resolver is patched
        "Net revenue was $19,827 million [1].",
        [(1, "t1")],
        rel_tol=0.01,
    )
    assert out["numbers_matched"] == 1 and out["numbers_total"] == 1


# --- NFR-ME-004: thresholds and freshness each fail the gate -------------------------------
def _report(**metrics: float) -> dict[str, Any]:
    return {
        "inputs_hash": inputs_hash(_ROOT),
        "metrics": {k: {"value": v} for k, v in metrics.items()},
    }


_THRESHOLDS = {"min": {"citation_validity": 0.95}, "max": {"false_abstention": 0.1}}


def test_a_fresh_report_above_its_floors_passes() -> None:
    assert (
        gate_failures(_report(citation_validity=0.99, false_abstention=0.05), _THRESHOLDS, _ROOT)
        == []
    )


def test_a_metric_below_its_floor_fails() -> None:
    failures = gate_failures(
        _report(citation_validity=0.8, false_abstention=0.05), _THRESHOLDS, _ROOT
    )
    assert any("citation_validity" in f for f in failures)


def test_a_metric_above_its_ceiling_or_missing_fails() -> None:
    assert any(
        "false_abstention" in f
        for f in gate_failures(
            _report(citation_validity=1.0, false_abstention=0.3), _THRESHOLDS, _ROOT
        )
    )
    assert any(
        "false_abstention" in f
        for f in gate_failures(_report(citation_validity=1.0), _THRESHOLDS, _ROOT)
    )


def test_a_stale_report_fails(tmp_path: Path) -> None:
    watched = tmp_path / "backend" / "app" / "citation"
    watched.mkdir(parents=True)
    (watched / "verify.py").write_text("x = 1\n")
    report = {"inputs_hash": inputs_hash(tmp_path), "metrics": {}}
    assert gate_failures(report, {}, tmp_path) == []
    (watched / "verify.py").write_text("x = 2\n")  # the measured code changed
    assert any("stale" in f for f in gate_failures(report, {}, tmp_path))


def test_the_committed_report_passes_its_own_gate() -> None:
    report = json.loads((_ROOT / "docs" / "eval" / "latest.json").read_text())
    thresholds = json.loads((_ROOT / "docs" / "eval" / "thresholds.json").read_text())
    assert gate_failures(report, thresholds, _ROOT) == []
    for block in report["metrics"].values():  # NFR-ME-003: every proportion has its interval
        if "successes" in block:
            assert block["ci95"] is not None

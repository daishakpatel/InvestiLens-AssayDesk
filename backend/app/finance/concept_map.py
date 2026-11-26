"""Concept map: canonical metric names -> ordered XBRL tags, with company overrides and
sector applicability (DR-020, DR-042). Source config: `concept_maps/*.yaml` (spec Appendix B/C).

The map is the single most important correctness layer (spec §11): first-available-tag-wins
selection turns inconsistent raw XBRL into canonical values Phase 1c can run formulas over.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

_CONFIG_DIR = Path(__file__).resolve().parent / "concept_maps"

MetricKind = Literal["flow", "stock"]
UnitFamily = Literal["currency", "per_share", "shares"]


@dataclass(frozen=True)
class ConceptMap:
    """Resolved concept map for one company (defaults merged with any override)."""

    version: str
    sector: str  # applicability key: standard | bank | reit
    _flow: dict[str, list[str]]
    _stock: dict[str, list[str]]
    _units: dict[str, UnitFamily]
    _sector_applicability: dict[str, bool]
    _cash_flow: frozenset[str] = frozenset()
    _annual_only: frozenset[str] = frozenset()
    # Footnote facts (Phase 7f): metric -> tags, metric -> note category, flows, non-USD units.
    _footnote: dict[str, list[str]] = field(default_factory=dict)
    _footnote_category: dict[str, str] = field(default_factory=dict)
    _footnote_flow: frozenset[str] = frozenset()
    _footnote_units: dict[str, str] = field(default_factory=dict)
    # Composite tag -> component tags summed into it (company overrides, Phase 7h).
    _composites: dict[str, list[str]] = field(default_factory=dict)
    # False when share classes convert at other than one for one (Visa): summing them misstates
    # the share count, so market cap stays missing instead (ADR-0074).
    sum_share_classes: bool = True

    def tags(self, metric: str) -> list[str]:
        """Ordered XBRL tags to try for a canonical metric (first available wins)."""
        return self._flow.get(metric) or self._stock.get(metric) or self._footnote.get(metric) or []

    def kind(self, metric: str) -> MetricKind | None:
        if metric in self._flow or metric in self._footnote_flow:
            return "flow"
        if metric in self._stock or metric in self._footnote:
            return "stock"
        return None

    def unit_family(self, metric: str) -> UnitFamily:
        return self._units.get(metric, "currency")

    def is_cash_flow(self, metric: str) -> bool:
        """Whether a flow is reported year-to-date in 10-Qs (cash-flow statement, FR-QT-002)."""
        return metric in self._cash_flow

    def is_annual_only(self, metric: str) -> bool:
        """Whether a flow is disclosed only in the 10-K, so it has no quarters (Phase 7d)."""
        return metric in self._annual_only

    def applies(self, metric: str) -> bool:
        """Whether a metric is applicable for this company's sector (DR-001/DR-042)."""
        return self._sector_applicability.get(metric, True)

    @property
    def composites(self) -> dict[str, list[str]]:
        """Composite tags ("composite:depreciation_amortization") and their summed parts."""
        return dict(self._composites)

    @property
    def footnote_metrics(self) -> dict[str, str]:
        """Footnote metric -> its note category, in concept-map order (Phase 7f)."""
        return dict(self._footnote_category)

    def footnote_unit(self, metric: str) -> str:
        return self._footnote_units.get(metric, "USD")

    @property
    def flow_metrics(self) -> list[str]:
        return list(self._flow)

    @property
    def stock_metrics(self) -> list[str]:
        return list(self._stock)


@lru_cache
def _load_yaml(name: str) -> dict[str, Any]:
    path = _CONFIG_DIR / name
    return yaml.safe_load(path.read_text()) or {}


@lru_cache
def _load_override(ticker: str, cik: str) -> dict[str, Any]:
    for candidate in (f"concept_overrides/{ticker.upper()}.yaml", f"concept_overrides/{cik}.yaml"):
        path = _CONFIG_DIR / candidate
        if path.exists():
            return yaml.safe_load(path.read_text()) or {}
    return {}


def _resolve_sector(sector: str | None, override: dict[str, Any]) -> str:
    if "sector" in override:
        return str(override["sector"])
    aliases = _load_yaml("sector_applicability.yaml").get("sector_aliases", {})
    return str(aliases.get(sector or "", "standard"))


def load_concept_map(*, ticker: str = "", cik: str = "", sector: str | None = None) -> ConceptMap:
    """Build the concept map for a company: defaults, with any override merged on top."""
    base = _load_yaml("concept_map_v1.yaml")
    override = _load_override(ticker, cik) if (ticker or cik) else {}

    flow = {k: list(v) for k, v in base.get("flow_metrics", {}).items()}
    stock = {k: list(v) for k, v in base.get("stock_metrics", {}).items()}
    for metric, tags in override.get("flow_metrics", {}).items():
        flow[metric] = list(tags)  # override replaces the tag list for that metric
    for metric, tags in override.get("stock_metrics", {}).items():
        stock[metric] = list(tags)

    resolved_sector = _resolve_sector(sector, override)
    applicability = _load_yaml("sector_applicability.yaml").get("sectors", {})
    sector_map = applicability.get(resolved_sector, applicability.get("standard", {}))

    footnote = base.get("footnote_metrics", {})
    return ConceptMap(
        version=str(base.get("version", "v1")),
        sector=resolved_sector,
        _flow=flow,
        _stock=stock,
        _units=base.get("units", {}),
        _sector_applicability=sector_map,
        _cash_flow=frozenset(base.get("cash_flow_metrics", [])),
        _annual_only=frozenset(base.get("annual_only_metrics", [])),
        _footnote={m: list(t) for group in footnote.values() for m, t in group.items()},
        _footnote_category={m: cat for cat, group in footnote.items() for m in group},
        _footnote_flow=frozenset(base.get("footnote_flow_metrics", [])),
        _footnote_units=dict(base.get("footnote_units", {})),
        _composites={k: list(v) for k, v in override.get("composite_tags", {}).items()},
        sum_share_classes=bool(override.get("sum_share_classes", True)),
    )

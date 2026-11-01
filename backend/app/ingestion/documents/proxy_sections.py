"""Proxy statement (DEF 14A / DEFA14A) sections by heading keyword (Phase 7g, FR-GV-001).

A proxy has no Item numbers, so sections start at headings: a short line, not a sentence, matching
one of the rules below (first match wins, most specific first). A section runs to the next such
heading, so text under an unmatched sub-heading stays with the section above it. Table-of-contents
lines start sections too; they are tiny, and `sections_found` ignores sections with no body.
Everything before the first heading (the cover and notice) is the `document` section.
"""

from __future__ import annotations

import re

from app.ingestion.documents.blocks import Block
from app.ingestion.documents.sections import WHOLE_DOCUMENT, Section, SectionResult

_MAX_HEADING_CHARS = 120
_MIN_SECTION_CHARS = 200  # a heading with less text under it is a table-of-contents line

# (key, chunk-ID code, heading pattern). Order matters: "Pay Ratio" before the board rules, etc.
RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("summary_compensation_table", "SCT", re.compile(r"^summary compensation table", re.I)),
    ("pay_versus_performance", "PVP", re.compile(r"^pay versus performance", re.I)),
    ("pay_ratio", "PR", re.compile(r"\bpay ratio\b", re.I)),
    ("cd_and_a", "CDA", re.compile(r"^compensation discussion (and|&) analysis", re.I)),
    (
        "say_on_pay",
        "SOP",
        re.compile(r"advisory (vote|resolution|approval)\b.*compensation|say[- ]on[- ]pay", re.I),
    ),
    (
        "beneficial_ownership",
        "OWN",
        re.compile(r"^security ownership\b", re.I),
    ),
    (
        "director_independence",
        "IND",
        re.compile(
            r"^(director|board) independence|^independence of (the )?(members of the )?board", re.I
        ),
    ),
    (
        "committees",
        "COM",
        re.compile(
            r"^(board )?committees\b|^committees of the board|^board and committee structure", re.I
        ),
    ),
    (
        "board",
        "BRD",
        re.compile(
            r"^(information about )?(the |our )?board of directors( and corporate governance)?$"
            r"|^corporate governance( matters)?$|^role of the board( of directors)?$"
            r"|^nominees to .{0,40}board of directors$",
            re.I,
        ),
    ),
)
SECTION_KEYS: tuple[str, ...] = tuple(key for key, _, _ in RULES)
# A heading's own numbering or bullet ("I. Summary compensation table", "•Advisory resolution")
# is not its title.
_ENUMERATOR = re.compile(r"^([•▪●◦‣]\s*|\(?([ivx]+|\d+|[a-z])[.)]\s+)", re.I)


def proxy_heading(block: Block) -> tuple[str, str] | None:
    """(key, code) if the block is a proxy section heading."""
    text = _ENUMERATOR.sub("", block.text.strip())
    if block.kind != "text" or not text or len(text) > _MAX_HEADING_CHARS or text.endswith("."):
        return None
    if not text[0].isupper():
        return None
    for key, code, pattern in RULES:
        if pattern.search(text):
            return key, code
    return None


def detect_proxy_sections(blocks: list[Block]) -> SectionResult:
    """Sections of a proxy statement; always confident (no section is required to exist)."""
    starts = [(i, heading) for i, b in enumerate(blocks) if (heading := proxy_heading(b))]
    sections: list[Section] = []
    first = starts[0][0] if starts else len(blocks)
    if first > 0:
        sections.append(
            Section(WHOLE_DOCUMENT, "Proxy statement", ["Proxy statement"], -1, first, "document")
        )
    for position, (idx, (key, code)) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(blocks)
        title = blocks[idx].text.strip()
        sections.append(Section(code, title, [title], idx, end, f"proxy_{key}"))
    return SectionResult(sections=sections, confidence=1.0, low_confidence=False)


def sections_found(sections: list[Section], blocks: list[Block]) -> list[str]:
    """Keys of the sections that have a body (FR-GV-001 coverage), in rule order."""
    found = set()
    for section in sections:
        body = blocks[section.start_idx + 1 : section.end_idx]
        size = sum(len(b.text) if b.kind == "text" else _MIN_SECTION_CHARS for b in body)
        if section.key.startswith("proxy_") and size >= _MIN_SECTION_CHARS:
            found.add(section.key.removeprefix("proxy_"))
    return [key for key in SECTION_KEYS if key in found]

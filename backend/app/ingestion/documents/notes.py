"""Notes to the financial statements: segmentation and classification (Phase 7f, ADR-0044).

Works on a filing's chunk rows, in document order, so every note is a run of whole chunks and
cites the same `paragraph_id`s as the rest of the app (FR-FN-001). The chunker starts a new chunk
at every note heading in the financial-statement sections, so a note never shares a chunk with
its neighbour.

A heading is "Note 7 - Debt" (the title may sit in the next paragraph) or "7. Debt". Note numbers
must increase, which skips a cross-reference ("see Note 3") set as its own line and a repeated
heading; once a filing's first heading fixes its style, only that style counts, so a numbered
list inside a "Note N" filing is not read as notes. Notes under bold headings with no number are
not detected (shortcut: needs the raw HTML's emphasis, as `filings.units` reads for risk factors;
upgrade if a covered filer uses them). Fewer than five notes in a 10-K is flagged, never raised.

Classification is by title keywords only, in a fixed priority order (`note_taxonomy_v1`,
FR-FN-002): a title matching two categories takes the first and records the others.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

NOTE_TAXONOMY_VERSION = "note_taxonomy_v1"
# Where the statements and notes live: 10-K Item 8, or Item 15 when the filer prints them after
# the exhibit list (NVIDIA, JPMorgan); 10-Q Part I Item 1, or an unitemized Part I (no headings).
NOTES_SECTIONS = frozenset({"8", "15", "P1-1", "P1-0"})
LOW_CONFIDENCE_MIN_NOTES = 5  # a 10-K with fewer notes was probably mis-segmented (FR-FN-001)

# "Note 7 -" (any dash), "NOTE 7 - Debt" (AMD), and page furniture glued in front
# ("+42.8+Note 5 -"). Nothing alphabetic may precede "Note", so "Refer to Note 5" is not a heading,
# and a bare "Note 12" needs its dash: JPMorgan's MD&A sets cross-references as their own lines.
_NOTE_STYLE = re.compile(r"^[^A-Za-z]{0,12}(?i:note)\s+(\d{1,3})\b\s*([-\u2013\u2014:.]?)\s*(.*)$")
_NUMBER_STYLE = re.compile(r"^(\d{1,2})\.\s+(\S.*)$")  # "7. Debt"
_MAX_HEADING_CHARS = 140
_MAX_TITLE_CHARS = 120

# Priority order: the first matching category wins (FR-FN-002). Specific before general, so
# "Commitments and Contingencies - Leases" is a leases note and "Debt and Leases" a debt note.
CATEGORY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("debt", re.compile(r"\bdebt\b|\bborrowings?\b|credit facilit|notes payable", re.I)),
    ("leases", re.compile(r"\bleas(e|es|ing)\b", re.I)),
    ("revenue_recognition", re.compile(r"\brevenues?\b|contracts? with customers", re.I)),
    ("income_taxes", re.compile(r"\bincome tax|\btax(es)?\b", re.I)),
    (
        "commitments_contingencies",
        re.compile(r"\bcommitments?\b|contingenc|litigation|legal proceedings|guarantees", re.I),
    ),
    ("segments", re.compile(r"\bsegments?\b", re.I)),
    (
        "stock_comp",
        re.compile(r"\b(stock|share|equity)[- ]based\b|stock compensation|equity incentive", re.I),
    ),
    ("goodwill_intangibles", re.compile(r"goodwill|intangible", re.I)),
    ("derivatives", re.compile(r"derivative|hedg", re.I)),
    ("fair_value", re.compile(r"fair value", re.I)),
)
OTHER = "other"
CATEGORIES: tuple[str, ...] = (*(name for name, _ in CATEGORY_RULES), OTHER)


@dataclass(frozen=True)
class Heading:
    number: int
    title: str  # "" when the title is the next paragraph
    style: re.Pattern[str]


@dataclass(frozen=True)
class Note:
    note_id: str  # "note-7": stable within the document; summaries are cached under it
    number: int
    title: str
    category: str
    also_matches: tuple[str, ...]  # other categories the title matched (a priority tie)
    paragraph_ids: tuple[str, ...]  # its chunks, in document order (backend-issued source IDs)

    def to_json(self) -> dict[str, Any]:
        return {
            "note_id": self.note_id,
            "number": self.number,
            "title": self.title,
            "category": self.category,
            "also_matches": list(self.also_matches),
            "paragraph_ids": list(self.paragraph_ids),
        }


@dataclass(frozen=True)
class NoteSegmentation:
    notes: list[Note]
    low_confidence: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "taxonomy": NOTE_TAXONOMY_VERSION,
            "low_confidence": self.low_confidence,
            "items": [note.to_json() for note in self.notes],
        }


def note_heading(text: str) -> Heading | None:
    """The note number and title if `text` (one paragraph) is a note heading."""
    line = text.strip()
    if len(line) > _MAX_HEADING_CHARS:
        return None
    for style in (_NOTE_STYLE, _NUMBER_STYLE):
        match = style.match(line)
        if match is None:
            continue
        title = match.group(style.groups).strip()  # the last group is the title
        if style is _NOTE_STYLE and not title and not match.group(2):
            return None  # "Note 12" with neither dash nor title: a cross-reference line
        # A sentence ("Note 5 of the Notes …", "1. The Company …") is not a title.
        if title and (
            not title[0].isupper() or title.endswith(".") or len(title) > _MAX_TITLE_CHARS
        ):
            return None
        return Heading(int(match.group(1)), title, style)
    return None


def classify(title: str) -> tuple[str, tuple[str, ...]]:
    """(category, other matching categories) from the title alone (FR-FN-002)."""
    matched = [name for name, pattern in CATEGORY_RULES if pattern.search(title)]
    return (matched[0], tuple(matched[1:])) if matched else (OTHER, ())


def section_code(row: Mapping[str, Any]) -> str:
    """The chunk's item code ("8", "P1-1") from its `parent_section_id` ("{document}_{code}")."""
    return str(row.get("parent_section_id") or "").split("_", 1)[-1]


def segment_notes(rows: Iterable[Mapping[str, Any]], *, filing_type: str) -> NoteSegmentation:
    """Split a filing's chunk rows (document order) into notes (FR-FN-001)."""
    notes: list[tuple[Heading, str, list[str]]] = []  # (heading, title, paragraph ids)
    current: list[str] | None = None
    style: re.Pattern[str] | None = None
    for row in rows:
        if section_code(row) not in NOTES_SECTIONS:
            current = None  # the notes end with their section
            continue
        paragraphs = str(row.get("text") or "").split("\n\n")
        heading = note_heading(paragraphs[0]) if row.get("chunk_type") == "text" else None
        if (
            heading is not None
            and style in (None, heading.style)
            and heading.number > (notes[-1][0].number if notes else 0)
        ):
            style = heading.style
            title = heading.title or (paragraphs[1].strip() if len(paragraphs) > 1 else "")
            current = []
            notes.append((heading, title, current))
        if current is not None:
            current.append(str(row["paragraph_id"]))

    built = []
    for heading, title, paragraph_ids in notes:
        category, also = classify(title)
        built.append(
            Note(
                note_id=f"note-{heading.number}",
                number=heading.number,
                title=title or f"Note {heading.number}",
                category=category,
                also_matches=also,
                paragraph_ids=tuple(paragraph_ids),
            )
        )
    annual = filing_type.startswith("10-K")
    return NoteSegmentation(built, low_confidence=annual and len(built) < LOW_CONFIDENCE_MIN_NOTES)

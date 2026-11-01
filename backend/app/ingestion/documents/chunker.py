"""Financial-aware chunking (CH-001…008, DP-006).

Produces `document_chunks` rows: section-bounded text chunks (never crossing an Item), tables as
first-class `table` chunks, with deterministic IDs, a contextual header, parent-section links,
content + near-duplicate hashes, and driver-language flags. No embeddings here (Phase 2b).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.ingestion.documents.blocks import Block
from app.ingestion.documents.drivers import is_driver_language
from app.ingestion.documents.notes import NOTES_SECTIONS, note_heading
from app.ingestion.documents.sections import Section, detect_sections
from app.ingestion.documents.tokens import estimate_tokens

# One version for the whole parse (ADR-0041): stamped on every chunk and, once a document is
# parsed, on `documents.parser_version`, so up-to-date documents are skipped. v2: 8-K items are
# full dotted codes and 10-Q items are part-qualified, which changes their chunk IDs. v3: every
# financial-statement note starts its own chunk (Phase 7f, ADR-0044). v4: proxy statements are
# split into keyword sections (Phase 7g, ADR-0045). v5: lines break only at block elements, so
# inline-tagged figures stay in their sentence; text chunk IDs end in "_p{n}"; annual-report
# layouts (Intel) are sectioned by their own headings (ADR-0052).
PARSER_VERSION = "sec-ingest-v5"
_TARGET_TOKENS = 400
_MAX_TOKENS = 800
_OVERLAP_MAX_TOKENS = 60  # ~15% of target: carry one small paragraph into the next chunk
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_DIGITS = re.compile(r"\d")
_SUBHEADING_MAX_CHARS = 140
# Risk factors and financial-statement notes start a new unit at each sub-heading (CH-002/008).
_RISK_OR_NOTES = {"1A", "8", "P1-1", "P2-1A"}


@dataclass
class FilingMeta:
    document_id: int
    company_id: int
    company_name: str
    filing_type: str
    period_label: str
    filing_date: date | None = None
    period_end: date | None = None
    tier: int = 1  # SEC filings are Tier 1 (§15)


@dataclass
class _Acc:
    """Accumulator for the current text chunk within a section."""

    paragraphs: list[str] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return estimate_tokens("\n\n".join(self.paragraphs))


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _dedup_hash(text: str) -> str:
    """Hash with digits masked, so boilerplate differing only by year/figures collapses (CH-006)."""
    masked = _DIGITS.sub("#", " ".join(text.lower().split()))
    return hashlib.sha256(masked.encode()).hexdigest()


def _context_header(meta: FilingMeta, section_path: list[str]) -> str:
    # CH-004: embedded with the chunk, not shown in citations.
    path = " > ".join(section_path)
    return f"{meta.company_name} | {meta.filing_type} | {meta.period_label} | {path}"


def _split_oversized(paragraph: str) -> list[str]:
    """Split a single over-max paragraph on sentence boundaries."""
    sentences = _SENTENCE.split(paragraph)
    out: list[str] = []
    current: list[str] = []
    for sentence in sentences:
        current.append(sentence)
        if estimate_tokens(" ".join(current)) >= _TARGET_TOKENS:
            out.append(" ".join(current))
            current = []
    if current:
        out.append(" ".join(current))
    return out or [paragraph]


def _is_bare_note(text: str) -> bool:
    heading = note_heading(text)
    return heading is not None and not heading.title


def _is_subheading(text: str) -> bool:
    return len(text) <= _SUBHEADING_MAX_CHARS and not text.endswith((".", ";", ":"))


class _ChunkBuilder:
    def __init__(self, meta: FilingMeta) -> None:
        self.meta = meta
        self.rows: list[dict[str, Any]] = []
        self.index = 0

    def add_text_chunk(self, section: Section, body: str, char_start: int) -> None:
        if not body.strip():
            return
        parent_id = f"{self.meta.document_id}_{section.item_code}"
        # Deterministic (DP-006). The "p" (since v5) keeps a new ID from ever equalling one issued
        # by an older parse, so a stale citation fails as not found instead of naming other text.
        paragraph_id = f"{parent_id}_p{self.index}"
        self.rows.append(
            {
                "document_id": self.meta.document_id,
                "company_id": self.meta.company_id,
                "chunk_index": self.index,
                "chunk_type": "text",
                "text": body,
                "section": section.title,
                "subsection": None,
                "section_path": section.section_path,
                "paragraph_id": paragraph_id,
                "parent_section_id": parent_id,
                "page": None,
                "char_start": char_start,
                "char_end": char_start + len(body),
                "tier": self.meta.tier,
                "filing_type": self.meta.filing_type,
                "filing_date": self.meta.filing_date,
                "period_end": self.meta.period_end,
                "parser_version": PARSER_VERSION,
                "content_hash": _content_hash(body),
                "chunk_metadata": {
                    "context_header": _context_header(self.meta, section.section_path),
                    "section_key": section.key,
                    "dedup_hash": _dedup_hash(body),
                    "is_driver_language": is_driver_language(body),
                },
            }
        )
        self.index += 1

    def add_table_chunk(self, section: Section, block: Block) -> None:
        table = block.table
        if table is None:
            return
        parent_id = f"{self.meta.document_id}_{section.item_code}"
        body = f"{table.caption}\n\n{table.markdown}"  # caption + Markdown (CH-003)
        self.rows.append(
            {
                "document_id": self.meta.document_id,
                "company_id": self.meta.company_id,
                "chunk_index": self.index,
                "chunk_type": "table",
                "text": body,
                "section": section.title,
                "subsection": None,
                "section_path": section.section_path,
                "paragraph_id": f"{parent_id}_{table.table_id}",
                "parent_section_id": parent_id,
                "page": None,
                "char_start": None,
                "char_end": None,
                "tier": self.meta.tier,
                "filing_type": self.meta.filing_type,
                "filing_date": self.meta.filing_date,
                "period_end": self.meta.period_end,
                "parser_version": PARSER_VERSION,
                "content_hash": _content_hash(body),
                "chunk_metadata": {
                    "context_header": _context_header(self.meta, section.section_path),
                    "section_key": section.key,
                    "dedup_hash": _dedup_hash(table.markdown),
                    "table_id": table.table_id,
                    "row_labels": table.row_labels,
                    "col_labels": table.header,
                    "cell_refs": table.cell_refs,
                },
            }
        )
        self.index += 1


def _flush(
    builder: _ChunkBuilder, section: Section, acc: _Acc, offset: int
) -> tuple[list[str], int]:
    """Emit the accumulated paragraphs as a chunk; return overlap paragraphs + new offset."""
    if not acc.paragraphs:
        return [], offset
    body = "\n\n".join(acc.paragraphs)
    builder.add_text_chunk(section, body, offset)
    offset += len(body) + 2
    tail = acc.paragraphs[-1]
    overlap = [tail] if estimate_tokens(tail) <= _OVERLAP_MAX_TOKENS else []
    return overlap, offset


def chunk_filing(html_blocks: list[Block], meta: FilingMeta) -> list[dict[str, Any]]:
    """Chunk a parsed filing into `document_chunks` row-mappings (CH-001…008)."""
    result = detect_sections(html_blocks, meta.filing_type)
    builder = _ChunkBuilder(meta)
    for section in result.sections:
        is_risk_or_notes = section.item_code in _RISK_OR_NOTES
        acc = _Acc()
        offset = 0
        for block in html_blocks[section.start_idx + 1 : section.end_idx]:
            if block.kind == "table":
                overlap, offset = _flush(builder, section, acc, offset)  # close text before table
                acc = _Acc(paragraphs=list(overlap))
                builder.add_table_chunk(section, block)
                continue
            # Risk-factor / note sub-heading starts a new logical unit (CH-002/CH-008), and every
            # note heading starts a chunk wherever the notes are printed (FR-FN-001). A bare
            # "Note 7 -" keeps its title line ("Debt") in the same chunk.
            starts_note = section.item_code in NOTES_SECTIONS and note_heading(block.text)
            bare_note = len(acc.paragraphs) == 1 and _is_bare_note(acc.paragraphs[0])
            if (
                acc.paragraphs
                and not bare_note
                and (starts_note or (is_risk_or_notes and _is_subheading(block.text)))
            ):
                _, offset = _flush(builder, section, acc, offset)
                acc = _Acc()
            for piece in (
                _split_oversized(block.text)
                if estimate_tokens(block.text) > _TARGET_TOKENS
                else [block.text]
            ):
                acc.paragraphs.append(piece)
                if acc.tokens >= _TARGET_TOKENS:
                    overlap, offset = _flush(builder, section, acc, offset)
                    acc = _Acc(paragraphs=list(overlap))
        _flush(builder, section, acc, offset)
    return builder.rows

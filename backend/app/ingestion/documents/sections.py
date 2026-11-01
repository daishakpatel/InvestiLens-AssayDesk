"""Section detection for SEC filings (DP-001, FR-EX-001, FR-EX-012).

Detects Item headings, de-duplicates the table-of-contents copy of each heading (the real heading
is the later occurrence), assigns every block to a section, and reports a confidence score so
low-confidence parses can be flagged rather than trusted silently. Per form:

- 10-K: "Item 1A", "Item 7", … (codes as before).
- 10-Q: items are numbered again in Part II, so codes are part-qualified ("P1-1" = Part I Item 1,
  the financial statements and notes; "P2-1A" = Part II risk factors). Keyed on the bare number,
  Part II's "Item 1" overwrote Part I's and all of Part I was dropped.
- 8-K: full dotted codes ("5.02"), via `eightk_items`.
- DEF 14A / DEFA14A: keyword headings (board, compensation tables, …), via `proxy_sections`.
- anything else (an exhibit): the whole document is one section.

A 10-K or 10-Q with no "Item N" heading at all (Intel's annual-report layout) is sectioned by its
own headings instead, where they carry a standard item title ("Risk Factors", "Management's
Discussion and Analysis"); a few non-item headings ("Key Terms") only end the section before them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.ingestion.documents.blocks import Block
from app.ingestion.documents.eightk_items import parse_item_heading

# A heading block starts with "Item N[letter]" and is short (not a sentence referencing an item).
_ITEM_RE = re.compile(r"^item\s+(\d+[a-z]?)\b[.:)\s-]*(.*)$", re.I)
_PART_RE = re.compile(r"^part\s+(ii|i|2|1)\b", re.I)
_MAX_HEADING_CHARS = 200

# Annual-report layouts: a heading's whole text → item code, per form. Matched on whole headings
# only (never sentences), first occurrence: the table of contents is one table, not headings,
# and later copies are page footers. Code "0" marks a non-item heading that only ends a section.
# "Legal Proceedings" is left out: these layouts print it inside the contingencies note.
_TITLE_ITEMS: dict[str, list[tuple[re.Pattern[str], str]]] = {
    "10-K": [
        (re.compile(r"(fundamentals of |introduction to )?(our )?business"), "1"),
        (re.compile(r"risk factors"), "1A"),
        (re.compile(r"cybersecurity"), "1C"),
        (re.compile(r"properties"), "2"),
        (re.compile(r"market for (our|the registrant's) common (stock|equity).*"), "5"),
        (re.compile(r"management's discussion and analysis.*"), "7"),
        (re.compile(r"quantitative and qualitative disclosures about market risk"), "7A"),
        (re.compile(r"financial statements and (supplemental details|supplementary data)"), "8"),
        (re.compile(r"controls and procedures"), "9A"),
        (re.compile(r"exhibits( and financial statement schedules)?"), "15"),
        (re.compile(r"other key information|information about our executive officers"), "0"),
        (re.compile(r"key terms|form 10-k cross-reference index|signatures"), "0"),
    ],
    "10-Q": [
        # The statements may have no group heading: the first statement's own title starts them.
        (
            re.compile(
                r"consolidated condensed (financial statements|statements of (operations|income)).*"
            ),
            "P1-1",
        ),
        (re.compile(r"management's discussion and analysis.*"), "P1-2"),
        (re.compile(r"quantitative and qualitative disclosures about market risk"), "P1-3"),
        (re.compile(r"controls and procedures"), "P1-4"),
        (re.compile(r"risk factors"), "P2-1A"),
        (re.compile(r"issuer purchases of equity securities"), "P2-2"),
        (re.compile(r"rule 10b5-1 trading arrangements|other information"), "P2-5"),
        (re.compile(r"exhibits"), "P2-6"),
        (re.compile(r"key terms|form 10-q cross-reference index|signatures"), "0"),
    ],
}

# Core items used to score detection confidence.
_CORE_10K = {"1", "1A", "2", "3", "5", "7", "7A", "8", "9A"}
_CORE_10Q = {"P1-1", "P1-2"}  # financial statements + notes, and MD&A
WHOLE_DOCUMENT = "0"
PROXY_FORMS = ("DEF 14A", "DEFA14A")


@dataclass
class Section:
    item_code: str  # "1A", "7" (10-K); "P1-1" (10-Q); "5.02" (8-K); "0" (whole document)
    title: str
    section_path: list[str]
    start_idx: int  # index into the blocks list (the heading block; -1 = no heading)
    end_idx: int  # exclusive
    key: str = ""  # stable name for downstream lookups, e.g. "10q_part1_item1_notes"


@dataclass
class SectionResult:
    sections: list[Section]
    confidence: float  # 0-1; fraction of core items detected
    low_confidence: bool


def _short_text(block: Block) -> str | None:
    if block.kind != "text" or len(block.text) > _MAX_HEADING_CHARS:
        return None
    return block.text.strip()


def _layout_row(block: Block) -> str | None:
    """A one-row table used for layout, read as text: JPM sets each 8-K heading as
    ["Item 8.01.", "Other Events."]."""
    table = block.table
    if block.kind != "table" or table is None or len(table.rows) != 1:
        return None
    return " ".join(cell for cell in table.rows[0] if cell)


def _item_heading(block: Block) -> tuple[str, str] | None:
    text = _short_text(block)
    match = _ITEM_RE.match(text) if text else None
    return (match.group(1).upper(), match.group(2).strip()) if match else None


def _part(block: Block) -> int | None:
    text = _short_text(block)
    match = _PART_RE.match(text) if text else None
    return None if match is None else (2 if match.group(1).lower() in ("ii", "2") else 1)


def _candidates(blocks: list[Block], filing_type: str) -> list[tuple[int, str, str, str]]:
    """(block index, code, label, title) of every heading candidate, in document order."""
    out: list[tuple[int, str, str, str]] = []
    if filing_type.startswith("8-K"):
        for idx, block in enumerate(blocks):
            text = _short_text(block) or _layout_row(block)
            heading = parse_item_heading(text) if text else None
            if heading:
                out.append((idx, heading[0], f"Item {heading[0]}", heading[1]))
        return out
    quarterly = filing_type.startswith("10-Q")
    part = 1  # items before any "Part" heading belong to Part I
    for idx, block in enumerate(blocks):
        if quarterly and (found := _part(block)) is not None:
            part = found
            continue
        heading = _item_heading(block)
        if heading is None:
            continue
        code, title = heading
        if quarterly:
            numeral = "I" if part == 1 else "II"
            out.append((idx, f"P{part}-{code}", f"Part {numeral}, Item {code}", title))
        else:
            out.append((idx, code, f"Item {code}", title))
    return out


def _label(filing_type: str, code: str) -> str:
    if filing_type.startswith("10-Q"):
        part, item = code[1:].split("-", 1)
        return f"Part {'I' if part == '1' else 'II'}, Item {item}"
    return f"Item {code}"


def _title_starts(blocks: list[Block], filing_type: str) -> list[tuple[int, str, str, str]]:
    """(block index, code, label, title) for an annual-report layout's own headings."""
    rules = _TITLE_ITEMS["10-Q" if filing_type.startswith("10-Q") else "10-K"]
    seen: set[int] = set()  # each rule once: its first heading
    out: list[tuple[int, str, str, str]] = []
    for idx, block in enumerate(blocks):
        raw = _short_text(block) or _layout_row(block)
        if not raw:
            continue
        text = " ".join(raw.replace("\u2019", "'").lower().split()).rstrip(":.")
        for rule, (pattern, code) in enumerate(rules):
            if rule not in seen and pattern.fullmatch(text):
                seen.add(rule)
                label = raw.strip() if code == WHOLE_DOCUMENT else _label(filing_type, code)
                out.append((idx, code, label, "" if code == WHOLE_DOCUMENT else raw.strip()))
                break
    if out and out[0][1] == "1":
        # The overview and strategy printed before "Our Business" belong to Item 1; without
        # this they would fall before the first section and be dropped.
        out[0] = (-1, *out[0][1:])
    elif out and out[0][0] > 0:
        out.insert(0, (-1, WHOLE_DOCUMENT, "Filing text", ""))
    return out


def section_key(filing_type: str, code: str) -> str:
    """A stable section name, independent of the heading's wording."""
    if code == WHOLE_DOCUMENT:
        return "document"
    if filing_type.startswith("10-Q"):
        part, item = code[1:].split("-", 1)
        if (part, item) == ("1", "1"):
            return "10q_part1_item1_notes"  # financial statements and their notes (FR-EX-012)
        if item == WHOLE_DOCUMENT:
            return "10q_part1_unitemized"
        return f"10q_part{part}_item{item.lower()}"
    if filing_type.startswith("8-K"):
        return f"8k_item{code}"
    return f"10k_item{code.lower()}"


def detect_sections(blocks: list[Block], filing_type: str) -> SectionResult:
    """Return the detected sections (TOC-deduplicated) with a confidence score."""
    if filing_type in PROXY_FORMS:
        # Imported here: proxy_sections builds on this module's Section types.
        from app.ingestion.documents.proxy_sections import detect_proxy_sections

        return detect_proxy_sections(blocks)
    structured = filing_type.startswith(("10-K", "10-Q", "8-K"))
    # Keep the LAST occurrence of each code: an earlier one is its table-of-contents entry.
    last_by_code: dict[str, tuple[int, str, str]] = {}
    if structured:
        for idx, code, label, title in _candidates(blocks, filing_type):
            last_by_code[code] = (idx, label, title)

    starts = sorted((idx, code, label, title) for code, (idx, label, title) in last_by_code.items())
    if not starts and filing_type.startswith(("10-K", "10-Q")):
        starts = _title_starts(blocks, filing_type)
    if (
        filing_type.startswith("10-Q")
        and starts
        and "P1-1" not in last_by_code
        and starts[0][0] > 0
    ):
        # A cross-reference layout (JPM) lists Part I's items only in a page-number table, so
        # its financial statements and MD&A carry no item heading. Keep them as one Part I
        # section rather than drop them; the missing Item 1 still lowers the confidence.
        starts.insert(0, (-1, f"P1-{WHOLE_DOCUMENT}", "Part I", "Financial information"))
    sections: list[Section] = []
    for position, (idx, code, label, title) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(blocks)
        full_title = f"{label}. {title}".strip(". ") if title else label
        sections.append(
            Section(code, full_title, [label], idx, end, section_key(filing_type, code))
        )

    if not sections and blocks:
        # No item heading and no item-titled heading (a document that has no items: a proxy
        # statement, an exhibit): keep the whole document as one section rather than drop it.
        sections.append(
            Section(WHOLE_DOCUMENT, "Filing text", ["Filing text"], -1, len(blocks), "document")
        )

    detected = {s.item_code for s in sections}
    if filing_type.startswith("10-K"):
        confidence = len(detected & _CORE_10K) / len(_CORE_10K)
    elif filing_type.startswith("10-Q"):
        confidence = len(detected & _CORE_10Q) / len(_CORE_10Q)
    elif filing_type.startswith("8-K"):
        confidence = 0.0 if detected == {WHOLE_DOCUMENT} else 1.0
    else:
        confidence = 1.0 if sections else 0.0  # no items expected
    return SectionResult(sections=sections, confidence=confidence, low_confidence=confidence < 0.8)

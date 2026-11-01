"""Linearize cleaned filing HTML into ordered blocks (text paragraphs + tables).

Tables are replaced with sentinels before extracting text so their position in the document order
is preserved; each sentinel maps back to a structured `ExtractedTable`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from selectolax.parser import HTMLParser

from app.ingestion.documents.normalize import normalize_text
from app.ingestion.documents.tables import ExtractedTable, extract_table

# Null-free sentinel: the HTML parser strips \x00, so use an unlikely ASCII token instead.
_SENTINEL = "zqztablezqz{}zqz"
_SENTINEL_RE = re.compile(r"zqztablezqz(\d+)zqz")


@dataclass
class Block:
    kind: str  # "text" | "table"
    text: str = ""  # normalized paragraph text (kind == text)
    table: ExtractedTable | None = None  # kind == table


def linearize(tree: HTMLParser) -> list[Block]:
    """Return document-ordered blocks. Mutates `tree` (replaces tables with sentinels)."""
    extracted: list[ExtractedTable] = []
    for i, node in enumerate(tree.css("table")):
        extracted.append(extract_table(node, table_id=f"t{i:04d}"))
        # A bare token: selectolax inserts a string as text, so "<p>" here became literal text.
        node.replace_with(_SENTINEL.format(i))

    body = tree.body or tree.root
    raw = body.text(separator="\n") if body is not None else ""

    # Split on table sentinels (which can appear mid-line), keeping document order.
    blocks: list[Block] = []
    parts = _SENTINEL_RE.split(raw)  # [text, idx, text, idx, ..., text]
    for position, part in enumerate(parts):
        if position % 2 == 1:  # capture group -> a table index
            blocks.append(Block(kind="table", table=extracted[int(part)]))
            continue
        for line in part.split("\n"):
            normalized = normalize_text(line)
            if normalized:
                blocks.append(Block(kind="text", text=normalized))
    return blocks

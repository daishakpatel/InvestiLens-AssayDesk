"""Remap stored chunk IDs after the v5 re-parse (ADR-0052).

Re-parsing renumbers text chunks, and v5 IDs end in `_p{n}`, so a reference made before it no
longer resolves. Run in two steps around the re-parse:

    cd backend && uv run python ../scripts/remap_chunk_ids.py snapshot OLD.json   # before
    (re-parse)
    cd backend && uv run python ../scripts/remap_chunk_ids.py apply OLD.json [--dry-run]

`snapshot` saves every text chunk's ID, document and text. `apply` finds each referenced old ID's
new chunk in the same document: by the stored quote where the reference keeps one (guidance items,
red flags, earnings dates, report sources, governance facts), else by the old chunk's own words.
A reference that cannot be matched keeps its old ID, which resolves to not-found (loudly), and is
listed. Table chunk IDs are unchanged by v5 and are left alone.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import ColumnClause, TableClause, column, select, table, update
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Session
from sqlalchemy.types import String, Text

from app.db import session_scope

# A text chunk ID from before v5: "{document}_{item}_{n}" (item may be dotted or part-qualified).
_OLD_ID = re.compile(r"\b\d+_[A-Za-z0-9.\-]+_p?\d+\b")
# A table chunk: "{document}_{item}_{table}". Its item changes when its section does (JPM's
# statements moved from Item 15 to Item 8); the document and table number do not.
_TABLE_ID = re.compile(r"\b(\d+)_[A-Za-z0-9.\-]+_(t\d+)\b")

_CHUNKS = table(
    "document_chunks",
    column("paragraph_id", String),
    column("document_id"),
    column("chunk_type", String),
    column("chunk_index"),
    column("text", Text),
)


def _norm(value: str) -> str:
    return " ".join(value.replace("\u2019", "'").split()).lower()


def _shingles(words: list[str], size: int = 8) -> set[str]:
    return {" ".join(words[i : i + size]) for i in range(max(1, len(words) - size + 1))}


def match_new_id(
    quote: str | None, old_id: str, old_text: str, candidates: list[tuple[str, str]]
) -> str | None:
    """The new chunk for an old one: the same ID if its text is unchanged; else, among the chunks
    holding `quote` (or all of them), the one sharing the most of the old text. Chunks overlap
    (each starts with the previous one's tail), so the first chunk holding the old chunk's opening
    words can be its neighbour; the best overlap is the chunk itself."""
    old_norm = _norm(old_text)
    texts = [(pid, _norm(body)) for pid, body in candidates]
    if any(pid == old_id and body == old_norm for pid, body in texts):
        return old_id
    if quote and (needle := _norm(quote)) and (holding := [t for t in texts if needle in t[1]]):
        texts = holding
    old = _shingles(old_norm.split())
    best, best_score = None, 0
    for pid, body in texts:
        score = len(old & _shingles(body.split()))
        if score > best_score:
            best, best_score = pid, score
    if best is None and quote and texts and len(texts) < len(candidates):
        return texts[0][0]  # the quote alone placed it (old text too short to compare)
    return best


class Remapper:
    def __init__(self, session: Session, old: dict[str, dict[str, Any]]) -> None:
        self.session = session
        self.old = old
        self._new: dict[int, list[tuple[str, str]]] = {}
        self.stats: Counter[str] = Counter()
        self.unmatched: list[str] = []

    def _candidates(self, document_id: int) -> list[tuple[str, str]]:
        if document_id not in self._new:
            rows = self.session.execute(
                select(_CHUNKS.c.paragraph_id, _CHUNKS.c.text)
                .where(_CHUNKS.c.document_id == document_id, _CHUNKS.c.chunk_type == "text")
                .order_by(_CHUNKS.c.chunk_index)
            )
            self._new[document_id] = [(r.paragraph_id, r.text) for r in rows]
        return self._new[document_id]

    def _table(self, old_id: str) -> str | None:
        """A table chunk's ID now: same document and table number, whatever its section."""
        m = _TABLE_ID.fullmatch(old_id)
        if m is None:
            return None
        rows = self.session.execute(
            select(_CHUNKS.c.paragraph_id).where(
                _CHUNKS.c.document_id == int(m.group(1)),
                _CHUNKS.c.paragraph_id.like(f"{m.group(1)}\\_%\\_{m.group(2)}"),
            )
        ).all()
        return rows[0].paragraph_id if len(rows) == 1 else None

    def resolve(self, where: str, old_id: str | None, quote: str | None = None) -> str | None:
        """The new ID for `old_id`, or None when it is not an old chunk or has no match."""
        snap = self.old.get(old_id or "")
        if snap is None:
            moved = self._table(old_id or "")
            if moved is not None and moved != old_id:
                self.stats[f"{where}: table moved"] += 1
                return moved
            return None
        new_id = match_new_id(
            quote, old_id or "", snap["text"], self._candidates(snap["document_id"])
        )
        self.stats[f"{where}: {'remapped' if new_id else 'unmatched'}"] += 1
        if new_id is None:
            self.unmatched.append(f"{where} {old_id}")
        return new_id

    def replace_ids(self, where: str, value: str, known: dict[str, str] | None = None) -> str:
        """Every old text chunk ID inside a serialised value: `known` matches first, else the
        old chunk's words."""
        known = known or {}
        pattern = re.compile(f"{_OLD_ID.pattern}|{_TABLE_ID.pattern}")
        return pattern.sub(
            lambda m: known.get(m.group(0)) or self.resolve(where, m.group(0)) or m.group(0), value
        )


def _table(name: str, *columns: ColumnClause[Any]) -> TableClause:
    return table(name, column("id"), *columns)


def _set(session: Session, t: TableClause, row_id: int, name: str, value: Any) -> None:
    session.execute(update(t).where(t.c.id == row_id).values({name: value}))


def apply(session: Session, old: dict[str, dict[str, Any]]) -> Remapper:
    r = Remapper(session, old)

    # References that keep their own quote: matched by the quote first.
    report_maps: dict[int, dict[str, str]] = defaultdict(dict)
    quoted = (
        ("guidance_items", "paragraph_id", "quote"),
        ("red_flags", "paragraph_id", "snippet"),
        ("earnings_events", "source_id", "quote"),
        ("research_sources", "source_id", "citation_text"),
    )
    for name, ref, quote in quoted:
        cols = [column(ref, String), column(quote, Text)]
        if name == "research_sources":
            cols.append(column("report_id"))
        t = _table(name, *cols)
        for row in session.execute(select(t)).mappings().all():
            new_id = r.resolve(name, row[ref], row[quote])
            if new_id:
                _set(session, t, row["id"], ref, new_id)
                if name == "research_sources":
                    report_maps[row["report_id"]][row[ref]] = new_id

    # A report's JSON cites the IDs its research_sources rows hold: reuse their matches.
    reports = _table("research_reports", column("report_json", JSONB))
    for row in session.execute(select(reports)).mappings().all():
        if row["report_json"] is None:
            continue
        body = json.dumps(row["report_json"])
        updated = r.replace_ids("research_reports", body, report_maps.get(row["id"]))
        if updated != body:
            _set(session, reports, row["id"], "report_json", json.loads(updated))

    # Governance facts cached on a proxy: each field is {value, quote, paragraph_id} or null.
    documents = _table("documents", column("metadata", JSONB))
    for row in session.execute(select(documents)).mappings().all():
        metadata = row["metadata"] or {}
        before = json.dumps(metadata)  # the loop below edits `metadata` in place
        for entry in (metadata.get("governance_facts") or {}).values():
            for fact in (entry.get("fields") or {}).values():
                if isinstance(fact, dict):
                    pid, quote = fact.get("paragraph_id"), fact.get("quote")
                    if new_id := r.resolve("governance_facts", pid, quote):
                        fact["paragraph_id"] = new_id
        body = json.dumps(metadata)
        updated = r.replace_ids("documents.metadata", body)  # any other cached IDs
        if updated != before:
            _set(session, documents, row["id"], "metadata", json.loads(updated))

    # IDs only: matched by the old chunk's words.
    for name, col in (
        ("earnings_briefs", "content"),
        ("notifications", "payload"),
        ("thesis_events", "payload"),
    ):
        t = _table(name, column(col, JSONB))
        for row in session.execute(select(t)).mappings().all():
            if row[col] is None:
                continue
            body = json.dumps(row[col])
            updated = r.replace_ids(f"{name}.{col}", body)
            if updated != body:
                _set(session, t, row["id"], col, json.loads(updated))
    for name, col in (("exec_comp_facts", "source_id"), ("thesis_events", "ref_id")):
        t = _table(name, column(col, String))
        for row in session.execute(select(t)).mappings().all():
            if row[col] and (updated := r.replace_ids(f"{name}.{col}", row[col])) != row[col]:
                _set(session, t, row["id"], col, updated)
    for name in ("chat_messages", "claim_verifications"):
        t = _table(name, column("source_ids", ARRAY(String)))
        for row in session.execute(select(t)).mappings().all():
            ids = list(row["source_ids"] or [])
            updated = [r.replace_ids(f"{name}.source_ids", s) for s in ids]
            if updated != ids:
                _set(session, t, row["id"], "source_ids", updated)
    return r


def snapshot(path: Path) -> None:
    with session_scope() as session:
        rows = session.execute(
            select(_CHUNKS.c.paragraph_id, _CHUNKS.c.document_id, _CHUNKS.c.text).where(
                _CHUNKS.c.chunk_type == "text"
            )
        )
        data = {r.paragraph_id: {"document_id": r.document_id, "text": r.text} for r in rows}
    path.write_text(json.dumps(data))
    print(f"saved {len(data)} text chunks to {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("step", choices=("snapshot", "apply"))
    parser.add_argument("path", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="report only; roll back")
    args = parser.parse_args()
    if args.step == "snapshot":
        snapshot(args.path)
        return
    old = json.loads(args.path.read_text())
    with session_scope() as session:
        result = apply(session, old)
        for key, count in sorted(result.stats.items()):
            print(f"{count:6d}  {key}")
        for line in result.unmatched[:50]:
            print("  unmatched:", line)
        if args.dry_run:
            session.rollback()
            print("dry run: rolled back")


if __name__ == "__main__":
    main()

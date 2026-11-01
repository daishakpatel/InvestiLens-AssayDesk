# ADR-0052: Lines break at block elements; text chunk IDs end in `_p{n}`; annual-report layouts

- **Status:** Accepted
- **Date:** 2026-10-31
- **Spec refs:** §13, DP-001, DP-006, CIT-002, FR-EX-001, FR-EX-012, FR-FN-001

## Problem

`linearize` read the page with a line break between every element. Inline XBRL wraps each figure
in its own element, so "commitments were $95.2 billion" came out as three lines, and in risk and
notes sections, where every short line starts a chunk, each fragment became its own chunk (median
chunk about 20 characters in Apple's and AMD's notes). Each table was also replaced with the
string `"<p>sentinel</p>"`, which the parser inserts as text, so every filing gained literal `<p>`
and `</p>` lines (about 7,100 chunks held nothing else). Separately, Intel's 10-K and 10-Qs print
no "Item N" headings, so each was one unsectioned "Filing text" block. Fixing the line breaks
renumbers chunks while keeping the item codes, so an old `{document}_{item}_{n}` would resolve to
different text: the silent failure ADR-0041 rules out.

## Decision

`PARSER_VERSION = "sec-ingest-v5"`. Lines break only after block elements (`p`, `div`, `br`, `li`,
`tr`, headings, lists, …); a table becomes a bare sentinel; a superscript (`<sup>`, or CSS
`vertical-align:super` / negative `top`) gets a space before it, so a footnote marker never joins
a figure ("$111,905 2", not "$111,9052"). Proxy headings drop a leading enumerator or bullet
before matching ("I. Summary compensation table", "•Advisory resolution…"). Text chunk IDs are
`{document}_{item}_p{n}`; table chunk IDs (`{document}_{item}_{table_id}`) are unchanged. A 10-K
or 10-Q with no item heading is sectioned by its own headings carrying standard item titles
(`_TITLE_ITEMS` in `sections.py`), with a few non-item headings ("Key Terms") ending a section.
Stored references made before v5 are remapped once by quote (`scripts/remap_chunk_ids.py`); one
that cannot be matched keeps its old ID and resolves to not-found.

## Consequences

Sentences and figures stay whole in chunks, quotes and the number check; notes chunks are real
paragraphs; Intel's sections feed management language, risk diffs and red flags. Every document
is re-chunked once, so its embeddings are paid for again (table chunks and unchanged text keep
theirs). Revisit with content-addressed chunk IDs if re-parses become routine.

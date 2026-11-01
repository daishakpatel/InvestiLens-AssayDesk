# ADR-0053: Report sections search their own filing sections, latest 10-K onward

- **Status:** Accepted
- **Date:** 2026-11-01
- **Spec refs:** §10, §16, RAG-013, RAG-014, RAG-016

## Problem

Each report section ran one fixed keyword query over every chunk the company has: five years of
10-Ks and 10-Qs, 8-K press releases (EX-99) and proxy statements. On the dev DB NVIDIA's bull/bear
pool cited crypto-mining demand from its FY2022–2025 10-Ks, its management commentary was ten
proxy passages (director bios, stock-ownership rules) and the 10-K's cybersecurity item, and AMD's
risks were ten press-release safe-harbor paragraphs. Retrieval had no filter beyond the company
(RAG-013 allows filing type, date, section and tier), and its recency boost (RAG-014) is a 10%
nudge meant for "latest" chat questions.

## Decision

`SearchScope(section_keys, filed_since)` (`app/rag/types.py`) is an optional argument to
`retrieve_evidence`, `vector_search` and `keyword_search`, applied as SQL filters on
`chunk_metadata.section_key` and `filing_date`. Each report section declares its scope in
`_SECTION_SCOPE` (`app/research/report.py`): MD&A (`10k_item7`, `10q_part1_item2`) for the prose
and management sections, risk factors (`10k_item1a`, `10q_part2_item1a`) for risks, the business
description (`10k_item1`) for the overview and summary, risks plus MD&A for bull/bear; every scope
starts at the latest parsed 10-K's filing date, so the evidence is the current annual report and
the 10-Qs after it. When a scope finds nothing (an unlabelled layout) the section falls back to the
unscoped search and logs `report.retrieve.unscoped`. Passages that are only a forward-looking-
statements disclaimer are dropped from report evidence. A filtered vector search sets
`hnsw.iterative_scan = strict_order` so HNSW keeps scanning until enough rows pass the filter;
that needs pgvector 0.8 or later (the `pgvector/pgvector:pg16` image has 0.8.6). Chat is unchanged.

## Consequences

Reports cite the company's current disclosures: on the dev DB every section's evidence is MD&A,
risk factors or the business description from the latest 10-K onward. Older risks no longer reach
a report even when still relevant, and earnings-release text (EX-99) no longer feeds the prose
sections (the news and guidance features read it separately). A hosted Postgres with pgvector
older than 0.8 rejects the setting, which aborts the transaction, so every report section would
come back empty (logged as `report.retrieve.failed`): check `SELECT extversion FROM pg_extension
WHERE extname = 'vector'` before deploying. Revisit if chat should use the same scoping, or if a "what
changed" section needs the prior year's filings.

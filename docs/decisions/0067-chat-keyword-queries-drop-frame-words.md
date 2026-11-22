# ADR-0067: Chat keyword and rerank queries drop frame words and the company's name

- **Status:** Accepted
- **Date:** 2026-11-21
- **Spec refs:** §16.1, RAG-010, RAG-011, RAG-012, ADR-0014

## Problem

Keyword search (any-word fallback, `ts_rank_cd`) and the `LexicalReranker` (share of query words
a passage contains) weigh every question word equally. In "How did management characterize
NVIDIA's capital spending?" four of six content words are frame or company words: "NVIDIA" is in
nearly every passage of NVIDIA's own filings and "management" in every MD&A heading, so press
releases and governance tables outranked the 10-Q passage that answers it. That passage was vector
search's #1 hit but fused 48th, past the 40-candidate cut. JPMorgan's "demand for products"
question failed the same way. Both were live false abstentions on 2026-11-21.

## Decision

The chat pipeline passes `retrieve_evidence(keyword_noise=…)`, a pattern of frame words (how, did,
what, why, explain, about, management, say, characterize, describe, discuss, comment) plus the
company's ticker and name words (legal suffixes dropped), possessive included. It is cut from the
keyword-search queries and the rerank query only; vector search keeps the full question. Report
sections pass nothing: their fixed queries use "management" on purpose.

## Consequences

Chat ranks on the words that name the content ("capital spending", "demand for products"). Mock
MRR rose from 0.72 to 0.81 with hit@5 unchanged. A question about another company keeps that name.
"Risk management" loses "management" for keyword ranking (vector search still sees it); a
learned reranker (ADR-0014) would make the list unnecessary.

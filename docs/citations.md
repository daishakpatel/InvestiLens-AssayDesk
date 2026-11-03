# Citation & Verification System (Phase 3a)

The layer that turns LLM output with `[SOURCE:<id>]` markers into verified, numbered, clickable
citations — and rejects anything it can't prove. Phase 3b (reports) and 3c (chat) call it; it
generates nothing itself. Sources: spec §13 (citations), §14 (hallucination/confidence), §15
(tiers), ADR-0004 (anchors), ADR-0015 (entailment verifier).

## Entry points (`app/citation/pipeline.py`)

- `verify_text(text, evidence)` — split prose with `[SOURCE:id]` markers into claims, verify each,
  render numbered citations.
- `verify_claims(raw_claims, evidence)` — same, for already-segmented structured claims (3b).
  Reports call `strip_markers` on each item first, because models also write markers inside JSON
  text. `strip_markers` reads `[SOURCE:a]`, `[source_id=a, source_id=b]`, `[cite: a; b]` and a bare
  ID with a known prefix (`[news:12]`); the IDs are then checked like any other.
- `persist(session, output, report_id=|chat_message_id=)` — log every claim to
  `claim_verifications` (CIT-005 L6).

`evidence` is a set of `EvidenceItem` (a `SourceRecord` + the text the LLM saw + retrieval score);
it is the single authority for ID validation (CIT-005 L1).

## Verification layers (CIT-005), in order

1. **ID validation** — any cited id not in the evidence set → reject `UNKNOWN_SOURCE` (CIT-A1).
2. **Numeric match** (deterministic) — every number/percent/money/date in the claim must be within
   tolerance of a value in the cited source, or (for percents) of a source ratio ×100, covering
   `derived_metric`/`xbrl_fact` values → else `NUMERIC_MISMATCH`.
3. **Entailment** (`Entailer` ABC, ADR-0015) — default `LexicalEntailer` (offline). A claim backed
   only by a structured value whose numbers matched is deterministically `supported`
   (HAL-002: calculation confidence = high). `unsupported` → reject `UNSUPPORTED`; `partially` →
   soften or reject per `citation_partial_policy`.
4. **Scope** — a causal claim needs causal language in a cited source; otherwise it is softened to
   correlation ("due to" → "amid") or rejected → `OVERREACH`.
5. **Coverage** — a factual sentence with no citation → reject `NO_CITATION`; a non-factual
   connective sentence is kept as-is.

## Confidence (HAL-002)

`score = w_tier·tier + w_sources·n + w_retrieval·r + w_entailment·e + w_agreement·a`, clamped to
[0,1], mapped to a **label only** (`strongly_supported ≥ 0.8`, `supported ≥ 0.55`, else
`limited_evidence`). The raw score stays in `VerifiedClaim.internal_confidence` (excluded from
serialization). Weights/thresholds are config-driven and calibrated in Phase 5b.

## Tiers (§15)

`app/citation/tiers.py` maps source/document type → tier (1 SEC filings/XBRL/derived, 2 earnings
releases, 3 IR/transcripts, 4 licensed news, 5 other). Used in both retrieval reranking (Phase 2c)
and confidence scoring here.

## Rendering (CIT-006)

Accepted/softened claims' validated source IDs are numbered in first-appearance order, appended as
`[n]`, and collected into a reference list (`Citation`). Rejected claims are dropped from the
rendered text (still logged). `sufficient=False` when every factual claim was rejected (HAL-003).

## Source resolution — `GET /sources/{source_id}` (CIT-006)

`app/citation/resolver.py` resolves a backend-issued id to a `SourceDetail` (record + marked span +
deep link; derived metrics also return lineage inputs, CIT-003). Id scheme:

| prefix / shape | type |
|----------------|------|
| `news:<id>` | news_item |
| `price:<TICKER>:<date>` or `price:<TICKER>:<date>:<end_date>` | price_bar (Tier 4): that trading day's open/high/low/close/volume, or the two days' closes, as reported by the provider; exact trading days only (ADR-0054) |
| `xbrl:<TICKER>:<metric>:FY<year>` | xbrl_fact: a base metric from `financial_metrics`, one ID per metric and period (ADR-0034) |
| `derived:<TICKER>:<name>:<period>` | derived_metric: a ratio or a derived Q4 value, with lineage |
| `xbrl:<fact_id>` (integer) | xbrl_fact: a raw `financial_facts` row |
| chunk `paragraph_id` | text_chunk / table_chunk, or earnings_release/transcript if the parent document is one |

## Deferred

- Numeric **auto-repair** (substitute the backend's correct value) — currently reject-only; the
  structured value is available for a later repair pass.
- LLM/NLI entailment verifier and numeric/entailment **eval calibration** → Phase 5b (ADR-0015).
- `research_sources` population and the regeneration retry on `NO_CITATION` → Phase 3b.

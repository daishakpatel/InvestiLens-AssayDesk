# ADR-0061: Report share links are plain public links, not revocable tokens

- **Status:** Accepted
- **Date:** 2026-11-14
- **Spec refs:** §10.16 (shareable read-only links, "opt-in, revocable"), SEC-007

## Problem

§10.16 asks for opt-in, revocable read-only links to a report. But reports are already public:
`GET /research/{id}` needs no sign-in, `GET /companies/{ticker}/research/latest` shows any reader
the newest report whatever user generated it, and the report diff (ADR-0059) compares across every
report of a company. A revocable token only protects something private, so honouring the spec's
wording would first mean making user-generated reports owner-only and teaching `/latest` and the
diff to skip other readers' private reports.

## Decision

Reports stay public: they are built only from public filings, prices and news, and they are shared
per company, not per user. Sharing is a plain link: the frontend route `/report/:researchId` loads
one stored report through the existing `GET /research/{id}`, and the report header gains a
"Copy link" button. No token, no new endpoint, no migration. The project owner chose this on
2026-11-14 over the private-plus-token design.

## Consequences

Anyone with a link (or who guesses a sequential id) can read that report, which is no more than
they can already read via `/latest`. Links can't be revoked or expire. Revisit if reports ever take
private input (portfolio holdings, a user's notes or thesis): that would need owner-only reports,
random share tokens and revocation, plus the `/latest` and diff changes above.

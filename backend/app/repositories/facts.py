"""Financial-facts persistence.

Idempotency (ING-001): a company's raw XBRL facts are replaced wholesale on each ingest —
delete-then-insert keyed by company — so re-running yields an identical set with no duplicates.
Point-in-time is preserved because every fact keeps its own `accession_number` and `filed_date`
(DR-023); facts from all accessions in companyfacts are retained.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models import FinancialFact


def replace_company_facts(session: Session, *, company_id: int, facts: list[dict[str, Any]]) -> int:
    """Delete all facts for a company, then bulk-insert `facts`. Returns rows inserted."""
    session.execute(delete(FinancialFact).where(FinancialFact.company_id == company_id))
    if facts:
        session.bulk_insert_mappings(FinancialFact, facts)
    return len(facts)


def facts_for_company(session: Session, company_id: int) -> list[FinancialFact]:
    """Return a company's company-wide XBRL facts (input to the concept-map builder). Per-class
    cover-page counts (`dimensions` set, ADR-0074) are left out: they are not company totals."""
    from sqlalchemy import select

    facts = session.scalars(select(FinancialFact).where(FinancialFact.company_id == company_id))
    return [f for f in facts if not f.dimensions]  # stored as JSON null, so filtered here

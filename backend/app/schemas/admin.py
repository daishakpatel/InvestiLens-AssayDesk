"""Admin (internal) response schemas (spec §24.2)."""

from __future__ import annotations

from pydantic import BaseModel


class IngestionRunSummary(BaseModel):
    id: str
    source: str
    status: str | None = None
    started_at: str | None = None
    ended_at: str | None = None


class EvalRunSummary(BaseModel):
    id: str
    name: str | None = None
    status: str | None = None
    git_sha: str | None = None


class RefreshAccepted(BaseModel):
    ticker: str
    job_id: str
    status: str = "queued"


class RejectedClaim(BaseModel):
    claim_id: str | None = None
    text: str | None = None
    status: str | None = None  # rejected | softened
    reason_code: str | None = None
    source_ids: list[str] = []
    entailment_label: str | None = None
    numeric_match: bool | None = None


class RejectedClaims(BaseModel):
    """Claims validation removed or softened in one report (spec §10.16, CIT-005)."""

    research_id: str
    checked: int  # every claim the validator saw, accepted included
    counts: dict[str, int]  # reason_code -> number of rejected/softened claims
    claims: list[RejectedClaim]

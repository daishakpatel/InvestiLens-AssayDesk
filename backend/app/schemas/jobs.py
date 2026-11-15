"""Research job request/response and job-status schemas (spec §24.2, §25.1)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.research import ResearchReport

JobStatus = Literal["queued", "processing", "done", "failed"]


class ResearchRequest(BaseModel):
    ticker: str
    force_refresh: bool = False


class ResearchAccepted(BaseModel):
    """202 response when a research job is queued."""

    research_id: str
    job_id: str
    status: JobStatus = "processing"


class JobStage(BaseModel):
    name: str
    status: Literal["pending", "running", "done", "failed"]
    message: str | None = None


class JobState(BaseModel):
    status: JobStatus
    progress: int = Field(ge=0, le=100)
    stage: str | None = None
    stages: list[JobStage] = []


class ResearchSummary(BaseModel):
    """One row of a reader's research history (§26.2): enough to list it and link to it."""

    research_id: str
    ticker: str
    generated_at: str | None = None
    model: str | None = None


class ResearchReportEnvelope(BaseModel):
    research_id: str
    ticker: str
    generated_at: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    data_version: str | None = None
    report: ResearchReport

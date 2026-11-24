"""Company-comparison endpoints (§37.1): deterministic side-by-side, peer suggestions, and
AI commentary.

The deterministic comparison and peer suggestions are public reads (like the other company data
endpoints). AI commentary is a POST gated by auth + AI budget — it calls the LLM, so it follows the
same cost-control pattern as research/chat and never returns a recommendation (LGL-006).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_optional_user_id
from app.billing.budget import check_budget
from app.comparison.commentary import generate_commentary
from app.comparison.engine import compare_companies, gather
from app.comparison.peers import suggest_peers
from app.db import get_db
from app.models import Company, User
from app.repositories import peers as peer_repo
from app.schemas.citations import VerifiedOutput
from app.schemas.comparison import (
    ComparisonCommentaryRequest,
    ComparisonResponse,
    PeerSuggestionsResponse,
)

router = APIRouter(prefix="/compare", tags=["Comparison"])

_DB = Depends(get_db, scope="function")
_USER = Depends(get_current_user)
_MAYBE_USER = Depends(get_optional_user_id)
_MIN_TICKERS = 2
_MAX_TICKERS = 6


def _parse_csv(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse_tickers(raw: str) -> list[str]:
    tickers = _parse_csv(raw) or []
    if not (_MIN_TICKERS <= len(tickers) <= _MAX_TICKERS):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"compare between {_MIN_TICKERS} and {_MAX_TICKERS} tickers",
        )
    return tickers


@router.get("", response_model=ComparisonResponse)
def compare(
    tickers: str | None = Query(None, description="Comma-separated tickers, e.g. NVDA,AMD,INTC"),
    metrics: str | None = Query(None, description="Optional comma-separated metric names"),
    period: str | None = Query(None, description="Calendar year or fiscal period; default latest"),
    peer_set_id: int | None = Query(
        None, description="Compare a company with a peer set instead of listing tickers (7h)"
    ),
    db: Session = _DB,
    user_id: int | None = _MAYBE_USER,
) -> ComparisonResponse:
    """Deterministic, calendarized, percentile-ranked comparison (no AI)."""
    if peer_set_id is not None:
        names = _peer_set_tickers(db, peer_set_id, user_id)
    elif tickers is not None:
        names = _parse_tickers(tickers)
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "give tickers or peer_set_id")
    try:
        return compare_companies(db, names, metrics=_parse_csv(metrics), period=period)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


def _peer_set_tickers(db: Session, peer_set_id: int, user_id: int | None) -> list[str]:
    """The set's company first, then its members; a set the caller cannot see is 404."""
    peer_set = peer_repo.get_visible(db, peer_set_id, user_id)
    if peer_set is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "peer set not found")
    company = db.get(Company, peer_set.company_id)
    members = [c.ticker for _, c in peer_repo.members(db, [peer_set.id])]
    if company is None or not members:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "the peer set has no members")
    return [company.ticker, *members]


@router.get("/peers/{ticker}", response_model=PeerSuggestionsResponse)
def peers(
    ticker: str,
    limit: int = Query(5, ge=1, le=20),
    db: Session = _DB,
) -> PeerSuggestionsResponse:
    """Suggest comparable companies by SIC/industry + market-cap band (§37.1)."""
    try:
        return suggest_peers(db, ticker, limit=limit)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/commentary", response_model=VerifiedOutput)
def commentary(
    body: ComparisonCommentaryRequest,
    db: Session = _DB,
    user: User = _USER,
) -> VerifiedOutput:
    """AI commentary on the differences — cited + verified (Phase 3a), non-advisory (LGL-006)."""
    check_budget(db, user)
    try:
        g = gather(db, body.tickers, metrics=body.metrics, period=body.period)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    out = generate_commentary(db, g, user_id=user.id)
    db.commit()  # keeps the llm_calls row the budget check reads
    return out

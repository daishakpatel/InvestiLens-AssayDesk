"""Watchlist, alerts, notifications, and feedback endpoints (spec §24.2, §26.2).

User-scoped CRUD over `watchlists`/`watchlist_items`/`alerts`; the authenticated user comes from
the `get_current_user_id` seam (ADR-0006). Alert *dispatch* — checking for new filings/price
moves/news and writing `notifications` — runs as the `send_alerts` background job (Phase 5d);
this router exposes the CRUD plus the read side of the notifications that dispatch produces.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.alerts import registry as alert_registry
from app.alerts.registry import AlertEventType as AlertEventTypeDef
from app.api.deps import get_current_user_id
from app.api.errors import ProblemException
from app.api.pagination import NEXT_CURSOR_HEADER, clamp_limit, decode_cursor, encode_cursor
from app.api.research import report_body
from app.config import get_settings
from app.db import get_db
from app.models import Alert as AlertRow
from app.models import Company
from app.repositories import alerts as alert_repo
from app.repositories import companies as company_repo
from app.repositories import notifications as notif_repo
from app.repositories import reports as report_repo
from app.repositories import watchlists as watchlist_repo
from app.schemas.chat import ReportFeedbackRequest
from app.schemas.common import FieldError
from app.schemas.watchlist import (
    Alert,
    AlertCreate,
    AlertEventType,
    AlertUpdate,
    Notification,
    Watchlist,
    WatchlistCreate,
    WatchlistItem,
)

router = APIRouter(tags=["Watchlist & Alerts"])

_DB = Depends(get_db, scope="function")
_USER = Depends(get_current_user_id)


@router.get("/watchlists", response_model=list[Watchlist])
def list_watchlists(db: Session = _DB, user_id: int = _USER) -> list[Watchlist]:
    rows = watchlist_repo.list_for_user(db, user_id=user_id)
    return [
        Watchlist(
            id=str(w.id),
            name=w.name,
            items=[
                WatchlistItem(ticker=t) for t in watchlist_repo.item_tickers(db, watchlist_id=w.id)
            ],
        )
        for w in rows
    ]


@router.post("/watchlists", status_code=201, response_model=Watchlist)
def create_watchlist(body: WatchlistCreate, db: Session = _DB, user_id: int = _USER) -> Watchlist:
    row = watchlist_repo.create(db, user_id=user_id, name=body.name)
    db.commit()
    return Watchlist(id=str(row.id), name=row.name, items=[])


@router.delete("/watchlists/{watchlist_id}", status_code=204)
def delete_watchlist(watchlist_id: int, db: Session = _DB, user_id: int = _USER) -> None:
    if not watchlist_repo.delete_for_user(db, user_id=user_id, watchlist_id=watchlist_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "watchlist not found")
    db.commit()


def _alert_out(row: AlertRow, ticker: str | None) -> Alert:
    return Alert(
        id=str(row.id),
        event_type=row.event_type,
        ticker=ticker,
        params=row.params,
        channels=row.channels,  # type: ignore[arg-type]  # DB strings → Literal
        enabled=row.enabled,
        supported=alert_registry.get(row.event_type) is not None,
        created_at=row.created_at.isoformat(),
    )


def _validated_params(event_type: AlertEventTypeDef, params: dict[str, Any]) -> dict[str, Any]:
    """Params checked against the event type's model, with its defaults filled in (FR-AL-009)."""
    try:
        return event_type.params_schema.model_validate(params).model_dump(mode="json")
    except ValidationError as exc:
        raise ProblemException(
            422,
            "Validation Error",
            "One or more fields failed validation.",
            errors=[
                FieldError(
                    field=".".join(["body", "params", *(str(p) for p in e["loc"])]),
                    message=e["msg"],
                )
                for e in exc.errors()
            ],
        ) from exc


@router.get("/alerts/event-types", response_model=list[AlertEventType])
def list_alert_event_types() -> list[AlertEventType]:
    """The alert kinds a user can choose from, with each one's params as JSON Schema."""
    return [
        AlertEventType(
            key=t.key,
            label=t.label,
            description=t.description,
            params_schema=t.params_schema.model_json_schema(),
            default_enabled=t.default_enabled,
        )
        for t in alert_registry.all_types()
    ]


@router.get("/alerts", response_model=list[Alert])
def list_alerts(db: Session = _DB, user_id: int = _USER) -> list[Alert]:
    return [
        _alert_out(row, ticker) for row, ticker in alert_repo.list_for_user(db, user_id=user_id)
    ]


@router.post("/alerts", status_code=201, response_model=Alert)
def create_alert(body: AlertCreate, db: Session = _DB, user_id: int = _USER) -> Alert:
    event_type = alert_registry.get(body.event_type)
    if event_type is None:
        raise ProblemException(
            422,
            "Validation Error",
            "One or more fields failed validation.",
            errors=[FieldError(field="body.event_type", message="unknown alert event type")],
        )
    params = _validated_params(event_type, body.params)
    company = None
    if body.ticker is not None:
        company = company_repo.get_by_ticker(db, body.ticker)
        if company is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"company {body.ticker.upper()} not ingested"
            )
    limit = get_settings().alert_max_per_user
    if alert_repo.count_for_user(db, user_id=user_id) >= limit:
        raise ProblemException(
            422, "Too many alerts", f"You can have at most {limit} alerts. Delete one first."
        )
    row = alert_repo.create(
        db,
        user_id=user_id,
        company_id=company.id if company else None,
        event_type=event_type.key,
        params=params,
        channels=list(dict.fromkeys(body.channels)),
        enabled=body.enabled,
    )
    db.commit()
    return _alert_out(row, company.ticker if company else None)


@router.patch("/alerts/{alert_id}", response_model=Alert)
def update_alert(
    alert_id: int, body: AlertUpdate, db: Session = _DB, user_id: int = _USER
) -> Alert:
    row = alert_repo.get_for_user(db, user_id=user_id, alert_id=alert_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "alert not found")
    if body.params is not None:
        event_type = alert_registry.get(row.event_type)
        if event_type is None:
            raise ProblemException(
                422, "Unsupported alert", "This alert type is no longer supported."
            )
        row.params = _validated_params(event_type, body.params)
    if body.channels is not None:
        row.channels = list(dict.fromkeys(body.channels))
    if body.enabled is not None:
        row.enabled = body.enabled
    db.commit()
    company = db.get(Company, row.company_id) if row.company_id is not None else None
    return _alert_out(row, company.ticker if company else None)


@router.delete("/alerts/{alert_id}", status_code=204)
def delete_alert(alert_id: int, db: Session = _DB, user_id: int = _USER) -> None:
    if not alert_repo.delete_for_user(db, user_id=user_id, alert_id=alert_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "alert not found")
    db.commit()


@router.get("/notifications", response_model=list[Notification], tags=["Notifications"])
def list_notifications(
    response: Response,
    limit: int | None = None,
    cursor: str | None = None,
    db: Session = _DB,
    user_id: int = _USER,
) -> list[Notification]:
    """The current user's in-app notifications, newest first (from the alert-dispatch job)."""
    page_size = clamp_limit(limit)
    rows = notif_repo.list_for_user(
        db, user_id=user_id, limit=page_size + 1, after_id=decode_cursor(cursor)
    )
    has_more = len(rows) > page_size
    rows = rows[:page_size]
    if has_more and rows:
        response.headers[NEXT_CURSOR_HEADER] = encode_cursor(rows[-1].id)
    tickers = company_repo.tickers_by_id(db, {n.company_id for n in rows if n.company_id})
    return [
        Notification(
            id=str(n.id),
            title=n.title,
            body=n.body,
            channel=n.channel,  # type: ignore[arg-type]  # DB string → Literal
            company_id=str(n.company_id) if n.company_id is not None else None,
            ticker=tickers.get(n.company_id) if n.company_id is not None else None,
            event_type=(n.payload or {}).get("event_type"),
            severity=(n.payload or {}).get("severity"),
            source_ids=(n.payload or {}).get("source_ids", []),
            read=n.read_at is not None,
            created_at=n.created_at.isoformat(),
        )
        for n in rows
    ]


@router.post("/notifications/{notification_id}/read", status_code=204, tags=["Notifications"])
def mark_notification_read(notification_id: int, db: Session = _DB, user_id: int = _USER) -> None:
    if not notif_repo.mark_read(db, user_id=user_id, notification_id=notification_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "notification not found")
    db.commit()


_CLAIM_SECTIONS = (
    "executive_summary",
    "company_overview",
    "revenue_analysis",
    "profitability_analysis",
    "balance_sheet_analysis",
    "cash_flow_analysis",
    "valuation_analysis",
)


@router.post("/feedback/report", status_code=204, tags=["Feedback"])
def report_feedback(body: ReportFeedbackRequest, db: Session = _DB, user_id: int = _USER) -> None:
    """Thumbs up/down on a report or one of its claims, kept for evaluation (spec §18.5)."""
    report = report_repo.get_report(db, int(body.report_id)) if body.report_id.isdigit() else None
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report not found")
    if body.claim_id is not None:
        content = report_body(report)
        claims = {c.claim_id for name in _CLAIM_SECTIONS for c in getattr(content, name)}
        claims |= {
            c.claim_id for f in content.bull_factors + content.bear_factors for c in f.claims
        }
        if body.claim_id not in claims:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "claim not found in this report")
    report_repo.set_feedback(
        db,
        user_id=user_id,
        report_id=report.id,
        claim_id=body.claim_id,
        rating=1 if body.rating == "up" else -1,
        reason=body.reason,
    )
    db.commit()

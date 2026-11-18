"""Notification delivery (§26.2, SEC-014, ADR-0025, ADR-0064).

Every notification is persisted as an in-app row (durable, readable via `GET /notifications`).
When the alert's channel is `email`, that row is also the outbox: `send_pending_emails` sends it
after the dispatch that created it has committed, through the configured email backend:
- `LogEmailBackend` (default) — no SMTP provider is configured in this project, so email is
  logged (PII-safe: recipient + subject, never body secrets) rather than silently dropped.
- `SmtpEmailBackend` — real SMTP when the SMTP_* settings are provided.

Keeping email behind an interface means the alert-dispatch job never knows which backend is live,
and tests use the log backend with zero network.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import Notification, User
from app.repositories import notifications as notif_repo
from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)


class EmailBackend(ABC):
    @abstractmethod
    def send(self, *, to: str, subject: str, body: str) -> None: ...


class LogEmailBackend(EmailBackend):
    """Logs the delivery instead of sending (no provider configured). PII-safe: no body in logs."""

    def send(self, *, to: str, subject: str, body: str) -> None:
        log_event(logger, logging.INFO, "notification.email.logged", to=to, subject=subject)


class SmtpEmailBackend(EmailBackend):
    def __init__(self, settings: Settings) -> None:
        self._s = settings

    def send(self, *, to: str, subject: str, body: str) -> None:
        import smtplib
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["From"] = self._s.smtp_from
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP(self._s.smtp_host, self._s.smtp_port) as smtp:
            smtp.starttls()
            if self._s.smtp_user:
                smtp.login(self._s.smtp_user, self._s.smtp_password)
            smtp.send_message(msg)


def get_email_backend(settings: Settings | None = None) -> EmailBackend:
    settings = settings or get_settings()
    if settings.notification_email_backend == "smtp" and settings.smtp_host:
        return SmtpEmailBackend(settings)
    return LogEmailBackend()


class NotificationSender:
    """Persists a notification once per (alert, event_key); its email is sent later, from the
    committed row, by `send_pending_emails` (ADR-0064)."""

    def send(
        self,
        session: Session,
        *,
        user: User,
        alert_id: int,
        company_id: int | None,
        event_key: str,
        title: str,
        body: str,
        channels: list[str],
        payload: dict[str, Any],
    ) -> bool:
        """Create the notification. False when it was already delivered (FR-AL-007)."""
        created = notif_repo.create_once(
            session,
            user_id=user.id,
            alert_id=alert_id,
            company_id=company_id,
            event_key=event_key,
            title=title,
            body=body,
            channel="email" if "email" in channels else "in_app",
            payload=payload,
        )
        return created is not None


def send_pending_emails(session: Session, backend: EmailBackend | None = None) -> int:
    """Send every committed email notification not yet sent; returns how many went out.

    Each row is claimed with `FOR UPDATE SKIP LOCKED`, so overlapping runs never send the same
    one, and marked and committed on its own, so a crash repeats at most one email. A send that
    fails is logged and stays pending for the next run."""
    backend = backend or get_email_backend()
    failed: list[int] = []
    sent = 0
    while True:
        row = session.scalar(
            select(Notification)
            .where(
                Notification.channel == "email",
                Notification.emailed_at.is_(None),
                Notification.id.not_in(failed),
            )
            .order_by(Notification.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return sent
        user = session.get(User, row.user_id)
        try:
            if user is None:
                raise LookupError("recipient gone")
            backend.send(to=user.email, subject=row.title, body=row.body or "")
        except Exception as exc:  # one bad send must not hold up the rest
            log_event(
                logger,
                logging.ERROR,
                "notification.email.failed",
                notification_id=row.id,
                error=type(exc).__name__,
            )
            failed.append(row.id)
            continue
        row.emailed_at = datetime.now(UTC)
        session.commit()
        sent += 1

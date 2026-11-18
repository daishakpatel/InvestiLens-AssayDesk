"""notifications.emailed_at: the email outbox

ADR-0064: dispatch used to send an alert email inside its transaction, so a crash before the
final commit rolled the notification back and the next run emailed the whole batch again. Email
now goes out after the notification is committed, one row at a time, and `emailed_at` records
it. Existing email rows were sent under the old flow, so they are marked sent now.

Revision ID: d4f9b2a6c8e1
Revises: c3e8a1f5d7b2
Create Date: 2026-11-17 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4f9b2a6c8e1"
down_revision: str | None = "c3e8a1f5d7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("notifications", sa.Column("emailed_at", sa.DateTime(timezone=True)))
    op.execute("UPDATE notifications SET emailed_at = created_at WHERE channel = 'email'")


def downgrade() -> None:
    op.drop_column("notifications", "emailed_at")

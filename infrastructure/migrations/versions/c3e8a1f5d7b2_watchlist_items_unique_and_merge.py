"""watchlist_items unique per list; merge one-company stand-in lists into one list per user

ADR-0062 (replaces the ADR-0031 stand-in): until now the frontend stored each watched company as
its own empty watchlist named by its ticker, because the API could not add items. Now that it can,
each user's stand-in lists become items on one list (their oldest stand-in, renamed "Watchlist"),
so watchlist-wide alerts, the red-flag watchlist feed and earnings-brief eligibility, which all
read `watchlist_items`, finally see those companies. Lists with real items or a non-ticker name
are left alone.

Downgrade drops the constraint only; the merge is not undone (the stand-ins carried no data
beyond the ticker, which the merged list still holds).

Revision ID: c3e8a1f5d7b2
Revises: 69ec64a10d9e
Create Date: 2026-11-15 00:00:00.000000
"""

from collections import defaultdict
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3e8a1f5d7b2"
down_revision: str | None = "69ec64a10d9e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UQ = "uq_watchlist_items_watchlist_id_company_id"


def upgrade() -> None:
    bind = op.get_bind()
    # Duplicates would block the constraint; keep the first row of each (none expected).
    bind.execute(
        sa.text(
            "DELETE FROM watchlist_items a USING watchlist_items b "
            "WHERE a.watchlist_id = b.watchlist_id AND a.company_id = b.company_id AND a.id > b.id"
        )
    )
    op.create_unique_constraint(_UQ, "watchlist_items", ["watchlist_id", "company_id"])

    standins = bind.execute(
        sa.text(
            "SELECT w.id, w.user_id, c.id FROM watchlists w JOIN companies c ON c.ticker = w.name "
            "WHERE NOT EXISTS (SELECT 1 FROM watchlist_items i WHERE i.watchlist_id = w.id) "
            "ORDER BY w.id"
        )
    ).all()
    by_user: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for list_id, user_id, company_id in standins:
        by_user[user_id].append((list_id, company_id))

    for entries in by_user.values():
        target = entries[0][0]  # oldest stand-in keeps its id, so it stays first in the UI
        for list_id, company_id in entries:  # oldest first, so items keep the watch order
            bind.execute(
                sa.text(
                    "INSERT INTO watchlist_items (watchlist_id, company_id, added_at) "
                    "VALUES (:w, :c, now()) ON CONFLICT DO NOTHING"
                ),
                {"w": target, "c": company_id},
            )
            if list_id != target:
                bind.execute(sa.text("DELETE FROM watchlists WHERE id = :w"), {"w": list_id})
        bind.execute(sa.text("UPDATE watchlists SET name = 'Watchlist' WHERE id = :w"), {"w": target})


def downgrade() -> None:
    op.drop_constraint(_UQ, "watchlist_items", type_="unique")

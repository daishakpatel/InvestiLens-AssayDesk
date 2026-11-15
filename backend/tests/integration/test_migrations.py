"""Migration up/down tests on empty and populated databases (DB-001).

Covers Phase 0b Definition of Done: the initial migration applies and reverses cleanly, the
pgvector extension and the special indexes are created, and the seed script populates every
table. References: DB-001 (migrations tested up/down), DB-003 (no float money columns),
ADR-0002/ADR-0010 (pgvector, HNSW), CIT-002 (chunk anchors).
"""

from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path
from types import ModuleType

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Base
from tests.integration.conftest import public_table_names

_MODEL_TABLES = set(Base.metadata.tables)


def _seed_module() -> ModuleType:
    """Import scripts/seed.py by path (it lives outside the package)."""
    seed_path = Path(__file__).resolve().parents[3] / "scripts" / "seed.py"
    spec = importlib.util.spec_from_file_location("assay_desk_seed", seed_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_creates_every_model_table(alembic_cfg: Config, test_engine: Engine) -> None:
    command.upgrade(alembic_cfg, "head")
    tables = public_table_names(test_engine)
    assert tables >= _MODEL_TABLES  # DB-001: every model table exists
    assert "alembic_version" in tables


def test_pgvector_extension_and_special_indexes(alembic_cfg: Config, test_engine: Engine) -> None:
    command.upgrade(alembic_cfg, "head")
    with test_engine.connect() as conn:
        ext = conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).scalar()
        assert ext == 1  # ADR-0002 / ADR-0010

        hnsw = conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE indexname = 'ix_document_chunks_embedding_hnsw'"
            )
        ).scalar()
        assert hnsw is not None and "hnsw" in hnsw and "vector_cosine_ops" in hnsw

        gin_am = conn.execute(
            text(
                "SELECT am.amname FROM pg_class c "
                "JOIN pg_am am ON am.oid = c.relam "
                "WHERE c.relname = 'ix_document_chunks_tsv'"
            )
        ).scalar()
        assert gin_am == "gin"  # CIT-002 keyword search


def test_no_float_money_columns(alembic_cfg: Config, test_engine: Engine) -> None:
    command.upgrade(alembic_cfg, "head")
    with test_engine.connect() as conn:
        floats = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema = 'public' AND data_type IN ('double precision', 'real')"
            )
        ).scalar()
    assert floats == 0  # DB-003


def test_downgrade_leaves_empty_schema(alembic_cfg: Config, test_engine: Engine) -> None:
    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "base")
    tables = public_table_names(test_engine)
    assert tables & _MODEL_TABLES == set()  # only alembic_version may remain


def test_up_down_up_on_populated_db(alembic_cfg: Config, test_engine: Engine) -> None:
    command.upgrade(alembic_cfg, "head")

    # Seed against the test engine directly (avoids app.db's cached dev-DB engine).
    seed = _seed_module()
    with Session(test_engine) as session:
        count = seed.seed_into(session)
        session.commit()
    assert count == 3  # NVDA, AAPL, JPM

    with test_engine.connect() as conn:
        companies = conn.execute(text("SELECT count(*) FROM companies")).scalar()
    assert companies == 3

    # Downgrade must succeed even with data present (FK cascades), then re-upgrade cleanly.
    command.downgrade(alembic_cfg, "base")
    assert public_table_names(test_engine) & _MODEL_TABLES == set()
    command.upgrade(alembic_cfg, "head")
    assert public_table_names(test_engine) >= _MODEL_TABLES


def test_alert_framework_v2_migration_backfills_and_reverses(
    alembic_cfg: Config, test_engine: Engine
) -> None:
    """FR-AL-005 / ADR-0039: existing alerts get `watermark_at = now()` at migration time (not
    their age, so deploying replays nothing), per-form filing types fold into `new_filing`, and
    the downgrade restores the v1 shape."""
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "8b0a53766648")
    with test_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (email, password_hash, role, is_active)"
                " VALUES ('m@example.com', 'x', 'user', true);"
                "INSERT INTO companies (ticker, cik, name, status)"
                " VALUES ('MIG', '0000000042', 'Mig', 'active');"
                "INSERT INTO alerts (user_id, company_id, alert_type, config, channel,"
                " last_triggered_at, is_active) SELECT u.id, c.id, 'new_10q', NULL, 'email',"
                " now() - interval '90 days', true FROM users u, companies c;"
                "INSERT INTO alerts (user_id, company_id, alert_type, config, channel, is_active)"
                " SELECT u.id, c.id, 'price_move', '{\"threshold\": 0.1}', 'in_app', true"
                " FROM users u, companies c;"
            )
        )
        before: datetime = conn.execute(text("SELECT now()")).scalar_one()

    command.upgrade(alembic_cfg, "e4a7c2d91b30")
    with test_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT event_type, params, channels, enabled, watermark_at FROM alerts ORDER BY id"
            )
        ).all()
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
        ("new_filing", {"form_types": ["10-Q"]}, ["email"], True),
        ("price_move", {"threshold": 0.1}, ["in_app"], True),
    ]
    assert all(r[4] >= before for r in rows)

    command.downgrade(alembic_cfg, "8b0a53766648")
    with test_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT alert_type, config, channel, is_active FROM alerts ORDER BY id")
        ).all()
    assert [tuple(r) for r in rows] == [
        ("new_10q", {}, "email", True),
        ("price_move", {"threshold": 0.1}, "in_app", True),
    ]
    command.upgrade(alembic_cfg, "head")


def test_metric_gaps_migration_up_and_down(alembic_cfg: Config, test_engine: Engine) -> None:
    """FR-QT-010 / ADR-0040: `metric_gaps` holds one reason per (company, metric, period) and
    cascades with its company; the downgrade drops it and nothing else."""
    command.upgrade(alembic_cfg, "head")
    insert_gap = (
        "INSERT INTO metric_gaps (company_id, metric, period_label, reason)"
        " SELECT id, 'operating_cash_flow', 'Q2-FY2026', :reason FROM companies"
        " WHERE ticker = 'GAP'"
    )
    with test_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO companies (ticker, cik, name, status)"
                " VALUES ('GAP', '0000000043', 'Gap', 'active')"
            )
        )
        conn.execute(text(insert_gap), {"reason": "missing_ytd_2q"})
        detected: datetime | None = conn.execute(
            text("SELECT detected_at FROM metric_gaps")
        ).scalar_one()
        assert detected is not None  # server default now()
    with pytest.raises(IntegrityError), test_engine.begin() as conn:
        conn.execute(text(insert_gap), {"reason": "missing_fy"})  # same (company, metric, period)
    with test_engine.begin() as conn:
        conn.execute(text("DELETE FROM companies WHERE ticker = 'GAP'"))
        assert conn.execute(text("SELECT count(*) FROM metric_gaps")).scalar_one() == 0

    command.downgrade(alembic_cfg, "e4a7c2d91b30")
    assert "metric_gaps" not in public_table_names(test_engine)
    assert "financial_metrics" in public_table_names(test_engine)
    command.upgrade(alembic_cfg, "head")


def test_watchlist_standins_merge_into_one_list(alembic_cfg: Config, test_engine: Engine) -> None:
    """ADR-0062: each user's one-company lists named by ticker (the ADR-0031 stand-in) become
    items on one list, oldest first; lists with items or a non-ticker name are untouched."""
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "69ec64a10d9e")
    with test_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (email, password_hash, role, is_active) VALUES"
                " ('a@example.com', 'x', 'user', true), ('b@example.com', 'x', 'user', true);"
                "INSERT INTO companies (ticker, cik, name, status) VALUES"
                " ('NVDA', '0000000001', 'Nvidia', 'active'),"
                " ('AMD', '0000000002', 'AMD', 'active');"
                "INSERT INTO watchlists (user_id, name) SELECT id, 'NVDA' FROM users"
                " WHERE email = 'a@example.com';"
                "INSERT INTO watchlists (user_id, name) SELECT id, 'Chips' FROM users"
                " WHERE email = 'a@example.com';"
                "INSERT INTO watchlists (user_id, name) SELECT id, 'AMD' FROM users"
                " WHERE email = 'a@example.com';"
                "INSERT INTO watchlists (user_id, name) SELECT id, 'Favourites' FROM users"
                " WHERE email = 'b@example.com';"
                "INSERT INTO watchlists (user_id, name) SELECT id, 'AMD' FROM users"
                " WHERE email = 'b@example.com';"
                "INSERT INTO watchlist_items (watchlist_id, company_id)"
                " SELECT w.id, c.id FROM watchlists w, companies c"
                " WHERE w.name = 'Chips' AND c.ticker = 'NVDA';"
            )
        )

    command.upgrade(alembic_cfg, "c3e8a1f5d7b2")
    query = text(
        "SELECT u.email, w.name, coalesce(string_agg(c.ticker, ',' ORDER BY i.id), '')"
        " FROM watchlists w JOIN users u ON u.id = w.user_id"
        " LEFT JOIN watchlist_items i ON i.watchlist_id = w.id"
        " LEFT JOIN companies c ON c.id = i.company_id"
        " GROUP BY u.email, w.id, w.name ORDER BY w.id"
    )
    with test_engine.connect() as conn:
        rows = [tuple(r) for r in conn.execute(query)]
    assert rows == [
        ("a@example.com", "Watchlist", "NVDA,AMD"),
        ("a@example.com", "Chips", "NVDA"),
        ("b@example.com", "Favourites", ""),
        ("b@example.com", "Watchlist", "AMD"),
    ]
    with pytest.raises(IntegrityError), test_engine.begin() as conn:  # now one row per company
        conn.execute(
            text(
                "INSERT INTO watchlist_items (watchlist_id, company_id)"
                " SELECT watchlist_id, company_id FROM watchlist_items LIMIT 1"
            )
        )

    command.downgrade(alembic_cfg, "69ec64a10d9e")  # reverses cleanly
    command.downgrade(alembic_cfg, "base")  # leave no rows behind for later tests
    command.upgrade(alembic_cfg, "head")

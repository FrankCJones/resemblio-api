"""Tests for migration 0017 (asset_versions backfill).

Seeds a fresh SQLite database, walks alembic to 0016 (post-FK, pre-backfill),
inserts representative extraction rows, runs 0017, and asserts that:

1. Each extractions row with a non-null ``dtcg_json`` now has a non-null
   ``asset_version_id``.
2. The asset_versions row carries the canonical content_hash for the payload.
3. Two extractions sharing the same ``(url, dtcg_json)`` collapse to one
   asset_versions row (dedup).
4. A re-run of 0017 is a no-op (idempotency).
5. Downgrade clears the FK and removes the backfilled rows.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, insert, select, text

from app.asset_versions import canonicalize_dtcg, content_hash_for


SAMPLE_DTCG_A: dict = {
    "schema_version": 1,
    "color": {"brand": {"$value": "#3366cc", "$type": "color"}},
}
SAMPLE_DTCG_B: dict = {
    "schema_version": 1,
    "color": {"brand": {"$value": "#ff3366", "$type": "color"}},
}


@pytest.fixture
def alembic_config(tmp_path: Path):
    """Yield an alembic Config bound to a throwaway sqlite file."""
    db_path = tmp_path / "backfill_test.sqlite"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parents[1] / "migrations"),
    )
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{db_path}")
    previous = os.environ.pop("RESEMBLIO_DB_URL", None)
    try:
        yield config, db_path
    finally:
        if previous is not None:
            os.environ["RESEMBLIO_DB_URL"] = previous


def _seed_user(engine, email: str = "seed@example.com") -> int:
    """Insert a minimal user row directly via SQL (no ORM at migration test time)."""
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "INSERT INTO users (email, password_hash, status, created_at, updated_at) "
                "VALUES (:email, 'x', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"email": email},
        )
        return int(result.lastrowid)


def _seed_extraction(engine, *, user_id: int, url: str, dtcg: dict, schema_version: int = 1) -> int:
    """Insert a minimal extractions row, matching the 0016-era schema shape."""
    import json

    with engine.begin() as conn:
        result = conn.execute(
            text(
                "INSERT INTO extractions "
                "(user_id, api_key_id, url, url_normalized, status, dtcg_json, "
                " schema_version, credit_cents, extracted_at, low_quality_review_pending) "
                "VALUES (:user_id, NULL, :url, :url, 'ok', :dtcg, :sv, 0, "
                "        CURRENT_TIMESTAMP, 0)"
            ),
            {
                "user_id": user_id,
                "url": url,
                "dtcg": json.dumps(dtcg),
                "sv": schema_version,
            },
        )
        return int(result.lastrowid)


def test_0017_backfill_links_extractions_to_asset_versions(alembic_config) -> None:
    """The backfill walks every extractions row and populates the FK."""
    config, db_path = alembic_config
    command.upgrade(config, "0016_extractions_asset_version_fk")
    engine = create_engine(f"sqlite+pysqlite:///{db_path}")

    user_id = _seed_user(engine)
    e1 = _seed_extraction(engine, user_id=user_id, url="https://a.example.com", dtcg=SAMPLE_DTCG_A)
    e2 = _seed_extraction(engine, user_id=user_id, url="https://b.example.com", dtcg=SAMPLE_DTCG_B)
    # Shared (url, dtcg) -- must dedup to the same asset_versions row as e1.
    e3 = _seed_extraction(engine, user_id=user_id, url="https://a.example.com", dtcg=SAMPLE_DTCG_A)

    engine.dispose()
    command.upgrade(config, "0017_backfill_asset_versions")

    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, asset_version_id FROM extractions ORDER BY id")
        ).all()
        av_for = {row[0]: row[1] for row in rows}
        assert all(v is not None for v in av_for.values())
        # e1 and e3 share an asset_version_id (dedup); e2 has its own.
        assert av_for[e1] == av_for[e3]
        assert av_for[e1] != av_for[e2]

        av_count = conn.execute(text("SELECT COUNT(*) FROM asset_versions")).scalar()
        assert av_count == 2

        # Hash on the joined row matches the canonical hash of the payload.
        av_row = conn.execute(
            text(
                "SELECT content_hash, first_extracted_by_user_id, is_public "
                "FROM asset_versions WHERE id = :id"
            ),
            {"id": av_for[e1]},
        ).one()
        assert av_row[0] == content_hash_for(SAMPLE_DTCG_A)
        assert av_row[1] == user_id
        # SQLite stores booleans as integers; treat 0/False equivalently.
        assert av_row[2] in (False, 0)
    engine.dispose()


def test_0017_backfill_is_idempotent(alembic_config) -> None:
    """Re-running the backfill (via downgrade-then-upgrade) produces the same row count."""
    config, db_path = alembic_config
    command.upgrade(config, "0016_extractions_asset_version_fk")
    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    user_id = _seed_user(engine)
    _seed_extraction(engine, user_id=user_id, url="https://idem.example.com", dtcg=SAMPLE_DTCG_A)
    engine.dispose()

    command.upgrade(config, "0017_backfill_asset_versions")
    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    with engine.connect() as conn:
        first_count = conn.execute(text("SELECT COUNT(*) FROM asset_versions")).scalar()
    engine.dispose()

    # Downgrade clears the backfill, upgrade re-applies it; the count must match.
    command.downgrade(config, "0016_extractions_asset_version_fk")
    command.upgrade(config, "0017_backfill_asset_versions")
    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    with engine.connect() as conn:
        second_count = conn.execute(text("SELECT COUNT(*) FROM asset_versions")).scalar()
        # All extractions rows are re-linked.
        unlinked = conn.execute(
            text(
                "SELECT COUNT(*) FROM extractions "
                "WHERE dtcg_json IS NOT NULL AND asset_version_id IS NULL"
            )
        ).scalar()
        assert unlinked == 0
    engine.dispose()
    assert first_count == second_count == 1


def test_0017_downgrade_clears_backfill(alembic_config) -> None:
    """Downgrade nulls every extractions.asset_version_id and drops backfilled rows."""
    config, db_path = alembic_config
    command.upgrade(config, "0016_extractions_asset_version_fk")
    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    user_id = _seed_user(engine)
    _seed_extraction(engine, user_id=user_id, url="https://down.example.com", dtcg=SAMPLE_DTCG_A)
    engine.dispose()

    command.upgrade(config, "0017_backfill_asset_versions")
    command.downgrade(config, "0016_extractions_asset_version_fk")

    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT asset_version_id FROM extractions")).all()
        assert all(row[0] is None for row in rows)
        count = conn.execute(text("SELECT COUNT(*) FROM asset_versions")).scalar()
        assert count == 0
    engine.dispose()

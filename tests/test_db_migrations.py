"""Tests for Alembic migration round trips."""
from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_alembic_upgrade_downgrade_round_trip() -> None:
    """Upgrade to head and downgrade to base against a fresh SQLite database."""
    db_path = Path("migration_test.sqlite")
    if db_path.exists():
        db_path.unlink()
    config = Config("alembic.ini")
    config.set_main_option("script_location", "migrations")
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{db_path}")
    previous_url = os.environ.pop("RESEMBLIO_DB_URL", None)
    try:
        command.upgrade(config, "head")
    finally:
        if previous_url is not None:
            os.environ["RESEMBLIO_DB_URL"] = previous_url
    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {"users", "api_keys", "api_key_events", "extractions", "credit_ledger", "stripe_events_seen", "topup_sessions", "asset_versions"}.issubset(tables)
    api_key_columns = {column["name"] for column in inspector.get_columns("api_keys")}
    assert "spend_cap_cents" in api_key_columns
    extraction_columns = {column["name"] for column in inspector.get_columns("extractions")}
    # 0016 adds the FK; 0018 (the drop-dtcg_json gate) is included in head, so
    # dtcg_json must NOT appear after a full upgrade to head.
    assert "asset_version_id" in extraction_columns
    assert "dtcg_json" not in extraction_columns
    asset_version_columns = {column["name"] for column in inspector.get_columns("asset_versions")}
    assert {"url", "content_hash", "dtcg_json", "is_public", "first_extracted_by_user_id"}.issubset(asset_version_columns)
    engine.dispose()
    previous_url = os.environ.pop("RESEMBLIO_DB_URL", None)
    try:
        command.downgrade(config, "base")
    finally:
        if previous_url is not None:
            os.environ["RESEMBLIO_DB_URL"] = previous_url
    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    tables_after = set(inspect(engine).get_table_names())
    assert not {"users", "api_keys", "api_key_events", "extractions", "credit_ledger", "stripe_events_seen", "topup_sessions"} & tables_after
    engine.dispose()
    db_path.unlink(missing_ok=True)

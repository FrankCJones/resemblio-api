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
    tables = set(inspect(engine).get_table_names())
    assert {"users", "api_keys", "api_key_events", "extractions", "credit_ledger"}.issubset(tables)
    engine.dispose()
    previous_url = os.environ.pop("RESEMBLIO_DB_URL", None)
    try:
        command.downgrade(config, "base")
    finally:
        if previous_url is not None:
            os.environ["RESEMBLIO_DB_URL"] = previous_url
    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    tables_after = set(inspect(engine).get_table_names())
    assert not {"users", "api_keys", "api_key_events", "extractions", "credit_ledger"} & tables_after
    engine.dispose()
    db_path.unlink(missing_ok=True)

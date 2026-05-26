"""Unit tests for scripts/migrate_sqlite_to_postgres.py.

These tests exercise the pure-data helpers (URL classification, redaction,
count verification, target-empty guard) and an end-to-end SQLite-to-SQLite
roundtrip that stands in for the real Postgres target. Using SQLite for
both endpoints is a deliberate test-only relaxation: the script's
URL-classification gate would normally refuse this, so the roundtrip test
bypasses ``run_migration`` and drives ``insert_rows`` / ``collect_source_rows``
directly. This keeps the unit-test suite hermetic (no network, no Postgres
install) while still covering the row-flow code paths.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import ApiKey, CreditLedger, User
from app.crypto import hash_password


def _load_migrator():
    """Import the migrator module by path because scripts/ is not a package."""
    here = Path(__file__).resolve().parents[1]
    path = here / "scripts" / "migrate_sqlite_to_postgres.py"
    spec = importlib.util.spec_from_file_location("migrate_sqlite_to_postgres", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["migrate_sqlite_to_postgres"] = module
    spec.loader.exec_module(module)
    return module


migrator = _load_migrator()


def test_redact_url_strips_password() -> None:
    """The password segment is replaced; user, host, db survive intact."""
    url = "postgresql+psycopg2://resemblio:hunter2@127.0.0.1:5432/resemblio"
    redacted = migrator._redact_url(url)
    assert "hunter2" not in redacted
    assert "resemblio" in redacted
    assert "127.0.0.1:5432" in redacted


def test_redact_url_passthrough_when_no_creds() -> None:
    """URLs without credentials are returned unchanged."""
    url = "sqlite:///./resemblio_dev.db"
    assert migrator._redact_url(url) == url


def test_classify_url_known_prefixes() -> None:
    """SQLite and Postgres prefixes classify correctly; others are unknown."""
    assert migrator._classify_url("sqlite:///x.db") == "sqlite"
    assert migrator._classify_url("sqlite+pysqlite:///:memory:") == "sqlite"
    assert migrator._classify_url("postgresql://u:p@h/d") == "postgres"
    assert migrator._classify_url("postgresql+psycopg2://u:p@h/d") == "postgres"
    assert migrator._classify_url("mysql://u:p@h/d") == "unknown"


def test_validate_urls_rejects_swapped_args() -> None:
    """Source must be SQLite, target must be Postgres; swap raises."""
    with pytest.raises(ValueError, match="SQLITE_SOURCE_URL"):
        migrator.validate_urls("postgresql://u:p@h/d", "sqlite:///x.db")
    with pytest.raises(ValueError, match="POSTGRES_TARGET_URL"):
        migrator.validate_urls("sqlite:///x.db", "sqlite:///y.db")


def test_verify_counts_passes_on_match() -> None:
    """No exception when source + before == after for every table."""
    tcs = [
        migrator.TableCount(table="users", source_rows=3, target_rows_before=0, target_rows_after=3),
        migrator.TableCount(table="api_keys", source_rows=5, target_rows_before=0, target_rows_after=5),
    ]
    migrator.verify_counts(tcs)


def test_verify_counts_raises_on_mismatch() -> None:
    """A single off-by-one trips the gate; the error names the offending table."""
    tcs = [
        migrator.TableCount(table="users", source_rows=3, target_rows_before=0, target_rows_after=3),
        migrator.TableCount(table="api_keys", source_rows=5, target_rows_before=0, target_rows_after=4),
    ]
    with pytest.raises(RuntimeError, match="api_keys"):
        migrator.verify_counts(tcs)


def test_verify_counts_ignores_dry_run_rows() -> None:
    """Dry-run rows have target_rows_after=None and must not trigger the check."""
    tcs = [
        migrator.TableCount(table="users", source_rows=99, target_rows_before=0, target_rows_after=None),
    ]
    migrator.verify_counts(tcs)


def test_ensure_target_empty_raises_without_force(tmp_path: Path) -> None:
    """A populated target without --force is a hard stop."""
    url = f"sqlite:///{tmp_path / 'tgt.db'}"
    engine = create_engine(url, future=True)
    Base.metadata.create_all(engine)
    SessionMaker = sessionmaker(bind=engine, future=True)
    with SessionMaker() as s:
        s.add(User(email="seed@example.com", password_hash=hash_password("x"), status="active"))
        s.commit()
    with SessionMaker() as s:
        with pytest.raises(RuntimeError, match="not empty"):
            migrator.ensure_target_empty_or_force(s, force=False)
    engine.dispose()


def test_ensure_target_empty_allows_force(tmp_path: Path) -> None:
    """The same populated target is permitted when --force is passed."""
    url = f"sqlite:///{tmp_path / 'tgt.db'}"
    engine = create_engine(url, future=True)
    Base.metadata.create_all(engine)
    SessionMaker = sessionmaker(bind=engine, future=True)
    with SessionMaker() as s:
        s.add(User(email="seed@example.com", password_hash=hash_password("x"), status="active"))
        s.commit()
    with SessionMaker() as s:
        before = migrator.ensure_target_empty_or_force(s, force=True)
    assert before["users"] == 1
    engine.dispose()


def test_collect_and_insert_roundtrip_preserves_rows(tmp_path: Path) -> None:
    """End-to-end on two SQLite files: rows + ids survive the copy.

    Stands in for the real SQLite-to-Postgres path. The data-flow helpers
    are engine-agnostic; the only Postgres-specific code path
    (`reset_postgres_sequences`) is not exercised here and is covered by
    integration testing during the cutover dry-run.
    """
    src_url = f"sqlite:///{tmp_path / 'src.db'}"
    tgt_url = f"sqlite:///{tmp_path / 'tgt.db'}"
    src_engine = create_engine(src_url, future=True)
    tgt_engine = create_engine(tgt_url, future=True)
    Base.metadata.create_all(src_engine)
    Base.metadata.create_all(tgt_engine)
    SrcSession = sessionmaker(bind=src_engine, future=True)
    TgtSession = sessionmaker(bind=tgt_engine, future=True)

    with SrcSession() as s:
        user = User(email="frank@optsus.com", password_hash=hash_password("pw"), status="active")
        s.add(user)
        s.flush()
        s.add(
            CreditLedger(
                user_id=user.id,
                entry_type="onboarding_grant",
                amount_cents=500,
                balance_after_cents=500,
                note="seed",
            )
        )
        s.commit()
        seeded_user_id = user.id

    with SrcSession() as src, TgtSession() as tgt:
        for model in (User, CreditLedger):
            rows = migrator.collect_source_rows(src, model)
            migrator.insert_rows(tgt, model, rows)
        tgt.commit()

    with TgtSession() as tgt:
        migrated = tgt.scalars(select(User)).all()
        ledger = tgt.scalars(select(CreditLedger)).all()
        assert len(migrated) == 1
        assert migrated[0].id == seeded_user_id
        assert migrated[0].email == "frank@optsus.com"
        assert len(ledger) == 1
        assert ledger[0].user_id == seeded_user_id
        assert ledger[0].balance_after_cents == 500

    src_engine.dispose()
    tgt_engine.dispose()


def test_parse_args_defaults() -> None:
    """dry_run defaults False; force defaults False; log level defaults INFO."""
    ns = migrator.parse_args([])
    assert ns.dry_run is False
    assert ns.force is False
    assert ns.log_level == "INFO"


def test_parse_args_flags() -> None:
    """--dry-run and --force are picked up."""
    ns = migrator.parse_args(["--dry-run", "--force"])
    assert ns.dry_run is True
    assert ns.force is True

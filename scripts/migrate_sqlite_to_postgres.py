"""One-shot migrator: copy Resemblio API data from SQLite into Postgres.

Why this exists
---------------
Production drifted to SQLite (`/opt/resemblio-api/app/resemblio_dev.db`) while
`Resemblio_INFRA.md` specifies Postgres 16 as the production target. This
script reads every row from the SQLite source and bulk-inserts it into the
Postgres target using the live SQLAlchemy ORM models, so the schema mapping
stays in lockstep with the application's own type definitions (e.g. the
`JsonType` / `InetType` variants in `app/models.py`).

Invariants this script enforces
-------------------------------
- Target tables must be empty unless ``--force`` is passed. The cutover is
  one-shot; refusing to write into a non-empty target prevents accidental
  double migration that would re-issue primary keys and corrupt the
  append-only `credit_ledger` ledger.
- Identity columns are preserved verbatim. Foreign keys (User.id ->
  ApiKey.user_id, etc.) are not re-mapped; SQLite ids land unchanged in
  Postgres, so existing application references stay valid. After all tables
  are inserted, Postgres sequences are advanced to ``max(id) + 1`` so the
  next ORM insert does not collide with a migrated row.
- Insert order follows FK topology: parents first (users, extractions),
  children last (api_key_events, credit_ledger). Violating the order would
  surface as IntegrityError on FK constraints.
- Per-table row counts are verified after insert. Any mismatch raises and
  the script exits non-zero so the operator runbook can trigger rollback
  before flipping `.env`.

Run modes
---------
- ``--dry-run`` (default off, but always recommended first): connects to
  both databases, counts source rows, reports the planned action, never
  writes. Safe to repeat.
- Real run: writes within a single transaction per table; on any failure
  the partial transaction rolls back and the script exits non-zero.
- ``--force``: allows writing into a non-empty target. Only intended for
  re-runs after a verified rollback that left stale rows. Operator must
  confirm in the runbook before passing this flag.

Dependencies: SQLAlchemy (already pinned in pyproject.toml), psycopg2-binary
or psycopg[binary] for the Postgres driver. The Postgres URL must use a
driver SQLAlchemy understands (e.g. ``postgresql+psycopg2://...``).

Schema version of the JSON report this script writes to stdout: 1.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

# Make `app.*` importable when the script is invoked from `code/api/` or
# from the installed location under `/opt/resemblio-api/app/`.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import Base  # noqa: E402
from app.models import (  # noqa: E402
    ApiKey,
    ApiKeyEvent,
    CreditLedger,
    Extraction,
    StripeEventSeen,
    TopupSession,
    User,
)

REPORT_SCHEMA_VERSION = 1

# Topological order: parents before children. The migrator iterates this list
# in order for both the count-and-compare pass and the insert pass. Add new
# tables to this tuple when models grow; never reorder existing entries
# without auditing FK dependencies.
MODEL_ORDER: tuple[type, ...] = (
    User,
    ApiKey,
    Extraction,
    ApiKeyEvent,
    CreditLedger,
    TopupSession,
    StripeEventSeen,
)

# Batch size for bulk inserts. Large enough to amortize round-trip overhead,
# small enough that a single failed batch's rollback is quick. Tuned for the
# expected v1 corpus (a single-digit number of users at cutover); not perf
# critical.
INSERT_BATCH_SIZE = 500

# SQLAlchemy URL prefixes considered SQLite. Used to gate cross-engine type
# coercion and to refuse obvious misuse (e.g. SQLite URL passed as target).
SQLITE_PREFIXES = ("sqlite://", "sqlite+pysqlite://")
POSTGRES_PREFIXES = ("postgresql://", "postgresql+psycopg2://", "postgresql+psycopg://")

logger = logging.getLogger("resemblio.migrate_sqlite_to_postgres")


@dataclass(frozen=True)
class TableCount:
    """Source / target row counts for a single table."""

    table: str
    source_rows: int
    target_rows_before: int
    target_rows_after: int | None  # None on dry run


@dataclass(frozen=True)
class MigrationReport:
    """Top-level run report emitted to stdout as JSON."""

    schema_version: int
    started_at: str
    finished_at: str
    dry_run: bool
    force: bool
    source_url_redacted: str
    target_url_redacted: str
    tables: list[TableCount]
    ok: bool
    error: str | None


def _redact_url(url: str) -> str:
    """Strip password from a SQLAlchemy URL for safe logging.

    SQLAlchemy URLs follow ``scheme://user:password@host/db`` and this
    redaction matches that single shape only; non-conforming URLs are
    returned unchanged because logging them as-is is no worse than the
    input the caller passed.
    """
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" not in rest:
        return url
    creds, host_part = rest.rsplit("@", 1)
    if ":" in creds:
        user = creds.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host_part}"
    return url


def _classify_url(url: str) -> str:
    """Return 'sqlite', 'postgres', or 'unknown' for the given URL prefix.

    Used by argument validation to fail loudly when the operator swaps the
    source and target flags or passes a typo'd driver.
    """
    if any(url.startswith(p) for p in SQLITE_PREFIXES):
        return "sqlite"
    if any(url.startswith(p) for p in POSTGRES_PREFIXES):
        return "postgres"
    return "unknown"


def validate_urls(source_url: str, target_url: str) -> None:
    """Reject obvious misuse before opening connections.

    The cutover risk is asymmetric: a SQLite target would silently work
    (writing a new file) while leaving the actual Postgres untouched, so the
    error message must be loud and the script must refuse to start.
    """
    src_kind = _classify_url(source_url)
    tgt_kind = _classify_url(target_url)
    if src_kind != "sqlite":
        raise ValueError(
            f"SQLITE_SOURCE_URL must be a SQLite URL (got {src_kind!r}); refusing to start"
        )
    if tgt_kind != "postgres":
        raise ValueError(
            f"POSTGRES_TARGET_URL must be a Postgres URL (got {tgt_kind!r}); refusing to start"
        )


def count_rows(session: Session, model: type) -> int:
    """Return the row count for one ORM model. Used in both passes."""
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def collect_source_rows(session: Session, model: type) -> list[dict]:
    """Read every row of `model` from the source as plain dicts.

    Returning dicts instead of ORM instances avoids dragging the source
    session's identity map into the target session and sidesteps detached-
    instance quirks during bulk insert. Columns are read via the mapper
    metadata so JSON / Inet variants flow through SQLAlchemy's own type
    coercion rather than ad-hoc string handling.
    """
    rows: list[dict] = []
    for instance in session.scalars(select(model)).all():
        rows.append({col.key: getattr(instance, col.key) for col in inspect(model).mapper.column_attrs})
    return rows


def insert_rows(session: Session, model: type, rows: Sequence[dict]) -> None:
    """Bulk-insert pre-collected rows into the target session in batches.

    Uses ``Session.bulk_insert_mappings`` so server-side defaults
    (`func.now()` timestamps, default scopes) are NOT re-applied; the
    migrated rows must preserve their original `created_at` and other
    server defaults exactly. Caller commits after all tables succeed for
    a given transaction unit.
    """
    for start in range(0, len(rows), INSERT_BATCH_SIZE):
        batch = rows[start : start + INSERT_BATCH_SIZE]
        session.bulk_insert_mappings(inspect(model), batch)


def reset_postgres_sequences(target_engine: Engine) -> None:
    """Advance each id sequence to max(id) so future inserts do not collide.

    SQLAlchemy's identity columns map to Postgres sequences named
    ``<table>_<column>_seq``. After bulk inserts with explicit ids the
    sequence stays at its prior value (often 1), so the next ORM insert
    would try to reuse id=1 and raise a UniqueViolation. This helper
    re-aligns the sequence for every model whose primary key is an
    auto-increment integer.
    """
    with target_engine.begin() as conn:
        for model in MODEL_ORDER:
            pk_cols = inspect(model).primary_key
            if len(pk_cols) != 1:
                continue
            pk = pk_cols[0]
            if pk.autoincrement is not True and pk.autoincrement != "auto":
                continue
            try:
                if pk.type.python_type is not int:
                    continue
            except (NotImplementedError, AttributeError):
                # Composite or non-numeric primary keys (e.g. TopupSession's
                # string id) have no Postgres sequence; skip silently.
                continue
            seq = f"{model.__tablename__}_{pk.name}_seq"
            # `setval(seq, GREATEST(max_id, 1), max_id IS NOT NULL)` keeps the
            # sequence valid even when the migrated table is empty.
            conn.execute(
                text(
                    f"SELECT setval('{seq}', GREATEST(COALESCE((SELECT MAX({pk.name}) FROM {model.__tablename__}), 1), 1), "
                    f"(SELECT MAX({pk.name}) FROM {model.__tablename__}) IS NOT NULL)"
                )
            )


def ensure_target_empty_or_force(target_session: Session, force: bool) -> dict[str, int]:
    """Return per-table target counts. Raise if any are non-zero and not --force."""
    before: dict[str, int] = {}
    non_empty: list[str] = []
    for model in MODEL_ORDER:
        n = count_rows(target_session, model)
        before[model.__tablename__] = n
        if n > 0:
            non_empty.append(f"{model.__tablename__}={n}")
    if non_empty and not force:
        raise RuntimeError(
            "Target tables are not empty: "
            + ", ".join(non_empty)
            + ". Pass --force only after a verified rollback. Refusing to run."
        )
    return before


def run_migration(
    source_url: str,
    target_url: str,
    dry_run: bool,
    force: bool,
) -> MigrationReport:
    """Execute the migration end-to-end. Returns a serializable report.

    Errors raised below are caught at the CLI boundary so the report can be
    written before the process exits non-zero.
    """
    validate_urls(source_url, target_url)
    started = datetime.now(timezone.utc).isoformat()
    logger.info("opening source %s", _redact_url(source_url))
    logger.info("opening target %s", _redact_url(target_url))

    source_engine = create_engine(source_url, future=True)
    target_engine = create_engine(target_url, future=True, pool_pre_ping=True)
    SrcSession = sessionmaker(bind=source_engine, autocommit=False, autoflush=False, future=True)
    TgtSession = sessionmaker(bind=target_engine, autocommit=False, autoflush=False, future=True)

    tables: list[TableCount] = []
    error: str | None = None
    ok = False

    try:
        with TgtSession() as tgt:
            before = ensure_target_empty_or_force(tgt, force)

        with SrcSession() as src:
            source_counts = {m.__tablename__: count_rows(src, m) for m in MODEL_ORDER}
            logger.info("source row counts: %s", source_counts)

            if dry_run:
                for model in MODEL_ORDER:
                    tables.append(
                        TableCount(
                            table=model.__tablename__,
                            source_rows=source_counts[model.__tablename__],
                            target_rows_before=before[model.__tablename__],
                            target_rows_after=None,
                        )
                    )
                ok = True
            else:
                # One transaction per table keeps failure blast radius small
                # and makes the error message point at the offending model.
                for model in MODEL_ORDER:
                    rows = collect_source_rows(src, model)
                    logger.info("migrating %s rows=%d", model.__tablename__, len(rows))
                    with TgtSession() as tgt:
                        try:
                            insert_rows(tgt, model, rows)
                            tgt.commit()
                        except SQLAlchemyError:
                            tgt.rollback()
                            raise

                reset_postgres_sequences(target_engine)

                with TgtSession() as tgt:
                    for model in MODEL_ORDER:
                        after = count_rows(tgt, model)
                        tables.append(
                            TableCount(
                                table=model.__tablename__,
                                source_rows=source_counts[model.__tablename__],
                                target_rows_before=before[model.__tablename__],
                                target_rows_after=after,
                            )
                        )
                verify_counts(tables)
                ok = True
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        logger.error("migration failed: %s", error)
    finally:
        source_engine.dispose()
        target_engine.dispose()

    finished = datetime.now(timezone.utc).isoformat()
    return MigrationReport(
        schema_version=REPORT_SCHEMA_VERSION,
        started_at=started,
        finished_at=finished,
        dry_run=dry_run,
        force=force,
        source_url_redacted=_redact_url(source_url),
        target_url_redacted=_redact_url(target_url),
        tables=tables,
        ok=ok,
        error=error,
    )


def verify_counts(tables: Sequence[TableCount]) -> None:
    """Raise if any table's post-insert target count does not equal source.

    The runbook treats this as the gate for proceeding with the .env flip.
    A mismatch here is a hard stop.
    """
    mismatches: list[str] = []
    for tc in tables:
        if tc.target_rows_after is None:
            continue
        expected = tc.source_rows + tc.target_rows_before
        if tc.target_rows_after != expected:
            mismatches.append(
                f"{tc.table}: source={tc.source_rows} + before={tc.target_rows_before} != after={tc.target_rows_after}"
            )
    if mismatches:
        raise RuntimeError("row count verification failed: " + "; ".join(mismatches))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI args. URLs come from env so they never appear in shell history."""
    parser = argparse.ArgumentParser(description="Migrate Resemblio API data from SQLite to Postgres")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", help="report only; do not write to target")
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow writing into a non-empty target (post-rollback re-run only)",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("MIGRATE_LOG_LEVEL", "INFO"),
        help="logging level (default INFO)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Reads URLs from env; emits JSON report to stdout."""
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    source_url = os.environ.get("SQLITE_SOURCE_URL")
    target_url = os.environ.get("POSTGRES_TARGET_URL")
    if not source_url or not target_url:
        logger.error("SQLITE_SOURCE_URL and POSTGRES_TARGET_URL must be set")
        return 2

    report = run_migration(source_url, target_url, dry_run=args.dry_run, force=args.force)
    sys.stdout.write(json.dumps({**asdict(report), "tables": [asdict(t) for t in report.tables]}, indent=2))
    sys.stdout.write("\n")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Add library_index_jobs queue table for the library indexer service.

Revision ID: 0019_library_index_jobs
Revises: 0018_drop_extractions_dtcg_json
Create Date: 2026-06-02

schema_version: library_index_jobs_v1 (one new table; no existing rows touched).

Motivation
----------
The Resemblio Library v1.1 mission (``projects/OptSus Team/missions/
resemblio-library-v1.1.md``) Phase 4 specifies a queue-and-worker shape: the
indexer drains pending jobs, runs the DRL compose pipeline against the
referenced ``asset_versions`` row, and writes per-page renders into
``library_pages``. Decoupling the trigger (seed insert / POST extraction)
from the work (compose + persist) keeps the request path fast and makes the
indexer trivially restartable: a crashed worker leaves rows in ``running``
state with a stale ``started_at`` that the next tick can reclaim.

Table shape
-----------
- ``status`` advances ``pending`` -> ``running`` -> ``complete | failed``.
  Failed rows carry the most recent ``last_error`` for triage.
- ``attempts`` is incremented on every compose attempt; the worker caps
  retries via the application constant (``LIBRARY_INDEX_MAX_ATTEMPTS``) so
  a poison row cannot livelock the queue.
- ``(status, enqueued_at)`` is the canonical drain order: oldest pending
  first. The composite index covers the worker's ``WHERE status='pending'
  ORDER BY enqueued_at LIMIT N`` query without a full scan.
- ``asset_version_id`` is FK'd back to the snapshot the job will compose;
  the indexed lookup powers the enqueue paths' "is there already a job for
  this asset_version?" check (cheap idempotency on the trigger side).

Idempotency of the migration itself
-----------------------------------
Probes ``information_schema`` and short-circuits if the table already
exists; downgrade drops the table iff present.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0019_library_index_jobs"
down_revision = "0018_drop_extractions_dtcg_json"
branch_labels = None
depends_on = None


_TABLE_NAME = "library_index_jobs"
_IX_STATUS_ENQUEUED_AT = "ix_library_index_jobs_status_enqueued_at"
_IX_ASSET_VERSION_ID = "ix_library_index_jobs_asset_version_id"


def _table_exists(bind: sa.engine.Connection, table: str) -> bool:
    """Return True if ``table`` already exists in the bound database."""
    inspector = sa.inspect(bind)
    return table in inspector.get_table_names()


def upgrade() -> None:
    """Create the library_index_jobs queue table plus its two indexes."""
    bind = op.get_bind()
    if _table_exists(bind, _TABLE_NAME):
        return
    op.create_table(
        _TABLE_NAME,
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "asset_version_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey("asset_versions.id"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "enqueued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        _IX_STATUS_ENQUEUED_AT,
        _TABLE_NAME,
        ["status", "enqueued_at"],
    )
    op.create_index(_IX_ASSET_VERSION_ID, _TABLE_NAME, ["asset_version_id"])


def downgrade() -> None:
    """Drop the library_index_jobs table; symmetric with upgrade."""
    bind = op.get_bind()
    if not _table_exists(bind, _TABLE_NAME):
        return
    op.drop_index(_IX_ASSET_VERSION_ID, table_name=_TABLE_NAME)
    op.drop_index(_IX_STATUS_ENQUEUED_AT, table_name=_TABLE_NAME)
    op.drop_table(_TABLE_NAME)

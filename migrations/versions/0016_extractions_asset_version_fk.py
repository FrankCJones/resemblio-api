"""Add extractions.asset_version_id FK pointing at the new library table.

Revision ID: 0016_extractions_asset_version_fk
Revises: 0015_asset_versions
Create Date: 2026-06-02

schema_version: 1 (no change to the response contract; the FK is internal).

Motivation
----------
The library refactor (see 0015) needs ``extractions`` rows to point at the
deduplicated ``asset_versions`` row that backs their DTCG payload. The FK is
nullable for the duration of the migration sequence so:

1. 0016 adds the column NULL.
2. 0017 backfills it from the existing ``extractions.dtcg_json`` payloads.
3. The application's extraction-creation path (POST /v1/extractions) writes
   both ``dtcg_json`` on extractions AND the new asset_versions row in the
   same transaction; ``asset_version_id`` is populated on every fresh row.
4. 0018 (separately deployed; Jim-gated) drops the duplicate columns from
   extractions.

The FK stays nullable on disk even after 0018 ships because seed rows
written before this migration ran already have NULL there; we never
back-fill historical rows we cannot dedup.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0016_extractions_asset_version_fk"
down_revision = "0015_asset_versions"
branch_labels = None
depends_on = None


_INDEX_NAME = "ix_extractions_asset_version_id"


def _column_exists(bind: sa.engine.Connection, table: str, column: str) -> bool:
    """Return True if ``table.column`` already exists in the bound database."""
    inspector = sa.inspect(bind)
    return any(c["name"] == column for c in inspector.get_columns(table))


def upgrade() -> None:
    """Add asset_version_id FK column + supporting lookup index."""
    bind = op.get_bind()
    if _column_exists(bind, "extractions", "asset_version_id"):
        return
    # Name the FK constraint explicitly: batch_alter_table on SQLite rebuilds
    # the table and refuses to copy unnamed constraints (raises "Constraint
    # must have a name"). Named constraints round-trip cleanly.
    with op.batch_alter_table("extractions") as batch:
        batch.add_column(
            sa.Column(
                "asset_version_id",
                sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                sa.ForeignKey(
                    "asset_versions.id",
                    name="fk_extractions_asset_version_id",
                ),
                nullable=True,
            )
        )
    op.create_index(_INDEX_NAME, "extractions", ["asset_version_id"])


def downgrade() -> None:
    """Drop the FK column and its index; symmetric with upgrade."""
    bind = op.get_bind()
    if not _column_exists(bind, "extractions", "asset_version_id"):
        return
    op.drop_index(_INDEX_NAME, table_name="extractions")
    with op.batch_alter_table("extractions") as batch:
        batch.drop_column("asset_version_id")

"""Add asset_versions library table for deduplicated DTCG snapshots.

Revision ID: 0015_asset_versions
Revises: 0014_idempotency_keys
Create Date: 2026-06-02

schema_version: asset_versions_v1 (one new table; no existing rows touched).

Motivation
----------
The brain-dump's library architecture (see ``projects/Resemblio/01-brain-dump.md``)
treats the deduplicated DTCG snapshot for a URL as a first-class entity that
many extraction rows can share. Today the DTCG JSON is denormalized on every
``extractions`` row: re-extracting the same URL writes the same payload again,
the public-corpus visibility flag has nowhere to live, and the library-hit
fast path (v1.2) has no table to look up against.

This migration adds the table only. Backfill of existing rows lands in 0017;
extractions.asset_version_id is added in 0016; the duplicate columns on
extractions are dropped in 0018 (gated as a second deploy by Jim).

Dedup contract
--------------
``(url, content_hash)`` is the logical dedup key. ``content_hash`` is the
SHA-256 of the canonical-JSON serialization of the DTCG payload (sort_keys=True,
separators=(",", ":")). Two extractions of the same URL that return the same
DTCG payload collapse to one ``asset_versions`` row.

Indexes
-------
- ``ix_asset_versions_url_fetched_at`` powers future library-hit lookups
  (newest snapshot for one URL).
- ``ix_asset_versions_content_hash`` powers dedup detection on insert.
- ``ix_asset_versions_is_public_fetched_at`` (partial, postgres-only) powers
  the v1.2 public-corpus browse query without scanning private rows.

Idempotency of the migration itself
-----------------------------------
Probes ``information_schema`` and short-circuits if the table already exists;
downgrade drops the table iff present.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0015_asset_versions"
down_revision = "0014_idempotency_keys"
branch_labels = None
depends_on = None


_TABLE_NAME = "asset_versions"
_IX_URL_FETCHED_AT = "ix_asset_versions_url_fetched_at"
_IX_CONTENT_HASH = "ix_asset_versions_content_hash"
_IX_PUBLIC_FETCHED_AT = "ix_asset_versions_is_public_fetched_at"


def _table_exists(bind: sa.engine.Connection, table: str) -> bool:
    """Return True if ``table`` already exists in the bound database."""
    inspector = sa.inspect(bind)
    return table in inspector.get_table_names()


def upgrade() -> None:
    """Create the asset_versions table plus its three indexes."""
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
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        # JSONB on postgres; JSON on sqlite (test path). Matches the variant
        # convention used on ``extractions.dtcg_json`` in 0001.
        sa.Column(
            "dtcg_json",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()).with_variant(
                sa.JSON(), "sqlite"
            ),
            nullable=False,
        ),
        sa.Column("raw_assets_url", sa.Text(), nullable=True),
        sa.Column(
            "manifest_schema_version",
            sa.Integer(),
            nullable=False,
            server_default="2",
        ),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "first_extracted_by_user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column(
            "is_public",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("version_label", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        _IX_URL_FETCHED_AT,
        _TABLE_NAME,
        ["url", sa.text("fetched_at DESC")],
    )
    op.create_index(_IX_CONTENT_HASH, _TABLE_NAME, ["content_hash"])
    # Partial index for public-corpus browse, postgres-only. On SQLite (test
    # path) a plain composite index is sufficient; partial indexes with this
    # syntax are postgres-specific.
    if bind.dialect.name == "postgresql":
        op.create_index(
            _IX_PUBLIC_FETCHED_AT,
            _TABLE_NAME,
            ["is_public", sa.text("fetched_at DESC")],
            postgresql_where=sa.text("is_public"),
        )
    else:
        op.create_index(
            _IX_PUBLIC_FETCHED_AT,
            _TABLE_NAME,
            ["is_public", "fetched_at"],
        )


def downgrade() -> None:
    """Drop the asset_versions table and its indexes; symmetric with upgrade."""
    bind = op.get_bind()
    if not _table_exists(bind, _TABLE_NAME):
        return
    op.drop_index(_IX_PUBLIC_FETCHED_AT, table_name=_TABLE_NAME)
    op.drop_index(_IX_CONTENT_HASH, table_name=_TABLE_NAME)
    op.drop_index(_IX_URL_FETCHED_AT, table_name=_TABLE_NAME)
    op.drop_table(_TABLE_NAME)

"""Create asset_components table for DRL component code storage.

Revision ID: 0023_asset_components
Revises: 0022_magic_link_plaintext_token
Create Date: 2026-06-14

schema_version: asset_component_v1

Motivation
----------
Issue #1 (Library v6 Epic A): the seed pipeline (seed_from_drl.py) reads
only the :root token block from each DRL asset and discards the rich
component markup (hover/focus/disabled states, motion, layout). The library
then composes generic Resemblio templates tinted with those tokens, so every
brand in the library is the same kit recolored.

This migration creates asset_components - a 1:N child of asset_versions -
to hold brand-stripped markup and component CSS per asset fragment. Issue #2
wires the seed to populate these rows; issue #3 updates the indexer to read
them when composing library_pages.

Table design notes
------------------
- 1:N with asset_versions keyed on (asset_version_id, fragment_key).
  fragment_key defaults to 'default'; future variants ('inverse', 'dark')
  add rows without schema changes.
- Large text blobs live here rather than on the hot asset_versions row to
  keep extraction queries cheap.
- states_present is a JSON array (e.g. ["rest","hover","focus","disabled"])
  that lets the indexer annotate which interaction states are present.
- schema_version is a string constant ('asset_component_v1') written by the
  application layer; increment it in the model if the column contract changes
  in a way that requires distinguishing old rows from new.

Indexes
-------
- Unique on (asset_version_id, fragment_key): one row per fragment per version.
- Non-unique on asset_version_id: fast lookup of all fragments for a given version.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0023_asset_components"
down_revision = "0022_magic_link_plaintext_token"
branch_labels = None
depends_on = None

_TABLE_NAME = "asset_components"
_INDEX_ASSET_VERSION = "ix_asset_components_asset_version_id"
_CONSTRAINT_UNIQUE = "uq_asset_components_version_fragment"


def upgrade() -> None:
    """Create the asset_components table with indexes."""
    op.create_table(
        _TABLE_NAME,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column(
            "asset_version_id",
            sa.BigInteger(),
            sa.ForeignKey("asset_versions.id"),
            nullable=False,
        ),
        sa.Column("fragment_key", sa.Text(), nullable=False),
        sa.Column("component_html", sa.Text(), nullable=False),
        sa.Column("component_css", sa.Text(), nullable=False),
        sa.Column("source_asset_path", sa.Text(), nullable=False),
        # JSON array of state names; Postgres uses JSONB in prod but the
        # SQLAlchemy dialect-variant is applied at ORM level, not migration
        # level. Plain JSON here is correct for both Postgres and SQLite tests.
        sa.Column("states_present", sa.JSON(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "asset_version_id",
            "fragment_key",
            name=_CONSTRAINT_UNIQUE,
        ),
    )
    op.create_index(
        _INDEX_ASSET_VERSION,
        _TABLE_NAME,
        ["asset_version_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the asset_components table and its indexes.

    Symmetric with upgrade: index first, then table (Alembic drops the
    UniqueConstraint implicitly when the table is dropped).
    """
    op.drop_index(_INDEX_ASSET_VERSION, table_name=_TABLE_NAME)
    op.drop_table(_TABLE_NAME)

"""Add library_pages table for indexer-generated per-page renders.

Revision ID: 0020_library_pages
Revises: 0019_library_index_jobs
Create Date: 2026-06-02

schema_version: library_pages_v1 (one new table; no existing rows touched).

Motivation
----------
Phase 4 of the Resemblio Library v1.1 mission persists the compose pipeline's
output as one row per ``(asset_version, category)`` so the Next.js library
routes (Phase 5) can read them directly with ISR caching. Each row carries
the composed HTML, a metadata JSON envelope (token subset + sample text +
display font, drives OG image + page copy), and a denormalized
``brand_slug`` for the URL hierarchy.

``is_canonical`` is the flag that powers ``/library/<brand>/<category>/``
("latest version"); the indexer sets it TRUE for the newest asset_version
per ``(brand_slug, category_slug)`` and FALSE for older versions of the same
brand. Versioned URLs (``/library/<brand>/<category>/<version>/``) read every
row regardless of the flag.

Constraints
-----------
- ``UNIQUE(asset_version_id, category_slug)`` makes the indexer idempotent:
  re-running compose for the same asset_version cannot duplicate rows. The
  worker's INSERT path catches the integrity error and skips when the row
  already exists for that ``(asset_version, category)`` pair.
- The partial index on ``is_canonical`` keeps the canonical-page query
  cheap; only rows with the flag TRUE are scanned for the latest-version
  brand pages.

Idempotency of the migration itself
-----------------------------------
Probes ``information_schema`` and short-circuits if the table already
exists; downgrade drops the table iff present.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0020_library_pages"
down_revision = "0019_library_index_jobs"
branch_labels = None
depends_on = None


_TABLE_NAME = "library_pages"
_IX_BRAND_CATEGORY = "ix_library_pages_brand_category"
_IX_IS_CANONICAL = "ix_library_pages_is_canonical"
_UQ_ASSET_VERSION_CATEGORY = "uq_library_pages_asset_version_category"


def _table_exists(bind: sa.engine.Connection, table: str) -> bool:
    """Return True if ``table`` already exists in the bound database."""
    inspector = sa.inspect(bind)
    return table in inspector.get_table_names()


def upgrade() -> None:
    """Create the library_pages table plus its indexes."""
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
        sa.Column("category_slug", sa.Text(), nullable=False),
        sa.Column("brand_slug", sa.Text(), nullable=False),
        sa.Column("version_label", sa.Text(), nullable=True),
        sa.Column("rendered_html", sa.Text(), nullable=True),
        sa.Column(
            "metadata_json",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()).with_variant(
                sa.JSON(), "sqlite"
            ),
            nullable=False,
        ),
        sa.Column(
            "is_canonical",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "asset_version_id", "category_slug",
            name=_UQ_ASSET_VERSION_CATEGORY,
        ),
    )
    op.create_index(
        _IX_BRAND_CATEGORY,
        _TABLE_NAME,
        ["brand_slug", "category_slug"],
    )
    if bind.dialect.name == "postgresql":
        op.create_index(
            _IX_IS_CANONICAL,
            _TABLE_NAME,
            ["is_canonical"],
            postgresql_where=sa.text("is_canonical"),
        )
    else:
        op.create_index(_IX_IS_CANONICAL, _TABLE_NAME, ["is_canonical"])


def downgrade() -> None:
    """Drop the library_pages table; symmetric with upgrade."""
    bind = op.get_bind()
    if not _table_exists(bind, _TABLE_NAME):
        return
    op.drop_index(_IX_IS_CANONICAL, table_name=_TABLE_NAME)
    op.drop_index(_IX_BRAND_CATEGORY, table_name=_TABLE_NAME)
    op.drop_table(_TABLE_NAME)

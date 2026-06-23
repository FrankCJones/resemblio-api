"""Add head_html column to asset_components for DRL font link storage.

Revision ID: 0024_asset_components_head_html
Revises: 0023_asset_components
Create Date: 2026-06-23

schema_version: asset_component_v2

Motivation
----------
Issue #38 (Library v6 Epic #35, Step 2): faithful DRL component ingestion.

The original asset_components table (migration 0023) stores component_css
and component_html from the DRL asset.html ``<body>`` and ``<style>`` blocks.
It does not store the ``<link rel="stylesheet">`` font tags from the
``<head>``. When _compose_real_component composed a library page it fell back
to build_google_fonts_link_tag (brand font registry) which resolves fonts
differently from the DRL reference. This caused font-family mismatches in the
fidelity oracle (Issue #37).

Column design
-------------
head_html TEXT NOT NULL DEFAULT '': raw ``<link rel="stylesheet">`` tags
extracted from the DRL asset.html ``<head>`` (Google Fonts CDN links only;
preconnect and local resource links are excluded). Empty string for rows
seeded before this migration or assets with no Google Fonts dependency.

The empty-string default lets the application layer distinguish "no fonts"
(correct) from NULL (data error). The indexer uses head_html when non-empty
and falls back to the registry path for legacy empty rows.

Upgrade is additive (ALTER TABLE ADD COLUMN with a server default); it does
not lock the table for more than a metadata change on Postgres 16.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0024_asset_components_head_html"
down_revision = "0023_asset_components"
branch_labels = None
depends_on = None

_TABLE_NAME = "asset_components"


def upgrade() -> None:
    """Add head_html column to asset_components."""
    op.add_column(
        _TABLE_NAME,
        sa.Column(
            "head_html",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    """Remove head_html column from asset_components."""
    op.drop_column(_TABLE_NAME, "head_html")

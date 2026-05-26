"""Add optional API key spend caps.

Revision ID: 0002_api_key_spend_cap
Revises: 0001_initial_schema
Create Date: 2026-05-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_api_key_spend_cap"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add nullable per-key spend cap in cents."""
    op.add_column("api_keys", sa.Column("spend_cap_cents", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Remove per-key spend cap."""
    op.drop_column("api_keys", "spend_cap_cents")

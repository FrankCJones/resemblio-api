"""Add subscription_tier to users for Library export entitlement.

Revision ID: 0025_users_subscription_tier
Revises: 0024_asset_components_head_html
Create Date: 2026-08-29

Phase I needs a real user-level entitlement source. A logged-in session,
credit balance, API key, or Stripe customer id is not enough to unlock public
Library downloads.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0025_users_subscription_tier"
down_revision = "0024_asset_components_head_html"
branch_labels = None
depends_on = None

_TABLE_NAME = "users"
_COLUMN_NAME = "subscription_tier"
_CHECK_NAME = "ck_users_subscription_tier"
_CHECK_SQL = "subscription_tier IN ('free', 'solo', 'studio', 'pro', 'apiplus', 'enterprise')"


def upgrade() -> None:
    """Add the user-level subscription tier column and closed-set check."""
    op.add_column(
        _TABLE_NAME,
        sa.Column(_COLUMN_NAME, sa.String(length=32), nullable=False, server_default="free"),
    )
    with op.batch_alter_table(_TABLE_NAME) as batch:
        batch.create_check_constraint(_CHECK_NAME, _CHECK_SQL)


def downgrade() -> None:
    """Remove the subscription tier column and check."""
    with op.batch_alter_table(_TABLE_NAME) as batch:
        batch.drop_constraint(_CHECK_NAME, type_="check")
    op.drop_column(_TABLE_NAME, _COLUMN_NAME)

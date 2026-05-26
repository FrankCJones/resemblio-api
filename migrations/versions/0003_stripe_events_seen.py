"""Track processed Stripe webhook events.

Revision ID: 0003_stripe_events_seen
Revises: 0002_api_key_spend_cap
Create Date: 2026-05-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_stripe_events_seen"
down_revision = "0002_api_key_spend_cap"
branch_labels = None
depends_on = None


def _bigint() -> sa.TypeEngine[int]:
    """Return BIGINT with SQLite autoincrement compatibility."""
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    """Create webhook idempotency table."""
    op.create_table(
        "stripe_events_seen",
        sa.Column("id", _bigint(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Text(), nullable=False, unique=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_stripe_events_seen_event_id", "stripe_events_seen", ["event_id"], unique=True)


def downgrade() -> None:
    """Drop webhook idempotency table."""
    op.drop_index("ix_stripe_events_seen_event_id", table_name="stripe_events_seen")
    op.drop_table("stripe_events_seen")

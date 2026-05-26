"""Add topup_sessions table and credit_ledger balance non-negative CHECK.

Revision ID: 0004_topup_sessions_and_balance_check
Revises: 0003_stripe_events_seen
Create Date: 2026-05-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_topup_sessions_and_balance_check"
down_revision = "0003_stripe_events_seen"
branch_labels = None
depends_on = None


def _bigint() -> sa.TypeEngine[int]:
    """Return BIGINT with SQLite autoincrement compatibility."""
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    """Create topup_sessions table and add ledger balance CHECK."""
    op.create_table(
        "topup_sessions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("user_id", _bigint(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_topup_sessions_user_id", "topup_sessions", ["user_id"])
    # Batch mode lets SQLite recreate the table to add the CHECK; Postgres uses
    # ALTER TABLE ADD CONSTRAINT under the same call.
    with op.batch_alter_table("credit_ledger") as batch:
        batch.create_check_constraint("ck_credit_ledger_balance_non_negative", "balance_after_cents >= 0")


def downgrade() -> None:
    """Drop the CHECK constraint and topup_sessions table."""
    with op.batch_alter_table("credit_ledger") as batch:
        batch.drop_constraint("ck_credit_ledger_balance_non_negative", type_="check")
    op.drop_index("ix_topup_sessions_user_id", table_name="topup_sessions")
    op.drop_table("topup_sessions")

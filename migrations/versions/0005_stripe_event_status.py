"""Add status column to stripe_events_seen for stateful idempotency.

Revision ID: 0005_stripe_event_status
Revises: 0004_topup_sessions_and_balance_check
Create Date: 2026-05-25

The webhook handler used to mark events 'seen' before the credit ledger row
committed. Any failure between the claim and the commit permanently stranded
the customer because Stripe redelivery would short-circuit to duplicate-200
with no credit recorded.

Path B fix (see ``app/routes/webhooks.py``): the row carries a status of
``processing`` while the handler is in flight and flips to ``processed`` only
after side effects commit successfully. Handler failure marks status='failed'
(audit trail preserved); cycle-6 ``_mark_event_failed`` replaced the earlier
delete-row pattern.

Existing rows are backfilled to ``processed`` because pre-migration they
represented completed processing.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_stripe_event_status"
down_revision = "0004_topup_sessions_and_balance_check"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add status column with backfill to 'processed' for existing rows."""
    # ``server_default='processed'`` covers the backfill of existing rows in one
    # statement: every NULL slot the new column would otherwise carry takes the
    # default value at write time, which Postgres applies during ADD COLUMN.
    # batch_alter_table is used so SQLite (the test target) can recreate the
    # table to add the NOT NULL column.
    with op.batch_alter_table("stripe_events_seen") as batch:
        batch.add_column(
            sa.Column(
                "status",
                sa.Text(),
                nullable=False,
                server_default="processed",
            )
        )


def downgrade() -> None:
    """Drop the status column."""
    with op.batch_alter_table("stripe_events_seen") as batch:
        batch.drop_column("status")

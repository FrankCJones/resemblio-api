"""Add claimed_at lease column to stripe_events_seen.

Revision ID: 0006_stripe_event_claim_lease
Revises: 0005_stripe_event_status
Create Date: 2026-05-26

Cycle 7 (Codex cross-review of cycle 6) surfaced a stranding hazard: if a
webhook handler crashes AND the follow-up ``_mark_event_failed`` itself fails
to commit, the ``stripe_events_seen`` row stays at status='processing'
indefinitely. Every subsequent Stripe redelivery then short-circuits to the
in-flight branch in ``app/routes/webhooks.py`` and returns 200 with no credit.

Fix: a ``claimed_at`` timestamp scoped to the ``processing`` state acts as a
lease. The webhook's ON CONFLICT DO UPDATE WHERE clause re-claims any row
whose lease has expired (``_STALE_PROCESSING_LEASE_SECONDS = 300``). Existing
rows are backfilled to ``now()`` so the lease starts fresh; rows that have
already reached ``processed`` are unaffected because the WHERE clause restricts
re-claim to ``failed`` and stale ``processing``.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_stripe_event_claim_lease"
down_revision = "0005_stripe_event_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add nullable claimed_at column defaulting to now() on existing rows."""
    # ``server_default=func.now()`` ensures the backfill of any existing row is
    # a non-null timestamp at migration time; subsequent inserts also get a
    # default of now(). The column is nullable because production code only
    # writes claimed_at when the row enters 'processing', and a row that
    # transitions to 'processed' may later have claimed_at cleared by an
    # operational sweep without breaking the schema.
    with op.batch_alter_table("stripe_events_seen") as batch:
        batch.add_column(
            sa.Column(
                "claimed_at",
                sa.DateTime(timezone=True),
                nullable=True,
                server_default=sa.func.now(),
            )
        )


def downgrade() -> None:
    """Drop the claimed_at column."""
    with op.batch_alter_table("stripe_events_seen") as batch:
        batch.drop_column("claimed_at")

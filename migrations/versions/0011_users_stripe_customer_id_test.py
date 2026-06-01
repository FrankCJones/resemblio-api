"""Add users.stripe_customer_id_test column for cross-mode reconciliation forensics.

Revision ID: 0011_users_stripe_customer_id_test
Revises: 0010_auto_refund_audit_events
Create Date: 2026-06-01

schema_version: users_v2 (additive nullable column; no existing rows touched)

Motivation
----------
The 2026-06-01 17:53 UTC LIVE Stripe cutover rolled back when the checkout
flow tried to create a session bound to ``cus_UaUxR4NxLhUVFo``, a customer
object that existed in Stripe TEST but had never been replicated into LIVE.
The DB carried the TEST customer id in ``users.stripe_customer_id``; Stripe
LIVE returned ``No such customer`` and the API surfaced an opaque 500.

The reconciliation helper (``tools/resemblio_customer_reconcile.py``)
creates a new LIVE-mode customer for affected users and rewrites
``users.stripe_customer_id`` to the new LIVE id. The TEST id must NOT be
discarded: it remains the join key for forensics against Stripe TEST events
that may already have been recorded against the old id. This migration adds
``stripe_customer_id_test`` to hold that retained TEST id.

Idempotency
-----------
``op.add_column`` is wrapped in a probe against ``information_schema`` so a
re-run on a DB that already has the column is a no-op rather than a hard
fail. The downgrade is symmetric.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0011_users_stripe_customer_id_test"
down_revision = "0010_auto_refund_audit_events"
branch_labels = None
depends_on = None


_TABLE_NAME = "users"
_COLUMN_NAME = "stripe_customer_id_test"


def _column_exists(bind: sa.engine.Connection, table: str, column: str) -> bool:
    """Return True if ``table.column`` already exists in the bound database.

    Uses SQLAlchemy's inspector so the check works against both Postgres
    (prod) and SQLite (test fixtures) without dialect branching.
    """
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    return any(c["name"] == column for c in inspector.get_columns(table))


def upgrade() -> None:
    """Add the nullable VARCHAR(64) column if it is not already present."""
    bind = op.get_bind()
    if _column_exists(bind, _TABLE_NAME, _COLUMN_NAME):
        return
    op.add_column(
        _TABLE_NAME,
        sa.Column(_COLUMN_NAME, sa.String(64), nullable=True),
    )


def downgrade() -> None:
    """Drop the column if it exists; symmetric with upgrade."""
    bind = op.get_bind()
    if not _column_exists(bind, _TABLE_NAME, _COLUMN_NAME):
        return
    op.drop_column(_TABLE_NAME, _COLUMN_NAME)

"""S20 R4 auto-refund audit table.

Revision ID: 0010_auto_refund_audit_events
Revises: 0009_extractions_raw_quality_score
Create Date: 2026-05-31

schema_version: auto_refund_audit_v1 (one new append-only table; no existing
rows or columns touched).

Motivation
----------
R1 wired ``apply_heuristic_penalties`` into the extraction success path and
re-used the existing ``_refund`` helper for the credit reversal. R4 adds the
customer-facing email notification and a dedicated audit trail so the
business can answer two operational questions from row data alone:

1. "How many auto-refunds did we issue this week, on which extractions, for
   which penalty reason?"
2. "Did the customer get the explanatory email, or did Resend fail?"

The credit_ledger already records the refund debit/credit pair but it is a
financial record, not a customer-communication record. Mixing email-send
status into credit_ledger would muddy the financial audit; a dedicated
``auto_refund_audit_events`` table keeps the two concerns separate.

Idempotency
-----------
``extraction_id`` is UNIQUE. A second auto-refund attempt on the same
extraction (which the ``_refund`` helper already short-circuits via its own
ledger-row check) cannot insert a duplicate audit row even under a race
where two route handlers raced past the refund check. Defense in depth.

Downgrade
---------
Drops the table. The credit_ledger refund rows survive; only the customer-
communication audit is lost.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0010_auto_refund_audit_events"
down_revision = "0009_extractions_raw_quality_score"
branch_labels = None
depends_on = None


_TABLE_NAME = "auto_refund_audit_events"
_UNIQUE_EXTRACTION_INDEX = "ix_auto_refund_audit_events_extraction_id"
_CREATED_AT_INDEX = "ix_auto_refund_audit_events_created_at"


def upgrade() -> None:
    """Create the auto-refund audit table and supporting indexes."""
    op.create_table(
        _TABLE_NAME,
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column(
            "extraction_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey("extractions.id"),
            nullable=False,
        ),
        sa.Column("user_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("refund_amount_cents", sa.Integer(), nullable=False),
        sa.Column("penalized_score", sa.Float(), nullable=True),
        sa.Column("raw_score", sa.Float(), nullable=True),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("penalties_applied", sa.JSON(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=False),
        # Email-send status. Plain text vocabulary so a DBA reads the table
        # directly: "sent" | "failed" | "skipped_no_sender". Failures are
        # explicitly recorded (with reason in ``email_error``) so the email
        # never blocks the refund itself.
        sa.Column("email_status", sa.String(32), nullable=False),
        sa.Column("email_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(_UNIQUE_EXTRACTION_INDEX, _TABLE_NAME, ["extraction_id"], unique=True)
    op.create_index(_CREATED_AT_INDEX, _TABLE_NAME, ["created_at"])


def downgrade() -> None:
    """Drop the auto-refund audit table and its indexes."""
    op.drop_index(_CREATED_AT_INDEX, table_name=_TABLE_NAME)
    op.drop_index(_UNIQUE_EXTRACTION_INDEX, table_name=_TABLE_NAME)
    op.drop_table(_TABLE_NAME)

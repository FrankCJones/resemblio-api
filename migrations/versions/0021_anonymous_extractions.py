"""Add anonymous extractions support (Stage O1).

Revision ID: 0021_anonymous_extractions
Revises: 0020_library_pages
Create Date: 2026-06-03

schema_version: anonymous_extractions_v1

Motivation
----------
Stage O1 of the URL-first central onboarding flow (per
``projects/OptSus Team/cto-reviews/2026-06-03-resemblio-url-first-onboarding-respec.md``)
ships an unauthenticated extraction endpoint. A stranger arriving at
resemblio.com can fire one extraction per IP per 24 hours without an
account; the result is bound to an opaque ``claim_token`` so the user
can convert to an account later and inherit ownership of the row.

Three tables land in this migration:

1. ``anonymous_extractions`` - claim-token registry. ``claim_token`` is
   32 random URL-safe bytes; ``ip_hash`` is the SHA-256 of the client IP
   (raw IP never persisted to keep PII narrow); ``extraction_id`` is a
   nullable FK to the ``extractions`` row that was actually created
   (NULL when the URL classified out-of-scope and no extraction ran).
   ``status`` is one of ``pending|complete|refunded|expired``.
   ``expires_at`` enforces a 24h claim window; a daily cleanup script
   reaps expired rows.

2. ``anon_extract_counters`` - Postgres-backed per-IP daily counter.
   The existing in-process token-bucket limiter (``app/rate_limit.py``)
   is process-local; multi-worker deploys would silently multiply the
   ceiling. Anonymous extraction MUST be cross-process correct because
   we do not want abusers discovering they can rate-limit-bypass by
   hammering during a uvicorn reload. A simple ``(ip_hash, day, count)``
   row with UNIQUE on ``(ip_hash, day)`` is enough; the route handler
   UPSERTs and checks the count against the configured per-IP daily cap.

3. ``notify_requests`` - append-only capture for unsupported-class URLs
   so we can email the visitor when the class becomes supported. No
   PII beyond the email + URL + detected class.

Idempotency
-----------
Probes ``information_schema`` for each table and short-circuits if it
already exists; downgrade drops each table only if present.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0021_anonymous_extractions"
down_revision = "0020_library_pages"
branch_labels = None
depends_on = None


_TABLE_ANON_EXTRACT = "anonymous_extractions"
_TABLE_ANON_COUNTERS = "anon_extract_counters"
_TABLE_NOTIFY = "notify_requests"

_IX_ANON_EXTRACT_CLAIM_TOKEN = "ix_anonymous_extractions_claim_token"
_IX_ANON_EXTRACT_IP_HASH = "ix_anonymous_extractions_ip_hash"
_IX_ANON_EXTRACT_EXPIRES = "ix_anonymous_extractions_expires_at"
_UQ_ANON_COUNTERS_IP_DAY = "uq_anon_extract_counters_ip_day"
_IX_NOTIFY_CREATED = "ix_notify_requests_created_at"


def _table_exists(bind: sa.engine.Connection, table: str) -> bool:
    """Return True if ``table`` already exists in the bound database."""
    inspector = sa.inspect(bind)
    return table in inspector.get_table_names()


def upgrade() -> None:
    """Create the three anonymous-extraction tables plus indexes."""
    bind = op.get_bind()

    if not _table_exists(bind, _TABLE_ANON_EXTRACT):
        op.create_table(
            _TABLE_ANON_EXTRACT,
            sa.Column(
                "id",
                sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                primary_key=True,
                autoincrement=True,
                nullable=False,
            ),
            sa.Column("claim_token", sa.String(64), nullable=False),
            sa.Column("ip_hash", sa.String(64), nullable=False),
            sa.Column(
                "extraction_id",
                sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                sa.ForeignKey("extractions.id"),
                nullable=True,
            ),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("classification", sa.String(32), nullable=False),
            sa.Column(
                "status",
                sa.String(16),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("claim_token", name="uq_anonymous_extractions_claim_token"),
        )
        op.create_index(
            _IX_ANON_EXTRACT_CLAIM_TOKEN, _TABLE_ANON_EXTRACT, ["claim_token"], unique=True
        )
        op.create_index(_IX_ANON_EXTRACT_IP_HASH, _TABLE_ANON_EXTRACT, ["ip_hash"])
        op.create_index(_IX_ANON_EXTRACT_EXPIRES, _TABLE_ANON_EXTRACT, ["expires_at"])

    if not _table_exists(bind, _TABLE_ANON_COUNTERS):
        op.create_table(
            _TABLE_ANON_COUNTERS,
            sa.Column(
                "id",
                sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                primary_key=True,
                autoincrement=True,
                nullable=False,
            ),
            sa.Column("ip_hash", sa.String(64), nullable=False),
            # Day bucket as ISO-8601 date string (UTC). Stored as TEXT for
            # cross-dialect simplicity; the route handler computes the
            # bucket from utcnow().date().isoformat() so all callers agree.
            sa.Column("day", sa.String(10), nullable=False),
            sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint("ip_hash", "day", name=_UQ_ANON_COUNTERS_IP_DAY),
        )

    if not _table_exists(bind, _TABLE_NOTIFY):
        op.create_table(
            _TABLE_NOTIFY,
            sa.Column(
                "id",
                sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                primary_key=True,
                autoincrement=True,
                nullable=False,
            ),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("email", sa.Text(), nullable=False),
            sa.Column("detected_class", sa.String(32), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(_IX_NOTIFY_CREATED, _TABLE_NOTIFY, ["created_at"])


def downgrade() -> None:
    """Drop the three tables; symmetric with upgrade."""
    bind = op.get_bind()
    if _table_exists(bind, _TABLE_NOTIFY):
        op.drop_index(_IX_NOTIFY_CREATED, table_name=_TABLE_NOTIFY)
        op.drop_table(_TABLE_NOTIFY)
    if _table_exists(bind, _TABLE_ANON_COUNTERS):
        op.drop_table(_TABLE_ANON_COUNTERS)
    if _table_exists(bind, _TABLE_ANON_EXTRACT):
        op.drop_index(_IX_ANON_EXTRACT_EXPIRES, table_name=_TABLE_ANON_EXTRACT)
        op.drop_index(_IX_ANON_EXTRACT_IP_HASH, table_name=_TABLE_ANON_EXTRACT)
        op.drop_index(_IX_ANON_EXTRACT_CLAIM_TOKEN, table_name=_TABLE_ANON_EXTRACT)
        op.drop_table(_TABLE_ANON_EXTRACT)

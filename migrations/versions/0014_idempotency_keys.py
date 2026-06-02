"""Add idempotency_keys table for POST /v1/extractions replay safety.

Revision ID: 0014_idempotency_keys
Revises: 0013_web_session_keys
Create Date: 2026-06-02

schema_version: idempotency_keys_v1 (one new table; no existing rows touched).

Motivation
----------
``POST /v1/extractions`` is a money-moving endpoint: it debits the user's
credit ledger before performing the extraction. A network-level retry
that lands on a second running of the same logical request would
double-charge the customer absent idempotency support. The
``Idempotency-Key`` header (RFC draft + Stripe convention) lets the
client opt-in to replay safety: a second request carrying the same key
returns the cached response from the first call instead of charging
again.

Table shape
-----------
``(user_id, key)`` is the composite primary key. Scoping to ``user_id``
means a token leaked from one tenant cannot collide with another
tenant's key namespace. ``request_hash`` (SHA-256 of the canonical body)
catches "same key, different body" misuse (always a client bug) and
lets the route 409 instead of silently replaying the wrong response.

``response_body`` is a serialized JSON string; we replay the bytes
verbatim so an extraction's status, manifest, signed URLs, and quality
score all round-trip exactly. Signed URLs in the cached body may have
expired by replay time; clients that need fresh URLs re-fetch via
``GET /v1/extractions/{id}``.

TTL
---
Rows older than ``IDEMPOTENCY_KEY_TTL_SECONDS`` (24h) are filtered out
at lookup time. ``created_at`` is indexed so a sweep job (deferred to
operations) can prune cheaply.

Idempotency of the migration itself
-----------------------------------
Probes ``information_schema`` and short-circuits if the table already
exists; downgrade is symmetric.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0014_idempotency_keys"
down_revision = "0013_web_session_keys"
branch_labels = None
depends_on = None


_TABLE_NAME = "idempotency_keys"
_CREATED_AT_INDEX = "ix_idempotency_keys_created_at"


def _table_exists(bind: sa.engine.Connection, table: str) -> bool:
    """Return True if ``table`` already exists in the bound database."""
    inspector = sa.inspect(bind)
    return table in inspector.get_table_names()


def upgrade() -> None:
    """Create the idempotency_keys table plus the created_at sweep index."""
    bind = op.get_bind()
    if _table_exists(bind, _TABLE_NAME):
        return
    op.create_table(
        _TABLE_NAME,
        sa.Column(
            "user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey("users.id"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("key", sa.String(length=256), primary_key=True, nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("response_body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(_CREATED_AT_INDEX, _TABLE_NAME, ["created_at"])


def downgrade() -> None:
    """Drop the idempotency_keys table and its index; symmetric with upgrade."""
    bind = op.get_bind()
    if not _table_exists(bind, _TABLE_NAME):
        return
    op.drop_index(_CREATED_AT_INDEX, table_name=_TABLE_NAME)
    op.drop_table(_TABLE_NAME)

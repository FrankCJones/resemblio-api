"""Add magic_link_tokens table for passwordless signup/login.

Revision ID: 0012_magic_link_tokens
Revises: 0011_users_stripe_customer_id_test
Create Date: 2026-06-01

schema_version: magic_link_tokens_v1 (one new table; no existing rows touched).

Motivation
----------
S3 introduces passwordless auth via Resend-delivered magic links. The web
BFF (Next.js) calls the FastAPI internal-auth endpoints, which mint a
single-use, time-limited token whose SHA-256 hash is stored here. The
plaintext token is never persisted on the server side; we only ever
compare hashes. Single-use is enforced by ``consumed_at`` going from NULL
to a UTC timestamp on the first successful redemption.

Keyed by ``email`` (not ``user_id``) on purpose: the user row may not
exist yet at the moment the magic link is requested. The
``request_magic_link`` flow is anti-enumeration: it never reveals whether
the email maps to an existing user, so token issuance must not depend on
a user row being present.

Idempotency
-----------
The upgrade probes ``information_schema`` so a re-run is a no-op rather
than a hard fail. The downgrade is symmetric.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0012_magic_link_tokens"
down_revision = "0011_users_stripe_customer_id_test"
branch_labels = None
depends_on = None


_TABLE_NAME = "magic_link_tokens"
_EMAIL_INDEX = "ix_magic_link_tokens_email"
_TOKEN_HASH_INDEX = "ix_magic_link_tokens_token_hash"


def _table_exists(bind: sa.engine.Connection, table: str) -> bool:
    """Return True if ``table`` already exists in the bound database."""
    inspector = sa.inspect(bind)
    return table in inspector.get_table_names()


def upgrade() -> None:
    """Create the magic_link_tokens table and supporting indexes."""
    bind = op.get_bind()
    if _table_exists(bind, _TABLE_NAME):
        return
    op.create_table(
        _TABLE_NAME,
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip", sa.Text(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
    )
    op.create_index(_EMAIL_INDEX, _TABLE_NAME, ["email"])
    op.create_index(_TOKEN_HASH_INDEX, _TABLE_NAME, ["token_hash"], unique=True)


def downgrade() -> None:
    """Drop the magic_link_tokens table and its indexes."""
    bind = op.get_bind()
    if not _table_exists(bind, _TABLE_NAME):
        return
    op.drop_index(_TOKEN_HASH_INDEX, table_name=_TABLE_NAME)
    op.drop_index(_EMAIL_INDEX, table_name=_TABLE_NAME)
    op.drop_table(_TABLE_NAME)

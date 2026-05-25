"""Initial Resemblio API schema.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-05-25
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def _bigint() -> sa.TypeEngine[int]:
    """Return BIGINT with SQLite autoincrement compatibility."""
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _jsonb() -> sa.TypeEngine[object]:
    """Return JSONB for Postgres with SQLite JSON compatibility."""
    return postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def _inet() -> sa.TypeEngine[str]:
    """Return INET for Postgres with SQLite string compatibility."""
    return postgresql.INET().with_variant(sa.String(length=64), "sqlite")


def upgrade() -> None:
    """Create all S1 tables and indexes."""
    op.create_table(
        "users",
        sa.Column("id", _bigint(), primary_key=True, autoincrement=True),
        sa.Column("email", sa.Text(), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("stripe_customer_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", _bigint(), primary_key=True, autoincrement=True),
        sa.Column("user_id", _bigint(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("key_prefix", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("scopes", _jsonb(), nullable=False, server_default='["extract"]'),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        sa.Column("created_from_ip", _inet(), nullable=True),
        sa.Column("grace_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "api_key_events",
        sa.Column("id", _bigint(), primary_key=True, autoincrement=True),
        sa.Column("api_key_id", _bigint(), sa.ForeignKey("api_keys.id"), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("ip", _inet(), nullable=True),
        sa.Column("metadata", _jsonb(), nullable=True),
    )
    op.create_table(
        "extractions",
        sa.Column("id", _bigint(), primary_key=True, autoincrement=True),
        sa.Column("user_id", _bigint(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("api_key_id", _bigint(), sa.ForeignKey("api_keys.id"), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("url_normalized", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("tokens_json", _jsonb(), nullable=True),
        sa.Column("dtcg_json", _jsonb(), nullable=True),
        sa.Column("r2_zip_key", sa.Text(), nullable=True),
        sa.Column("zip_sha256", sa.Text(), nullable=True),
        sa.Column("error_log", sa.Text(), nullable=True),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("credit_cents", sa.Integer(), nullable=False, server_default="500"),
    )
    op.create_table(
        "credit_ledger",
        sa.Column("id", _bigint(), primary_key=True, autoincrement=True),
        sa.Column("user_id", _bigint(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("entry_type", sa.Text(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("balance_after_cents", sa.Integer(), nullable=False),
        sa.Column("stripe_payment_intent_id", sa.Text(), nullable=True),
        sa.Column("extraction_id", _bigint(), sa.ForeignKey("extractions.id"), nullable=True),
        sa.Column("api_key_id", _bigint(), sa.ForeignKey("api_keys.id"), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    _create_indexes()


def _create_indexes() -> None:
    """Create non-primary indexes after all tables exist."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.create_index("ix_users_email_lower", "users", [sa.text("lower(email)")], unique=True)
    else:
        op.create_index("ix_users_email_lower", "users", ["email"], unique=False)
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])
    op.create_index("ix_api_keys_status", "api_keys", ["status"])
    op.create_index("ix_api_keys_grace_expires_at", "api_keys", ["grace_expires_at"])
    op.create_index("ix_api_key_events_api_key_id_occurred_at", "api_key_events", ["api_key_id", "occurred_at"])
    op.create_index("ix_extractions_user_id_extracted_at", "extractions", ["user_id", "extracted_at"])
    op.create_index("ix_extractions_url_normalized", "extractions", ["url_normalized"])
    op.create_index("ix_credit_ledger_user_id_created_at", "credit_ledger", ["user_id", "created_at"])


def downgrade() -> None:
    """Drop all S1 indexes and tables in dependency order."""
    _drop_indexes(
        [
            ("ix_credit_ledger_user_id_created_at", "credit_ledger"),
            ("ix_extractions_url_normalized", "extractions"),
            ("ix_extractions_user_id_extracted_at", "extractions"),
            ("ix_api_key_events_api_key_id_occurred_at", "api_key_events"),
            ("ix_api_keys_grace_expires_at", "api_keys"),
            ("ix_api_keys_status", "api_keys"),
            ("ix_api_keys_user_id", "api_keys"),
            ("ix_users_email_lower", "users"),
        ]
    )
    op.drop_table("credit_ledger")
    op.drop_table("extractions")
    op.drop_table("api_key_events")
    op.drop_table("api_keys")
    op.drop_table("users")


def _drop_indexes(indexes: Sequence[tuple[str, str]]) -> None:
    """Drop indexes while keeping downgrade ordering explicit."""
    for name, table in indexes:
        op.drop_index(name, table_name=table)


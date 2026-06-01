"""Add web_session_keys and api_keys.kind / is_visible_to_user.

Revision ID: 0013_web_session_keys
Revises: 0012_magic_link_tokens
Create Date: 2026-06-01

schema_version: web_session_keys_v1 (one new table, two additive nullable-
defaulted columns on api_keys; existing api_keys rows are backfilled to
the previous semantic ('user', visible)).

Motivation
----------
S3 introduces the BFF (backend-for-frontend) bridge between the Next.js
web app and the FastAPI API. The web process holds an opaque server-side
``internal_bff`` API key per user; the browser never sees it. To keep
visible-key flows (the dashboard "your API keys" list) from accidentally
showing the BFF key, ``api_keys`` grows two markers:

* ``kind`` (``'user' | 'internal_bff' | 'service'``) - the canonical
  classifier. Authoritative for behavior (rotation policy, scope checks,
  account-page visibility).
* ``is_visible_to_user`` - the simple boolean the dashboard list filters
  on. Derived from ``kind`` at signup time but stored separately so a
  service key can be flipped invisible without changing its semantic
  ``kind`` for audit purposes.

``web_session_keys`` is the join table that maps a user to the api_key
row currently acting as their BFF session key. Exactly one active BFF
key per user (UNIQUE on ``api_key_id``); rotation on a new login revokes
the old key and inserts a new row.

Backfill
--------
All existing api_keys rows are user-visible "user" kind. The migration
sets ``kind='user'`` and ``is_visible_to_user=true`` for every existing
row before the columns are made NOT NULL.

Idempotency
-----------
Each step probes ``information_schema`` and is skipped if the artifact
already exists. The downgrade is symmetric.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0013_web_session_keys"
down_revision = "0012_magic_link_tokens"
branch_labels = None
depends_on = None


_API_KEYS_TABLE = "api_keys"
_KIND_COLUMN = "kind"
_VISIBLE_COLUMN = "is_visible_to_user"
_SESSION_TABLE = "web_session_keys"
_SESSION_USER_INDEX = "ix_web_session_keys_user_id"
_SESSION_API_KEY_UNIQUE = "ix_web_session_keys_api_key_id"


def _column_exists(bind: sa.engine.Connection, table: str, column: str) -> bool:
    """Return True if ``table.column`` already exists in the bound database."""
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    return any(c["name"] == column for c in inspector.get_columns(table))


def _table_exists(bind: sa.engine.Connection, table: str) -> bool:
    """Return True if ``table`` already exists in the bound database."""
    inspector = sa.inspect(bind)
    return table in inspector.get_table_names()


def upgrade() -> None:
    """Add api_keys.kind, api_keys.is_visible_to_user, and web_session_keys."""
    bind = op.get_bind()

    if not _column_exists(bind, _API_KEYS_TABLE, _KIND_COLUMN):
        op.add_column(
            _API_KEYS_TABLE,
            sa.Column(_KIND_COLUMN, sa.Text(), nullable=False, server_default="user"),
        )
        # Backfill existing rows so the server_default does not leave any
        # rows ambiguous. The server_default stays in place so subsequent
        # inserts that omit ``kind`` continue to land as 'user'.
        op.execute(
            sa.text(f"UPDATE {_API_KEYS_TABLE} SET {_KIND_COLUMN} = 'user' WHERE {_KIND_COLUMN} IS NULL")
        )

    if not _column_exists(bind, _API_KEYS_TABLE, _VISIBLE_COLUMN):
        op.add_column(
            _API_KEYS_TABLE,
            sa.Column(_VISIBLE_COLUMN, sa.Boolean(), nullable=False, server_default=sa.true()),
        )
        op.execute(
            sa.text(
                f"UPDATE {_API_KEYS_TABLE} SET {_VISIBLE_COLUMN} = TRUE WHERE {_VISIBLE_COLUMN} IS NULL"
            )
        )

    if not _table_exists(bind, _SESSION_TABLE):
        op.create_table(
            _SESSION_TABLE,
            sa.Column(
                "id",
                sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                primary_key=True,
                autoincrement=True,
            ),
            sa.Column(
                "user_id",
                sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                sa.ForeignKey("users.id"),
                nullable=False,
            ),
            sa.Column(
                "api_key_id",
                sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                sa.ForeignKey("api_keys.id"),
                nullable=False,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(_SESSION_USER_INDEX, _SESSION_TABLE, ["user_id"])
        op.create_index(_SESSION_API_KEY_UNIQUE, _SESSION_TABLE, ["api_key_id"], unique=True)


def downgrade() -> None:
    """Drop web_session_keys and the two api_keys columns; symmetric with upgrade."""
    bind = op.get_bind()

    if _table_exists(bind, _SESSION_TABLE):
        op.drop_index(_SESSION_API_KEY_UNIQUE, table_name=_SESSION_TABLE)
        op.drop_index(_SESSION_USER_INDEX, table_name=_SESSION_TABLE)
        op.drop_table(_SESSION_TABLE)

    if _column_exists(bind, _API_KEYS_TABLE, _VISIBLE_COLUMN):
        op.drop_column(_API_KEYS_TABLE, _VISIBLE_COLUMN)

    if _column_exists(bind, _API_KEYS_TABLE, _KIND_COLUMN):
        op.drop_column(_API_KEYS_TABLE, _KIND_COLUMN)

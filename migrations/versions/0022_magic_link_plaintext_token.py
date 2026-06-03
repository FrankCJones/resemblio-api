"""Add magic_link_tokens.plaintext_token column for the test-auth E2E surface.

Revision ID: 0022_magic_link_plaintext_token
Revises: 0021_anonymous_extractions
Create Date: 2026-06-03

schema_version: magic_link_tokens_v2 (additive nullable column; no existing rows touched)

Motivation
----------
The O9 Playwright E2E suite cannot scrape a real inbox to retrieve a
magic-link token; it needs a programmatic readback path. The readback
endpoint at ``GET /v1/internal/auth/test_get_latest_magic_link`` returns
the latest unconsumed plaintext token for an email, but is dark by
default and refuses to respond unless BOTH ``RESEMBLIO_TEST_AUTH_ENABLED``
and ``RESEMBLIO_TEST_AUTH_TOKEN`` are set in the environment AND the
request carries a matching ``X-Test-Auth`` header.

This column is the plaintext mirror the readback endpoint reads from.
The ``request_magic_link`` route writes to it ONLY when the test-auth
surface is enabled; on prod with the flag off the column stays NULL and
the plaintext lives only in the outbound email body. The readback
endpoint refuses to return rows where ``plaintext_token`` is NULL, so a
prod deploy that toggles only the readback flag (without the write-side
companion) still cannot leak plaintext for any token minted before the
flag was set.

WARNING: enabling the test-auth surface on a prod box is a critical
safety violation. The combination of plaintext-token readback + a known
email bypasses email-as-second-factor for any account whose address the
caller knows. Both env vars must remain unset on every prod box.

Idempotency
-----------
``op.add_column`` is guarded by an information_schema probe so a re-run
on a DB that already has the column is a no-op. The downgrade is
symmetric.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0022_magic_link_plaintext_token"
down_revision = "0021_anonymous_extractions"
branch_labels = None
depends_on = None


_TABLE_NAME = "magic_link_tokens"
_COLUMN_NAME = "plaintext_token"


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
    """Add the nullable Text column if it is not already present."""
    bind = op.get_bind()
    if _column_exists(bind, _TABLE_NAME, _COLUMN_NAME):
        return
    op.add_column(
        _TABLE_NAME,
        sa.Column(_COLUMN_NAME, sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Drop the column if it exists; symmetric with upgrade."""
    bind = op.get_bind()
    if not _column_exists(bind, _TABLE_NAME, _COLUMN_NAME):
        return
    op.drop_column(_TABLE_NAME, _COLUMN_NAME)

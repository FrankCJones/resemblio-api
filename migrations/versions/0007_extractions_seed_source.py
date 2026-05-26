"""Add seed_source + source_id columns and relax api_key_id on extractions.

Revision ID: 0007_extractions_seed_source
Revises: 0006_stripe_event_claim_lease
Create Date: 2026-05-26

schema_version: 1 (extraction row contract unchanged; seed rows reuse
``SCHEMA_V1`` from ``app/constants.py``).

Motivation
----------
Bulk-seeding the public corpus from the Design Reference Library (DRL) needs
three schema accommodations so the seeder can write directly to Postgres,
bypassing the HTTP API and credit ledger (see
``scripts/SEED_FROM_DRL_DESIGN.md``):

1. ``seed_source TEXT NULL`` - marks a row as seeded vs organic. Examples:
   ``"drl_v1"``. Default API queries can filter ``seed_source IS NULL`` until
   the v1.1 public-corpus visibility flip ships.

2. ``source_id TEXT NULL`` - the per-asset identifier within ``seed_source``.
   Examples: ``"a24/alphabets/a24"``, ``"anthropic/wholes/hero-001"``. Composed
   with ``seed_source`` to form the idempotency key.

3. ``api_key_id`` becomes nullable. Seed rows are not billed and are not owned
   by an API key. Verified safe via grep over ``app/`` for usages: no joins,
   asserts, or non-null assumptions exist beyond the FK column definition
   itself; the only writers (``app/routes/extractions.py``) always supply a
   value and continue to do so for organic rows.

Idempotency
-----------
A partial unique index on ``(seed_source, source_id)`` constrained to rows
where ``seed_source IS NOT NULL`` makes the bulk-seed script re-runnable
end-to-end. Organic rows (``seed_source IS NULL``) are unaffected by the
constraint.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_extractions_seed_source"
down_revision = "0006_stripe_event_claim_lease"
branch_labels = None
depends_on = None


_UX_INDEX_NAME = "ux_extractions_seed_source_id"


def upgrade() -> None:
    """Add seed_source + source_id, relax api_key_id, add partial unique index."""
    with op.batch_alter_table("extractions") as batch:
        batch.add_column(sa.Column("seed_source", sa.Text(), nullable=True))
        batch.add_column(sa.Column("source_id", sa.Text(), nullable=True))
        batch.alter_column("api_key_id", existing_type=sa.BigInteger(), nullable=True)

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Partial unique index: only seeded rows participate. Organic rows
        # (seed_source IS NULL) carry no idempotency constraint.
        op.create_index(
            _UX_INDEX_NAME,
            "extractions",
            ["seed_source", "source_id"],
            unique=True,
            postgresql_where=sa.text("seed_source IS NOT NULL"),
        )
    else:
        # SQLite (test/dev) does not support partial unique indexes with the
        # same ``postgresql_where`` syntax, but it does support partial
        # indexes via plain SQL. Fall back to a non-unique helper index;
        # idempotency in tests is enforced application-side via SELECT then
        # INSERT/UPDATE.
        op.create_index(_UX_INDEX_NAME, "extractions", ["seed_source", "source_id"], unique=False)


def downgrade() -> None:
    """Drop the partial unique index, restore NOT NULL, drop seed columns."""
    op.drop_index(_UX_INDEX_NAME, table_name="extractions")
    with op.batch_alter_table("extractions") as batch:
        batch.alter_column("api_key_id", existing_type=sa.BigInteger(), nullable=False)
        batch.drop_column("source_id")
        batch.drop_column("seed_source")

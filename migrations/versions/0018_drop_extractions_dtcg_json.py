"""Drop extractions.dtcg_json now that asset_versions owns the payload.

Revision ID: 0018_drop_extractions_dtcg_json
Revises: 0017_backfill_asset_versions
Create Date: 2026-06-02

schema_version: 1 (the column is removed; no shape change for callers).

JIM-GATED. Do NOT run on prod in the same deploy as 0015-0017.
-------------------------------------------------------------
The deploy sequence is:

  Deploy A (immediate): apply 0015 + 0016 + 0017.
    - asset_versions table exists.
    - extractions.asset_version_id populated for every historical row with
      a non-null dtcg_json.
    - Application code on Deploy A writes BOTH extractions.dtcg_json AND
      the new asset_versions row on every fresh extraction (belt-and-braces
      until the drop migration is verified).

  Frank/Jim verification (between Deploy A and Deploy B):
    - SELECT COUNT(*) FROM extractions WHERE dtcg_json IS NOT NULL
      AND asset_version_id IS NULL  -> must be 0.
    - Spot-check that GET /v1/extractions/{id} still returns identical
      response bodies against a frozen smoke fixture.
    - Spot-check POST /v1/convert/shadcn/{id} against the same.

  Deploy B (separate, gated): apply 0018.
    - extractions.dtcg_json dropped.
    - extractions.r2_zip_key stays on extractions (it is per-extraction
      ZIP bytes; the per-snapshot asset_versions.raw_assets_url is a
      future-use slot, not a 1:1 swap).

Note on r2_zip_key
------------------
The mission-brief said "drop extractions.dtcg_json + extractions.raw_assets_url"
but the live schema never had ``raw_assets_url`` on extractions; the
equivalent column is ``r2_zip_key``. ZIP bytes are extraction-scoped (the
file lives under ``extractions/<user_id>/<extraction_id>.zip``); collapsing
the ZIP pointer onto asset_versions would conflate "this particular run"
(extractions) with "this particular content snapshot" (asset_versions) and
break the existing signed-URL paths in ``app/routes/extractions.py``. The
intent of the brief (decouple the snapshot from the run) is met by moving
``dtcg_json`` alone; ``r2_zip_key`` stays where it is.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0018_drop_extractions_dtcg_json"
down_revision = "0017_backfill_asset_versions"
branch_labels = None
depends_on = None


def _column_exists(bind: sa.engine.Connection, table: str, column: str) -> bool:
    """Return True if ``table.column`` exists in the bound database."""
    inspector = sa.inspect(bind)
    return any(c["name"] == column for c in inspector.get_columns(table))


def upgrade() -> None:
    """Drop extractions.dtcg_json. Partial: r2_zip_key intentionally stays."""
    bind = op.get_bind()
    if not _column_exists(bind, "extractions", "dtcg_json"):
        return
    with op.batch_alter_table("extractions") as batch:
        batch.drop_column("dtcg_json")


def downgrade() -> None:
    """Re-add the dtcg_json column NULL. Data is NOT restored; manual replay required.

    Restoring the data means re-joining each extractions row to its
    asset_versions row and writing the payload back. That replay is not
    automated here because the table sizes involved are small enough for an
    operator to script the recovery from a single SQL JOIN, and a fully
    automatic downgrade would silently mask whichever shape mismatch
    motivated the rollback.
    """
    bind = op.get_bind()
    if _column_exists(bind, "extractions", "dtcg_json"):
        return
    with op.batch_alter_table("extractions") as batch:
        batch.add_column(
            sa.Column(
                "dtcg_json",
                sa.dialects.postgresql.JSONB(astext_type=sa.Text()).with_variant(
                    sa.JSON(), "sqlite"
                ),
                nullable=True,
            )
        )

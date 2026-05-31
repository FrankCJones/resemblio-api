"""S20 heuristic-penalty audit column on the extractions table.

Revision ID: 0009_extractions_raw_quality_score
Revises: 0008_extractions_quality_review
Create Date: 2026-05-31

schema_version: 1 (extraction row contract additive; one new nullable column).

Motivation
----------
Quality-heuristics dispatch 2026-05-31 wires
``app.quality_heuristics.apply_heuristic_penalties`` into the success path
of ``POST /v1/extractions``. The penalized composite score becomes the
customer-facing ``quality_score`` and the value that gates the
``low_quality`` / auto-refund path. To preserve auditability of the base
score (so calibration drift between the raw scorer and the heuristic
penalties stays observable from row data alone), the raw composite is now
persisted alongside the penalized one in a dedicated column.

Columns added
-------------
1. ``raw_quality_score`` (Float, nullable): the composite from
   ``compute_quality_score`` BEFORE heuristic penalties. Null when scoring
   has not been run (failed extractions, pre-S20 rows, seed rows).

Downgrade
---------
Drops the column. The column is nullable so rolling back drops the audit
field without touching any persisted business logic. The customer-facing
``quality_score`` continues to carry the penalized value.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0009_extractions_raw_quality_score"
down_revision = "0008_extractions_quality_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the raw (pre-penalty) quality score audit column."""
    op.add_column(
        "extractions",
        sa.Column("raw_quality_score", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    """Drop the raw quality score audit column."""
    op.drop_column("extractions", "raw_quality_score")

"""S20 output-quality scoring columns on the extractions table.

Revision ID: 0008_extractions_quality_review
Revises: 0007_extractions_seed_source
Create Date: 2026-05-31

schema_version: 1 (extraction row contract additive; six new nullable columns
plus one boolean with default false. The persisted ``quality_score_v1`` payload
follows ``app/quality_scoring.py``.)

Motivation
----------
S20 ADR (Resemblio_BUILD_LOG.md, search "S20 ADR", 2026-05-26) adds an
output-quality scorer that runs after a successful extraction. A low score
classifies the run as ``low_quality_output``, refunds the credit, and flags
the row for operator review. The scorer needs durable per-row state so the
review queue is a one-query GET and so weight calibration can be reproduced
from row data alone.

Columns added
-------------
1. ``quality_score`` (Float, nullable): composite 0.0-1.0 score from the
   six-dimension scorer. Null when scoring has not been run (e.g. failed
   extractions, seed rows pre-script).
2. ``quality_dimension_scores`` (JSON, nullable): per-dimension floats keyed
   by dimension name. Lets a reviewer reproduce the composite arithmetic.
3. ``low_quality_review_pending`` (Boolean, not null, default false): the
   queue flag. Indexed for the operator review query.
4. ``low_quality_reviewed_at`` (DateTime with tz, nullable): when an
   operator closed the review.
5. ``low_quality_review_verdict`` (String(32), nullable):
   ``"agreed_low_quality"`` | ``"false_positive"`` | null. Drives the
   false-positive monitoring loop (CTO hardening backlog).
6. ``low_quality_reviewer`` (String(64), nullable): operator identifier
   (``"frank"`` for v1.1.x; ``"system"`` if any automated verdict is added
   later).

Index
-----
``ix_extractions_low_quality_review_pending`` on
``(low_quality_review_pending)`` makes the review queue
``SELECT * FROM extractions WHERE low_quality_review_pending = true`` a
single-index scan.

Downgrade
---------
Drops the index then drops each column in reverse order, mirroring the
upgrade for clean rollback. The columns are nullable (except the boolean
which has a default), so existing rows survive a forward-then-back without
data loss; the boolean's default-false re-populates rolled-back rows on
re-upgrade.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0008_extractions_quality_review"
down_revision = "0007_extractions_seed_source"
branch_labels = None
depends_on = None


_REVIEW_INDEX_NAME = "ix_extractions_low_quality_review_pending"


def upgrade() -> None:
    """Add the S20 quality-review columns and the review-queue index."""
    op.add_column(
        "extractions",
        sa.Column("quality_score", sa.Float(), nullable=True),
    )
    op.add_column(
        "extractions",
        sa.Column("quality_dimension_scores", sa.JSON(), nullable=True),
    )
    op.add_column(
        "extractions",
        sa.Column(
            "low_quality_review_pending",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "extractions",
        sa.Column("low_quality_reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "extractions",
        sa.Column("low_quality_review_verdict", sa.String(32), nullable=True),
    )
    op.add_column(
        "extractions",
        sa.Column("low_quality_reviewer", sa.String(64), nullable=True),
    )
    op.create_index(
        _REVIEW_INDEX_NAME,
        "extractions",
        ["low_quality_review_pending"],
    )


def downgrade() -> None:
    """Drop the index and reverse-order drop every column added in upgrade()."""
    op.drop_index(_REVIEW_INDEX_NAME, table_name="extractions")
    for col in (
        "low_quality_reviewer",
        "low_quality_review_verdict",
        "low_quality_reviewed_at",
        "low_quality_review_pending",
        "quality_dimension_scores",
        "quality_score",
    ):
        op.drop_column("extractions", col)

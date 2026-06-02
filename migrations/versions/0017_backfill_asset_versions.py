"""Backfill asset_versions from existing extractions.dtcg_json payloads.

Revision ID: 0017_backfill_asset_versions
Revises: 0016_extractions_asset_version_fk
Create Date: 2026-06-02

schema_version: backfill_v1 (no schema change; data-only migration).

Motivation
----------
0015 added ``asset_versions`` and 0016 added ``extractions.asset_version_id``
NULL. This migration walks every ``extractions`` row carrying a non-null
``dtcg_json`` and:

1. Computes ``content_hash = sha256(canonical_json(dtcg_json))`` where
   ``canonical_json`` matches ``app/asset_versions.py:canonicalize_dtcg``
   (sort_keys=True, separators=(",", ":"), ensure_ascii=False).
2. Looks up an existing ``asset_versions`` row by ``(url, content_hash)``.
   If present, reuses its id. If absent, inserts a fresh row.
3. Sets ``extractions.asset_version_id`` to that id.

Idempotency
-----------
Re-running this migration is a no-op for any row that already has
``asset_version_id`` populated. The dedup short-circuit on
``(url, content_hash)`` prevents duplicate asset_versions inserts on re-run.
Safe to run more than once (e.g. a prod redeploy that re-issues
``alembic upgrade head``).

Provenance fields on backfilled asset_versions rows
---------------------------------------------------
- ``first_extracted_by_user_id`` <- ``extractions.user_id``
- ``fetched_at`` <- ``extractions.extracted_at`` (best available; matches the
  original snapshot time, not the migration run time)
- ``is_public`` <- False (v1.1 corpus-visibility default; flipping to True is
  a v1.2 moderation step)
- ``version_label`` <- NULL (human label is optional and not derivable)
- ``manifest_schema_version`` <- ``extractions.schema_version`` (best
  available; the column on ``asset_versions`` defaults to 2 going forward but
  historical rows preserve their original schema_version)
- ``raw_assets_url`` <- NULL (extractions.r2_zip_key stays on extractions; the
  asset_versions raw_assets_url field is reserved for future per-snapshot
  ZIP pointers that may diverge from the per-extraction ZIP key)
"""
from __future__ import annotations

import hashlib
import json
import logging

import sqlalchemy as sa
from alembic import op


revision = "0017_backfill_asset_versions"
down_revision = "0016_extractions_asset_version_fk"
branch_labels = None
depends_on = None


logger = logging.getLogger("alembic.runtime.migration.0017_backfill_asset_versions")


def _canonicalize(dtcg: dict) -> bytes:
    """Return the canonical JSON byte serialization used for content hashing.

    Must match ``app/asset_versions.py:canonicalize_dtcg`` so future code
    paths compute the same hash for the same payload.
    """
    return json.dumps(dtcg, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _hash_for(dtcg: dict) -> str:
    """Return the SHA-256 hex digest of the canonical DTCG bytes."""
    return hashlib.sha256(_canonicalize(dtcg)).hexdigest()


def upgrade() -> None:
    """Walk extractions, insert-or-reuse asset_versions, set the FK.

    Logs counts at INFO so a prod deploy log shows backfill results without
    pulling the operator into a manual query.
    """
    bind = op.get_bind()
    extractions = sa.Table("extractions", sa.MetaData(), autoload_with=bind)
    asset_versions = sa.Table("asset_versions", sa.MetaData(), autoload_with=bind)

    select_stmt = sa.select(
        extractions.c.id,
        extractions.c.user_id,
        extractions.c.url,
        extractions.c.dtcg_json,
        extractions.c.extracted_at,
        extractions.c.schema_version,
        extractions.c.asset_version_id,
    ).where(
        extractions.c.dtcg_json.isnot(None),
        extractions.c.asset_version_id.is_(None),
    )

    inserted = 0
    reused = 0
    skipped_no_payload = 0
    for row in bind.execute(select_stmt).mappings().all():
        payload = row["dtcg_json"]
        if not isinstance(payload, dict) or not payload:
            skipped_no_payload += 1
            continue
        content_hash = _hash_for(payload)
        existing_id = bind.execute(
            sa.select(asset_versions.c.id).where(
                asset_versions.c.url == row["url"],
                asset_versions.c.content_hash == content_hash,
            )
        ).scalar()
        if existing_id is None:
            result = bind.execute(
                asset_versions.insert().values(
                    url=row["url"],
                    content_hash=content_hash,
                    dtcg_json=payload,
                    raw_assets_url=None,
                    manifest_schema_version=int(row["schema_version"] or 2),
                    fetched_at=row["extracted_at"],
                    first_extracted_by_user_id=row["user_id"],
                    is_public=False,
                    version_label=None,
                )
            )
            existing_id = result.inserted_primary_key[0]
            inserted += 1
        else:
            reused += 1
        bind.execute(
            extractions.update()
            .where(extractions.c.id == row["id"])
            .values(asset_version_id=existing_id)
        )

    logger.info(
        "asset_versions backfill complete: inserted=%d reused=%d skipped_no_payload=%d",
        inserted,
        reused,
        skipped_no_payload,
    )


def downgrade() -> None:
    """Reset every extractions.asset_version_id to NULL; drop backfilled rows.

    Partial-undo only: rows in asset_versions that point at organic-write
    flow (post-0017 deploy) cannot be safely distinguished from backfilled
    rows in this migration alone. We delete every asset_versions row whose
    content_hash matches a hash recomputed from an extractions.dtcg_json
    that still carries the same payload. The dtcg_json column survives until
    0018, so the backfill is reversible until 0018 ships.
    """
    bind = op.get_bind()
    extractions = sa.Table("extractions", sa.MetaData(), autoload_with=bind)
    asset_versions = sa.Table("asset_versions", sa.MetaData(), autoload_with=bind)

    # Collect FK ids to free, then null them out on extractions.
    referenced_ids = [
        row[0]
        for row in bind.execute(
            sa.select(extractions.c.asset_version_id).where(
                extractions.c.asset_version_id.isnot(None)
            )
        ).all()
    ]
    bind.execute(extractions.update().values(asset_version_id=None))
    if referenced_ids:
        bind.execute(
            asset_versions.delete().where(asset_versions.c.id.in_(set(referenced_ids)))
        )

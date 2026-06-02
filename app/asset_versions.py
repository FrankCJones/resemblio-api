"""Helpers for the asset_versions library table.

Single source of truth for two operations any caller needs:

1. ``canonicalize_dtcg`` / ``content_hash_for`` -- byte-stable serialization
   of a DTCG payload and its SHA-256 digest. Used by the extraction-creation
   route, the backfill migration (0017), and any future library-hit lookup.
   The hash MUST match across all three callers or dedup breaks silently.

2. ``insert_or_reuse_asset_version`` -- given a DB session + (url, dtcg) plus
   provenance fields, return the ``AssetVersion`` row that owns that content
   (insert a fresh row if none exists for ``(url, content_hash)``, otherwise
   reuse the existing one). Idempotent.

3. ``dtcg_for_extraction`` -- read-path adapter. Returns the joined
   ``asset_version.dtcg_json`` payload, or None when the extraction has no
   asset_version FK (failed or pre-0017 unbackfilled rows). Migration 0018
   dropped the legacy ``extractions.dtcg_json`` column; the helper is kept
   as the single read entry point so callers do not depend on the relation
   shape directly.
"""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app.models import AssetVersion, Extraction


def canonicalize_dtcg(dtcg: dict[str, Any]) -> bytes:
    """Return the canonical JSON byte serialization of a DTCG payload.

    Contract: ``sort_keys=True``, ``separators=(",", ":")``,
    ``ensure_ascii=False``. Two payloads that hash to the same value MUST
    serialize identically here; callers must not pre-format the dict in any
    way that changes key ordering or whitespace.

    Edge case: ``ensure_ascii=False`` is intentional so non-ASCII glyphs
    (Spanish, accent marks) in a DTCG token name or value do not change the
    hash when ``json.dumps`` defaults shift. The hash itself is the SHA-256
    of these UTF-8 bytes so the wire encoding is fully specified.
    """
    return json.dumps(dtcg, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def content_hash_for(dtcg: dict[str, Any]) -> str:
    """Return the SHA-256 hex digest used as the dedup key on asset_versions."""
    return hashlib.sha256(canonicalize_dtcg(dtcg)).hexdigest()


def insert_or_reuse_asset_version(
    session: Session,
    *,
    url: str,
    dtcg: dict[str, Any],
    first_extracted_by_user_id: int | None,
    manifest_schema_version: int,
    raw_assets_url: str | None = None,
    is_public: bool = False,
    version_label: str | None = None,
) -> "AssetVersion":
    """Return the ``AssetVersion`` that owns ``(url, hash(dtcg))``.

    Inserts a new row when no row exists for that ``(url, content_hash)``;
    otherwise returns the existing row unchanged (no audit fields rewritten
    -- the first writer wins on ``first_extracted_by_user_id``,
    ``is_public``, and ``version_label``).

    The caller is responsible for committing the surrounding transaction;
    this helper flushes so the returned ``AssetVersion`` has a stable
    primary key but does not commit.

    Parameters
    ----------
    is_public
        Tier-aware public-corpus visibility flag. Defaults to False to match
        the organic extraction-creation path (v1.1 keeps every organic row
        private until v1.2 moderation tooling exists). The DRL bulk-seed
        path overrides this to True so the library indexer can pick up the
        bootstrap corpus on its first run. See migration 0015 contract for
        the partial index that powers public-browse queries.
    version_label
        Human label for the snapshot (e.g. ``"DRL bootstrap 2026-05-21"``).
        NULL for organic rows; the DRL seed populates this from each
        ``_extractions/<system>/extraction.json:captured`` date so the
        library timeline view can sort + group bootstrap rows distinctly
        from organic re-extractions.

    Edge case: a concurrent writer racing on the same ``(url, content_hash)``
    pair will either lose the insert and pick up the winner's row on a
    retry, or commit a duplicate row if the application is deployed without
    a UNIQUE constraint on the dedup key. v1.1 does NOT add the UNIQUE
    constraint (the dedup is best-effort and double-rows are not a
    correctness bug: extractions just point at one of the duplicates). v1.2
    can tighten this by adding a partial UNIQUE index.
    """
    from app.models import AssetVersion  # local import: avoid cycle at module load

    digest = content_hash_for(dtcg)
    existing = session.execute(
        select(AssetVersion).where(
            AssetVersion.url == url,
            AssetVersion.content_hash == digest,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    row = AssetVersion(
        url=url,
        content_hash=digest,
        dtcg_json=dtcg,
        raw_assets_url=raw_assets_url,
        manifest_schema_version=manifest_schema_version,
        first_extracted_by_user_id=first_extracted_by_user_id,
        is_public=is_public,
        version_label=version_label,
    )
    session.add(row)
    session.flush()
    return row


def dtcg_for_extraction(extraction: "Extraction") -> dict[str, Any] | None:
    """Return the DTCG payload for an extraction from the joined asset_versions row.

    Returns ``None`` when the extraction has no ``asset_version_id``
    (failed extractions, or historical rows that pre-date the 0017 backfill
    and never had a DTCG payload to migrate). Migration 0018 dropped the
    legacy denormalized ``extractions.dtcg_json`` column; this helper is the
    single read entry point so callers do not depend on the relation shape.
    """
    av = extraction.asset_version
    if av is not None:
        return av.dtcg_json
    return None

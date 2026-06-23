"""Helpers for the asset_versions library table.

Single source of truth for the following operations:

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

4. ``AssetComponentSpec`` / ``insert_asset_component`` -- typed write path
   for brand-stripped DRL component code (markup + CSS). One row per
   (asset_version_id, fragment_key) in asset_components. Added in issue #1
   as the storage foundation the seed (#2) and indexer (#3) build on.

5. ``get_asset_component`` -- read-path counterpart to ``insert_asset_component``.
   Returns the ``AssetComponent`` row for a given (asset_version_id, fragment_key),
   or None when absent. Used by the library indexer (#3) to decide whether to
   serve the stored real DRL component or fall back to the generic template path.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app.models import AssetComponent, AssetVersion, Extraction


# Fixed schema version tag written to every asset_components row.
# Increment (e.g. 'asset_component_v2') if the column contract changes
# in a way that requires distinguishing old rows from new.
_ASSET_COMPONENT_SCHEMA_VERSION = "asset_component_v1"


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


# ---------------------------------------------------------------------------
# AssetComponent write path (issue #1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssetComponentSpec:
    """Typed write-path shape for a single component fragment.

    Frozen so callers cannot mutate a spec after constructing it.
    All fields are required; see ``insert_asset_component`` for how they
    map onto an ``AssetComponent`` row.

    Fields
    ------
    fragment_key
        Slot name within the asset. Use ``'default'`` for the primary
        fragment. Reserved values for future use: ``'inverse'``, ``'dark'``,
        ``'compact'``. Any string is accepted; the unique index on
        ``(asset_version_id, fragment_key)`` enforces uniqueness at DB level.
    component_html
        Brand-stripped component markup from the DRL ``asset.html``. Must
        include all states that ``states_present`` declares.
    component_css
        Brand-stripped component CSS (component-scoped rules, not the
        ``:root`` token block). Sourced from DRL ``tokens.css`` component
        section or the inline ``<style>`` block in ``asset.html``.
    source_asset_path
        DRL provenance path relative to the DRL root, e.g.
        ``'assets/atoms/buttons/a24-cinematic-001'``. Never an absolute
        OS path; the DRL root is not part of this string.
    states_present
        List of UI state names the markup demonstrates, e.g.
        ``["rest", "hover", "focus", "disabled"]``. Used by the indexer
        to annotate which interaction states are available.
    head_html
        Raw ``<link rel="stylesheet">`` tags extracted from the DRL
        ``asset.html`` ``<head>`` (Google Fonts CDN only; preconnect and
        local resource links excluded). Defaults to empty string for assets
        with no Google Fonts dependency or rows seeded before migration 0024.
        ``_compose_real_component`` uses this when non-empty so the candidate
        page loads the same fonts as the DRL reference (Issue #38).
    """

    fragment_key: str
    component_html: str
    component_css: str
    source_asset_path: str
    states_present: list[str]
    head_html: str = ""


def insert_asset_component(
    session: Session,
    asset_version_id: int,
    spec: AssetComponentSpec,
) -> "AssetComponent":
    """Persist a component fragment, upserting on (asset_version_id, fragment_key).

    Idempotent: if a row already exists for ``(asset_version_id, fragment_key)``,
    the mutable code fields are updated in place to reflect the latest input.
    A different ``fragment_key`` creates a distinct row under the same
    ``asset_version_id``.

    The caller is responsible for committing the surrounding transaction;
    this helper flushes so the returned ``AssetComponent`` has a stable PK.

    Edge cases
    ----------
    - Concurrent writers racing on the same ``(asset_version_id, fragment_key)``
      may collide on the unique constraint. The DB will raise ``IntegrityError``
      on the second insert; the caller should retry or use a row-level lock if
      high contention is expected. The seed pipeline is single-process today so
      this is not a current concern.
    - ``states_present`` is not validated against ``component_html``; the caller
      is responsible for accuracy. The field is informational metadata for the
      indexer, not a structural constraint.

    Parameters
    ----------
    session
        Active SQLAlchemy session.
    asset_version_id
        FK to ``asset_versions.id``; the referenced row must exist before calling.
    spec
        Typed spec carrying the component code, provenance, and state list.
    """
    from app.models import AssetComponent  # local import: avoid circular at module load

    existing = session.execute(
        select(AssetComponent).where(
            AssetComponent.asset_version_id == asset_version_id,
            AssetComponent.fragment_key == spec.fragment_key,
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.component_html = spec.component_html
        existing.component_css = spec.component_css
        existing.source_asset_path = spec.source_asset_path
        existing.states_present = spec.states_present
        existing.head_html = spec.head_html
        session.flush()
        return existing

    row = AssetComponent(
        asset_version_id=asset_version_id,
        fragment_key=spec.fragment_key,
        component_html=spec.component_html,
        component_css=spec.component_css,
        source_asset_path=spec.source_asset_path,
        states_present=spec.states_present,
        head_html=spec.head_html,
        schema_version=_ASSET_COMPONENT_SCHEMA_VERSION,
    )
    session.add(row)
    session.flush()
    return row


def get_asset_component(
    session: Session,
    asset_version_id: int,
    fragment_key: str = "default",
) -> "AssetComponent | None":
    """Return the AssetComponent row for (asset_version_id, fragment_key), or None if absent.

    This is the read-path counterpart to ``insert_asset_component``. The library
    indexer calls it once per asset_version (before the per-class render loop) to
    decide whether to serve the stored real DRL component for the matching class page
    or fall back to the generic token-tinted template path.

    Edge cases
    ----------
    - Returns None for any asset_version_id that has no asset_components rows at all.
    - Returns None when the row exists but under a different fragment_key. Currently
      only ``'default'`` is seeded; future variants (``'inverse'``, ``'dark'``) each
      require their own call with the appropriate key.
    - The function does NOT raise on missing rows; the caller is responsible for
      deciding what "absent" means in context (indexer: honest empty body).

    Parameters
    ----------
    session
        Active SQLAlchemy session.
    asset_version_id
        FK to ``asset_versions.id``; the row need not exist - None is returned cleanly.
    fragment_key
        Slot name within the asset. Defaults to ``'default'`` (the primary fragment).
    """
    from app.models import AssetComponent  # local import: avoid circular at module load

    return session.execute(
        select(AssetComponent).where(
            AssetComponent.asset_version_id == asset_version_id,
            AssetComponent.fragment_key == fragment_key,
        )
    ).scalar_one_or_none()

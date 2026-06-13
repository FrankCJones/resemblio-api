"""Curation metadata-overlay seam for DRL-seeded brands.

Some DRL-curated taxonomy is mis-tagged for a small set of brands. The clearest
case is Apple, tagged ``category: consumer-dtc`` with ``applicable_to`` including
``saas-marketing`` - neither right for a premium consumer-product-marketing site.

The DRL tree (``projects/Design Reference Library/``) is **read-only** from the
Resemblio project (Resemblio ``CLAUDE.md`` forbidden actions). So rather than
edit the source taxonomy, this module applies a Resemblio-owned, schema-versioned
overlay on top of each brand-stripped entry at seed time, keyed by brand slug and
bounded to an explicit allowlist (the keys of ``metadata_overrides.json``).

This is Frank's **Option B** decision (2026-06-12). The nuance vs decision D20
("not a Resemblio write-back of derived state"): this corrects *mislabeled
curated taxonomy at seed time*; it does **not** persist Resemblio-*derived*
render output upstream. The DRL tree is never written. Audit + rationale:
``projects/Resemblio/02-prd/2026-06-12-library-v5-phase2-metadata.md``.

Pure, deterministic, no network. Applied in ``scripts/seed_from_drl.py`` right
after ``brand_strip`` produces a ``StrippedEntry``, so the corrected values flow
through ``build_bundle`` into ``dtcg_json`` and reach prod only at the gated
Phase 4 re-seed (per D17 - no prod mutation happens at seed-module import time).
"""
from __future__ import annotations

import dataclasses
import json
from functools import lru_cache
from pathlib import Path
from typing import TypedDict

from transformer import StrippedEntry

# --- Named constants (workspace quality floor) -------------------------------

METADATA_OVERRIDES_SCHEMA_VERSION = 1
"""Schema version of ``metadata_overrides.json``. Bump on any breaking change to
the override record shape; ``load_metadata_overrides`` rejects other versions."""

DEFAULT_OVERRIDES_PATH: Path = (
    Path(__file__).resolve().parent / "metadata_overrides.json"
)
"""Sibling JSON carrying the bounded per-brand correction allowlist."""


class BrandMetadataOverride(TypedDict, total=False):
    """One brand's correction record inside ``metadata_overrides.json``.

    All fields optional; an override may set the category, prune mis-tagged
    ``applicable_to`` tokens, add the correct ones, or any combination.

    Fields:
        category: Replacement ``category`` token (set verbatim when present).
        applicable_to_remove: Tokens to strip from ``applicable_to``.
        applicable_to_add: Tokens to append to ``applicable_to`` (deduped,
            order-preserving) when not already present.
        reason: Human-readable justification (audit trail; not applied).
    """

    category: str
    applicable_to_remove: list[str]
    applicable_to_add: list[str]
    reason: str


@lru_cache(maxsize=4)
def load_metadata_overrides(
    path: Path | None = None,
) -> dict[str, BrandMetadataOverride]:
    """Load and validate the curation overlay file.

    Args:
        path: Override file location; defaults to ``DEFAULT_OVERRIDES_PATH``.

    Returns:
        Mapping ``brand_slug -> BrandMetadataOverride`` (the bounded allowlist).
        Returns an empty mapping when the file is absent (the overlay is then a
        no-op, which is the safe default).

    Raises:
        ValueError: when the file's ``schema_version`` is not
            ``METADATA_OVERRIDES_SCHEMA_VERSION`` (forward-compat guard, so a
            newer-shaped file is never silently mis-applied).
    """
    resolved = path or DEFAULT_OVERRIDES_PATH
    if not resolved.exists():
        return {}
    raw = json.loads(resolved.read_text(encoding="utf-8"))
    version = raw.get("schema_version")
    if version != METADATA_OVERRIDES_SCHEMA_VERSION:
        raise ValueError(
            f"metadata_overrides schema_version {version!r} unsupported; "
            f"expected {METADATA_OVERRIDES_SCHEMA_VERSION}"
        )
    overrides = raw.get("overrides") or {}
    # Drop JSON sidecar keys (e.g. "_about") defensively; only real slugs map to
    # dict records.
    return {
        slug: rec
        for slug, rec in overrides.items()
        if isinstance(rec, dict)
    }


def _apply_token_edits(
    current: tuple[str, ...],
    *,
    remove: list[str],
    add: list[str],
) -> tuple[str, ...]:
    """Return ``current`` with ``remove`` tokens dropped and ``add`` appended.

    Order-preserving and idempotent: an ``add`` token already present (after the
    removals) is not duplicated. Used for both ``applicable_to`` and the
    denormalized ``tags`` list so the two stay consistent after a correction.
    """
    remove_set = set(remove)
    kept = [t for t in current if t not in remove_set]
    for token in add:
        if token not in kept:
            kept.append(token)
    return tuple(kept)


def brand_slug_of(stripped: StrippedEntry) -> str:
    """Return the brand slug for a stripped entry.

    The brand is the first segment of ``source_id`` (``"apple/layout/apple-
    marketing-page-001" -> "apple"``). ``stripped.slug`` is the *asset* slug,
    which differs per asset (only the alphabet/library assets share the brand
    slug), so it is the wrong key for a brand-level overlay.
    """
    return stripped.source_id.split("/", 1)[0]


def apply_metadata_overrides(
    stripped: StrippedEntry,
    *,
    brand_slug: str | None = None,
    overrides: dict[str, BrandMetadataOverride] | None = None,
) -> StrippedEntry:
    """Apply the curation overlay to one brand-stripped entry.

    Keyed on the brand slug (``brand_slug`` when given, else derived from
    ``source_id`` via :func:`brand_slug_of`). Keying on the brand - not the
    per-asset ``stripped.slug`` - is what makes the correction consistent across
    *every* asset of a brand, not just its canonical alphabet specimen. A brand
    outside the bounded allowlist is returned **unchanged** (identical object
    semantics: ``apply_metadata_overrides(e) == e``). The operation is idempotent.

    Args:
        stripped: Brand-stripped DRL entry (frozen dataclass).
        brand_slug: Explicit brand/system slug; derived from ``source_id`` when
            omitted.
        overrides: Allowlist mapping; defaults to the shipped overlay file.

    Returns:
        A corrected ``StrippedEntry`` (via ``dataclasses.replace``) for an
        allowlisted brand, otherwise the input unchanged.
    """
    table = load_metadata_overrides() if overrides is None else overrides
    key = brand_slug if brand_slug is not None else brand_slug_of(stripped)
    override = table.get(key)
    if override is None:
        return stripped

    changes: dict[str, object] = {}

    category = override.get("category")
    if category is not None and category != stripped.category:
        changes["category"] = category

    remove = override.get("applicable_to_remove") or []
    add = override.get("applicable_to_add") or []
    if remove or add:
        new_applicable = _apply_token_edits(
            stripped.applicable_to, remove=remove, add=add
        )
        if new_applicable != stripped.applicable_to:
            changes["applicable_to"] = new_applicable

        # ``tags`` is a denormalized search/discovery list that also carries the
        # applicable_to tokens (brand_strip flattens kind + mood + applicable_to
        # + patterns into it). Reconcile it with the same edits, or the corrected
        # brand still ships the wrong token in its bundle/tokens.json artifact -
        # an inconsistency a customer inspecting the download would see.
        new_tags = _apply_token_edits(stripped.tags, remove=remove, add=add)
        if new_tags != stripped.tags:
            changes["tags"] = new_tags

    if not changes:
        return stripped
    return dataclasses.replace(stripped, **changes)

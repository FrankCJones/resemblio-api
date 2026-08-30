"""Public-corpus library read API (mission Phase 4 follow-on).

These endpoints back the Next.js `/library/...` route tree in
``code/web/app/app/lib/library-data.ts``. The web BFF flips
``RESEMBLIO_LIBRARY_DATA_SOURCE=api`` to switch from its mock fixtures
to live data sourced from these endpoints.

Surface
-------
- ``GET /v1/library/brands`` - paginated hub list of brands
- ``GET /v1/library/brands/{brand_slug}`` - canonical brand page
- ``GET /v1/library/brands/{brand_slug}/versions/{version_label}`` - snapshot
- ``GET /v1/library/brands/{brand_slug}/categories/{category_slug}`` - cross-time category
- ``GET /v1/library/brands/{brand_slug}/categories/{category_slug}/{version_label}``
- ``GET /v1/library/brands/{brand_slug}/categories/{category_slug}/{version_label}/{asset_id}``
- ``GET /v1/library/sitemap`` - flat sitemap row list

Quality gate
------------
Every row returned MUST have passed the Phase 4 indexer's quality gate
(mission D2). The indexer only writes ``library_pages`` rows for
``asset_versions`` where ``is_public=True`` AND the joined extraction's
``quality_score >= LIBRARY_INDEX_QUALITY_THRESHOLD`` AND no penalty flags
fired. Because the indexer is the only writer to ``library_pages``, a
simple ``SELECT`` from that table is by-construction quality-gated. The
route still re-enforces the ``asset_versions.is_public`` flag at read time
in case the moderation tooling flips a row from public to private after
indexing without deleting the downstream rows.

Response envelope
-----------------
Top-level ``schema_version=SCHEMA_V1_1`` (=2) consistent with the rest of
the API. The payload body carries its own contract-level tag
``data.schema_version='library_data_v1'`` matching the locked TypeScript
contract in ``library-data.ts > LIBRARY_DATA_SCHEMA_VERSION``.

Auth
----
Library content is public-by-construction (the moderation gate is upstream,
on ``asset_versions.is_public``). These routes are added to
``AUTH_FREE_PATHS`` in ``app/auth.py`` so the BFF and search-engine crawlers
can both read them without credentials. Quality-filtered rows alone are ever
exposed; no PII, no per-user data, no Stripe surface.

Caching
-------
``Cache-Control`` is set per endpoint with long TTLs on canonical brand /
asset-detail (1 hour public, 24 hour stale-while-revalidate) and shorter on
the sitemap (10 min). The indexer runs every 60s; clients revalidating on
the order of minutes will pick up new pages within one cache window.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Literal, TypedDict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app.brand_names import pretty_brand_name
from app.constants import SCHEMA_V1_1
from app.db import get_db
from app.library_category_aliases import (
    canonical_public_category_slug,
    category_lookup_slugs,
)
from app.missing_data_notice import hub_capture_signal_from_captured_groups
from app.library_token_exports import LibraryTokenPayload, build_library_token_payload
from app.models import AssetVersion, LibraryPage

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Contract constants (mirror the locked TypeScript contract)
# ---------------------------------------------------------------------------

# Matches ``LIBRARY_DATA_SCHEMA_VERSION`` in ``library-data.ts``. Bumping here
# requires a coordinated bump on the web contract; do not bump silently.
LIBRARY_DATA_SCHEMA_VERSION = "library_data_v1"

# Pagination defaults for the hub list. Mirrors the FastAPI-wide convention
# (extractions list endpoint) so a single client paginator works across both.
DEFAULT_HUB_PAGE_SIZE = 25
MAX_HUB_PAGE_SIZE = 100

# Cache-Control values per endpoint.  Long TTL for the heavy, mostly-static
# pages; shorter for the sitemap so newly-indexed pages appear promptly.
CACHE_PAGE = "public, max-age=3600, stale-while-revalidate=86400"
CACHE_HUB = "public, max-age=600, stale-while-revalidate=3600"
CACHE_SITEMAP = "public, max-age=600, stale-while-revalidate=3600"

# Validation regexes mirroring the web contract (library-data.ts).
# Version label is intentionally a generic slug-shape (not YYYY-MM): the
# indexer slugifies free-form labels (e.g. "DRL bootstrap 2026-05-21" ->
# "drl-bootstrap-2026-05-21") so the URL form is arbitrary slug-shape.
# Same posture as the web validator: shape-check at the edge, let the DB
# 404 decide membership.
_BRAND_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_CATEGORY_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?$")
_VERSION_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?$")
_ASSET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$", re.IGNORECASE)

RESERVED_BRAND_SLUGS = frozenset(
    {"app", "api", "about", "pricing", "contact", "library", "docs", "blog",
     "privacy", "terms", "admin", "_next"}
)

# Hub-card palette config (added 2026-06-03 for BLOCKER 3).
# Source slots inside ``library_pages.metadata_json``, in priority order.
# Index 0 is the canonical accent / primary (web contract `library-data.ts`
# line 116). Cap at 5 hex strings to match the BrandCard swatch row.
_HUB_PALETTE_SLOTS: tuple[str, ...] = ("accent", "bg", "surface", "text")
_HUB_PALETTE_MAX = 5
_HEX_COLOR_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_PUBLIC_COMPONENT_MARKER_RE = re.compile(r"\bdata-rs-source=[\"']drl-component[\"']", re.I)
_INTERNAL_PROVENANCE_RE = re.compile(
    r"(?:resemblio://|drl-bootstrap|drl-mined-from|drl-rebuild|urn:)",
    re.I,
)
PublicReadinessStatus = Literal["ready", "hold_no_marker", "fix_leak", "unknown"]


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------


class RelatedItem(TypedDict):
    """One related cross-link row matching the web contract."""

    label: str
    href: str


class LibraryPageData(TypedDict, total=False):
    """Single library-page payload mirroring ``LibraryPageData`` in TS."""

    schema_version: str
    brand_slug: str
    source_url: str
    category_slug: str | None
    category_label: str | None
    category_kind: str | None
    version_label: str | None
    asset_id: str | None
    is_canonical: bool
    is_version_snapshot: bool
    rendered_html: str
    related: list[RelatedItem]
    updated_at: str
    public_readiness_status: PublicReadinessStatus
    is_public_indexable: bool
    is_exportable: bool
    library_token_export: LibraryTokenPayload
    # Library v2 D3 acknowledgment fields (Phase 4, 2026-06-07).
    # Sourced from metadata_json.missing_data_notice and
    # metadata_json.capture_manifest written by the indexer.
    # Always present in responses; empty list when all showcase categories are
    # captured or no capture metadata has been written yet for this page.
    missing_groups: list[str]
    captured_groups: list[str]
    # Curated metadata (2026-06-08 - DRL reconciliation Gap C fix).
    # Sourced from asset_versions.dtcg_json, written by the seeder after
    # reading corpus.json (tier, category) and systems/<slug>/system.json
    # (design_principles, commercial_signal); mood + applicable_to are curated
    # design-behaviour tags always written by build_bundle. Fields are absent
    # (key not present) when the seed row predates the seeder or the asset is
    # an organic extraction. Web consumers must treat a missing key as None.
    # Phase 3 added tier/category/design_principles/commercial_signal; Phase 4
    # added mood/applicable_to to complete Frank's locked D-C set.
    tier: str | None
    category: str | None
    design_principles: list[str] | None
    commercial_signal: str | None
    mood: list[str] | None
    applicable_to: list[str] | None


class HubFeaturedRow(TypedDict, total=False):
    """One row of the hub list.

    Required fields: ``brand_slug``, ``source_url``, ``category_count``.
    Optional fields (added 2026-06-03 for the BLOCKER 3 hub card UI):

    - ``palette``: up to 5 deduplicated hex strings (lowercase, with leading
      ``#``) sourced from the brand's representative ``library_pages``
      row's ``metadata_json`` color slots. Ordered by web-contract priority:
      accent first (canonical primary), then bg, surface, text. Empty list
      when the brand carries no real palette tokens.
    - ``display_font``: the ``font_display`` CSS family string from the same
      ``library_pages.metadata_json``. ``None`` when not present.

    Library v2 fields (Phase 4, 2026-06-07):

    - ``captured_count``: number of primary showcase component groups with
      real captured (native or mined) data (0-5). As of issue #11 this is a
      CROSS-PAGE UNION across all of the brand's public asset_versions (see
      the issue #11 note below), NOT a single-page read.
    - ``total_showcase_groups``: total number of primary showcase component
      groups (5 in the current corpus). Computed by the count rule, so it is
      5 for any brand that has at least one public page (even a pre-v2 page
      with no capture data: "0 of 5", never "0 of 0").

    Old web clients ignore additive fields; new ones consume them with a
    fallback. No envelope or data schema_version bump required (web contract
    stays at ``library_data_v1``).

    Issue #11 (2026-06-20): ``captured_count`` is a CROSS-PAGE UNION, not a
    single-page read. ``_hub_meta_for_brand`` unions ``capture_manifest.groups``
    across ALL public asset_versions for the brand and calls
    ``hub_capture_signal_from_captured_groups`` (the single source of truth for
    the count rule). This ensures brands with multiple mined atom classes (one
    asset_version each) show the honest total rather than always "1 of 5". The
    per-page ``metadata_json.hub_capture_signal`` is still written by the
    indexer but is no longer the read source for this count.

    KNOWN GAP (Library v3 D8, documented 2026-06-08): the web hub's
    ``visibleHubCategories`` (``app/app/lib/library-categories.ts``) consumes a
    per-row ``captured_groups`` LIST to decide which showcase CHIPS to reveal.
    This hub row deliberately does NOT yet emit ``captured_groups`` - only the
    coarse ``captured_count``. The showcase-chip REVEAL is dormant-by-design
    until wired here. When wiring it, two things are required: (1) build the
    CROSS-BRAND union of captured group names (already computed by
    ``_hub_meta_for_brand`` as ``union_captured``; expose it here); AND (2) map
    manifest GROUP names (singular: ``button``, ``card``) to the showcase
    category SLUGS the web matches on (plural: ``buttons``, ``cards``). A raw
    copy of the per-page field would NOT work - ``"button" != "buttons"`` - so
    the reveal would silently never fire. ``captured_count`` alone is
    insufficient; the web needs to know WHICH groups, not how many.
    """

    brand_slug: str
    source_url: str
    category_count: int
    palette: list[str]
    display_font: str | None
    captured_count: int
    total_showcase_groups: int


class LibraryHubData(TypedDict):
    """Hub payload mirroring ``LibraryHubData`` in TS, plus pagination."""

    schema_version: str
    featured: list[HubFeaturedRow]
    page: int
    page_size: int
    total: int


class LibrarySitemapEntry(TypedDict):
    """One row in the sitemap list."""

    path: str
    last_modified: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _envelope(data: dict[str, Any]) -> dict[str, Any]:
    """Wrap a payload in the standard top-level response envelope."""
    return {"schema_version": SCHEMA_V1_1, "data": data}


def _json(data: dict[str, Any], *, cache: str, status_code: int = 200) -> JSONResponse:
    """Return a JSONResponse with the standard cache header attached."""
    return JSONResponse(status_code=status_code, content=_envelope(data),
                        headers={"Cache-Control": cache})


def _title_case(slug: str) -> str:
    """Title-case a kebab slug for display.

    Used for category labels (``buttons`` -> "Buttons") where simple
    capitalization is correct. For BRAND labels callers should use
    ``_brand_display`` instead; this function preserves the naive shape
    so category display copy stays stable.
    """
    return " ".join(p.capitalize() for p in slug.split("-") if p)


def _brand_display(brand_slug: str) -> str:
    """Return the brand's canonical display name (L-7 fix, Phase B 2026-06-03).

    Routes brand slugs through ``app.brand_names.pretty_brand_name`` so
    "openai" renders as "OpenAI" rather than "Openai". Used by every
    chip-label, related-list, and page-frame builder that surfaces a
    brand slug to the public page. Unknown slugs fall through to the
    title-case humanize so an organic row never raises.
    """
    return pretty_brand_name(brand_slug)


# Patterns matching build-internal version-label slugs that must not
# surface as user-facing related chips. Locked 2026-06-03 per Phase B
# stage B3 (closes C-5): "Aeon design system in drl-bootstrap-2026-05-21"
# leaked into the public chip row because the indexer's slugified
# version_label is the chip label. The filter is regex-based so any
# future build-internal label shape ("drl-rebuild-..." / "ci-..." /
# explicit ISO-date-only labels) is caught at the same edge.
_INTERNAL_VERSION_LABEL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^drl-bootstrap"),
    re.compile(r"^drl-mined-from"),
    re.compile(r"^drl-rebuild"),
    re.compile(r"^ci-"),
    re.compile(r"^\d{4}-\d{2}-\d{2}$"),
)


def _is_internal_version_label(version_label: str) -> bool:
    """Return True for version labels that should never surface to users.

    Build-internal labels (DRL bootstrap stamps, CI seed-rebuild stamps,
    raw ISO dates with no human prefix) read as developer artifacts when
    rendered in a related-chip row. Filtering at the chip-list edge
    keeps the version page itself reachable for any user that lands on
    its direct URL (the route still resolves) while removing it from
    the discoverability surface.
    """
    for pattern in _INTERNAL_VERSION_LABEL_PATTERNS:
        if pattern.search(version_label):
            return True
    return False


def _category_kind(slug: str) -> str:
    """Return the category-kind label the web contract expects."""
    return "tokens" if slug in {"palette", "typography"} else "components"


def _validate_brand_slug(brand_slug: str) -> None:
    """Reject invalid or reserved brand slugs with a 404."""
    if (brand_slug in RESERVED_BRAND_SLUGS) or not _BRAND_SLUG_RE.match(brand_slug):
        raise HTTPException(status_code=404, detail="brand_not_found")


def _validate_category_slug(category_slug: str) -> None:
    """Reject malformed category slugs with a 404."""
    if not _CATEGORY_SLUG_RE.match(category_slug):
        raise HTTPException(status_code=404, detail="category_not_found")


def _validate_version_label(version_label: str) -> None:
    """Reject malformed version labels (must be YYYY-MM) with a 404."""
    if not _VERSION_LABEL_RE.match(version_label):
        raise HTTPException(status_code=404, detail="version_not_found")


def _validate_asset_id(asset_id: str) -> None:
    """Reject malformed asset ids with a 404."""
    if not _ASSET_ID_RE.match(asset_id):
        raise HTTPException(status_code=404, detail="asset_not_found")


def _source_url_for_brand(session: Session, brand_slug: str) -> str | None:
    """Return the most recent ``asset_versions.url`` for a brand_slug.

    Picks the URL of the asset_version that owns the most recent library_page
    row for this brand. NULL when no rows exist for the brand.
    """
    stmt = (
        select(AssetVersion.url)
        .join(LibraryPage, LibraryPage.asset_version_id == AssetVersion.id)
        .where(LibraryPage.brand_slug == brand_slug)
        .where(AssetVersion.is_public.is_(True))
        .order_by(AssetVersion.fetched_at.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()



def _public_source_url(raw_source_url: str | None, brand_slug: str) -> str:
    """Return a public-safe ``source_url`` for library API responses.

    Real http(s) source URLs remain intact so the public surface can credit
    where the design came from. Internal seed URNs such as
    ``resemblio://seed/...`` are build provenance, not public attribution, so
    they collapse to the same display-safe fallback the web layer already uses.
    """
    if raw_source_url and re.match(r"^https?://", raw_source_url, re.IGNORECASE):
        return raw_source_url
    pretty = _brand_display(brand_slug)
    return f"https://{pretty}"

class _BrandHubMeta(TypedDict):
    """Aggregated brand metadata for hub card rendering (internal)."""

    palette: list[str]
    display_font: str | None
    captured_count: int
    total_showcase_groups: int


def _palette_from_metadata(metadata: dict[str, Any]) -> list[str]:
    """Extract the ordered hex palette from a library_pages metadata_json dict.

    Reads slots in ``_HUB_PALETTE_SLOTS`` order (accent first, per the web
    contract "index 0 is canonical accent / primary"). Hex strings are
    lowercased; duplicates are collapsed case-insensitively. Non-hex values
    (rgb(), named colors, garbage) are silently dropped. Result is capped at
    ``_HUB_PALETTE_MAX``.

    Args:
        metadata: a ``library_pages.metadata_json`` dict. Must be a real dict
            (caller is responsible for the isinstance check).

    Returns:
        List of lowercase hex color strings, length 0-``_HUB_PALETTE_MAX``.
    """
    palette: list[str] = []
    seen: set[str] = set()
    for slot in _HUB_PALETTE_SLOTS:
        if len(palette) >= _HUB_PALETTE_MAX:
            break
        raw = metadata.get(slot)
        if not isinstance(raw, str):
            continue
        candidate = raw.strip()
        if not _HEX_COLOR_RE.match(candidate):
            continue
        norm = candidate.lower()
        if norm in seen:
            continue
        seen.add(norm)
        palette.append(norm)
    return palette


def _hub_meta_for_brand(
    session: Session, brand_slug: str,
) -> _BrandHubMeta:
    """Return visual identity + honest capture count for a hub card.

    Queries ALL public ``library_pages`` rows for the brand in one pass,
    ordered by ``asset_versions.fetched_at DESC``:

    - **Palette and display_font** come from the representative row (the most
      recent public page), preserving the existing visual-identity contract.
    - **captured_count** is computed by unioning the captured component-group
      names across EVERY page's ``capture_manifest.groups``. This is required
      because post-issue-#5 each mined atom class is a DISTINCT asset_version
      (distinct ``synthetic_url`` per ``(brand, atom_class)``). A single-page
      read would return the per-page count (always 1 for a mined page), not
      the honest cross-page total.

    Count rule is delegated to ``hub_capture_signal_from_captured_groups``
    (``missing_data_notice.py``), the single source of truth for "N of 5."

    Palette ordering: ``_HUB_PALETTE_SLOTS`` (accent first). Hex strings are
    normalized to lowercase; duplicates collapsed case-insensitively. Non-hex
    values are silently dropped. Result capped at ``_HUB_PALETTE_MAX``.

    Defensive parsing: rows with absent or malformed ``capture_manifest``
    contribute zero captured groups and never raise. Pre-v2 rows (no
    ``capture_manifest`` key) degrade gracefully to zero contribution.

    Returns a ``_BrandHubMeta`` with safe defaults when no public
    ``library_pages`` row exists for the brand.
    """
    stmt = (
        select(LibraryPage.metadata_json)
        .join(AssetVersion, AssetVersion.id == LibraryPage.asset_version_id)
        .where(LibraryPage.brand_slug == brand_slug)
        .where(AssetVersion.is_public.is_(True))
        .order_by(AssetVersion.fetched_at.desc())
    )
    all_metadata: list[Any] = list(session.execute(stmt).scalars())

    if not all_metadata:
        return _BrandHubMeta(
            palette=[], display_font=None, captured_count=0, total_showcase_groups=0
        )

    # --- Palette and display font from the representative (most recent) row ---
    representative = all_metadata[0] if isinstance(all_metadata[0], dict) else {}
    palette = _palette_from_metadata(representative)
    font_raw = representative.get("font_display") if isinstance(representative, dict) else None
    display_font = font_raw if isinstance(font_raw, str) and font_raw.strip() else None

    # --- Union captured groups across ALL pages (issue #11) ---
    # Each mined-atom asset_version carries a capture_manifest for its own class.
    # Unioning ensures "buttons + cards + badges mined" produces 3, not 1.
    union_captured: set[str] = set()
    for metadata in all_metadata:
        if not isinstance(metadata, dict):
            continue
        manifest = metadata.get("capture_manifest")
        if not isinstance(manifest, dict):
            # Pre-v2 row: no capture_manifest -> contributes zero groups.
            continue
        groups = manifest.get("groups")
        if not isinstance(groups, dict):
            continue
        for group_name, detail in groups.items():
            if isinstance(detail, dict) and detail.get("captured") is True:
                union_captured.add(group_name)

    signal = hub_capture_signal_from_captured_groups(frozenset(union_captured))
    return _BrandHubMeta(
        palette=palette,
        display_font=display_font,
        captured_count=signal.captured_count,
        total_showcase_groups=signal.total_showcase_groups,
    )


def _related_for(session: Session, brand_slug: str,
                 *, exclude_category: str | None = None) -> list[RelatedItem]:
    """Build the cross-link related list for a page.

    Same shape the mock emits: sibling categories of this brand first
    (capped at 4), then each known version label for the brand.

    Hardening notes (2026-06-02): the prior implementation gated sibling
    categories on ``is_canonical=True``, which silently produced an empty
    list whenever the reconciler had not yet flipped canonical flags for
    a brand (e.g. seed-only corpora before the reconcile pass). We now
    fall back to the full set of categories the brand has any public page
    for, so the related block is always populated when data exists. The
    canonical contract is preserved on the page-detail endpoints; only the
    related-list lookup is relaxed. Every row is also filtered for non-empty
    string values defensively so a stray NULL never serialises as
    ``[None, None, ...]`` in the JSON response.
    """
    related: list[RelatedItem] = []

    # Sibling categories (cap 4). No is_canonical filter: any public page
    # for the brand contributes its category to the related set.
    cat_stmt = (
        select(LibraryPage.category_slug)
        .join(AssetVersion, AssetVersion.id == LibraryPage.asset_version_id)
        .where(LibraryPage.brand_slug == brand_slug)
        .where(AssetVersion.is_public.is_(True))
        .distinct()
    )
    cat_rows = session.execute(cat_stmt).all()
    cats = sorted({
        row[0] for row in cat_rows
        if row[0] and isinstance(row[0], str) and row[0] != exclude_category
    })
    for cat in cats[:4]:
        related.append(RelatedItem(
            label=f"{_title_case(cat)} from {_brand_display(brand_slug)}",
            href=f"/library/{brand_slug}/{cat}/",
        ))

    # Version labels for the brand (no cap; few in practice).
    ver_stmt = (
        select(LibraryPage.version_label)
        .join(AssetVersion, AssetVersion.id == LibraryPage.asset_version_id)
        .where(LibraryPage.brand_slug == brand_slug)
        .where(LibraryPage.version_label.is_not(None))
        .where(AssetVersion.is_public.is_(True))
        .distinct()
    )
    ver_rows = session.execute(ver_stmt).all()
    versions = sorted({
        row[0] for row in ver_rows
        if row[0] and isinstance(row[0], str)
    })
    # Filter build-internal version labels at the chip-row edge so a
    # "drl-bootstrap-2026-05-21" stamp does not surface as a user-facing
    # chip ("Aeon design system in drl-bootstrap-2026-05-21"). The
    # version page itself remains reachable for any user that types or
    # is linked the direct URL; only the discoverability surface is
    # filtered. See ``_is_internal_version_label`` for the pattern set.
    for v in versions:
        if _is_internal_version_label(v):
            continue
        related.append(RelatedItem(
            label=f"{_brand_display(brand_slug)} design system in {v}",
            href=f"/library/{brand_slug}/{v}/",
        ))
    return related


def _isoformat(dt: datetime | None) -> str:
    """Return an ISO-8601 timestamp; falls back to now when dt is NULL."""
    target = dt if dt is not None else datetime.now(timezone.utc)
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return target.isoformat()


def _extract_page_manifest_fields(
    metadata_json: Any,
) -> tuple[list[str], list[str]]:
    """Extract ``(missing_groups, captured_groups)`` from a page's metadata_json.

    ``missing_groups``: showcase category slugs that are NOT yet captured,
    sourced from ``metadata_json.missing_data_notice.missing_items``.

    ``captured_groups``: component group names that ARE captured, sourced
    from ``metadata_json.capture_manifest.groups``.

    Both return ``[]`` when the metadata is absent, malformed, or predates
    the Library v2 indexer that writes these fields.
    """
    meta = metadata_json if isinstance(metadata_json, dict) else {}

    # missing_groups from the missing_data_notice payload
    missing_groups: list[str] = []
    notice_raw = meta.get("missing_data_notice")
    if isinstance(notice_raw, dict):
        raw_items = notice_raw.get("missing_items")
        if isinstance(raw_items, list):
            missing_groups = [
                item["category_slug"]
                for item in raw_items
                if isinstance(item, dict) and isinstance(item.get("category_slug"), str)
            ]

    # captured_groups from the capture_manifest payload
    captured_groups: list[str] = []
    manifest_raw = meta.get("capture_manifest")
    if isinstance(manifest_raw, dict):
        groups_raw = manifest_raw.get("groups")
        if isinstance(groups_raw, dict):
            captured_groups = sorted(
                name
                for name, detail in groups_raw.items()
                if isinstance(detail, dict) and detail.get("captured") is True
            )

    return missing_groups, captured_groups


def _public_readiness_status(
    *,
    category_slug: str | None,
    rendered_html: str | None,
) -> PublicReadinessStatus:
    """Classify whether a library page can be treated as a finished asset.

    Brand overview pages remain indexable when the underlying asset_version is
    public. Category pages need a DRL component marker because Phase F found
    many category URLs resolving with generic render output. Leak signatures
    override marker status because an internally contaminated page is never
    public-ready.
    """
    html = rendered_html or ""
    if _INTERNAL_PROVENANCE_RE.search(html):
        return "fix_leak"
    if category_slug is None:
        return "ready"
    if _PUBLIC_COMPONENT_MARKER_RE.search(html):
        return "ready"
    return "hold_no_marker"


# Single source of truth for the curated-metadata field names (D13).
#
# Every component of the seam must agree on this exact set:
#   - PRODUCER: ``scripts/seed_from_drl.build_bundle`` writes these keys.
#   - READER:   ``_extract_curated_metadata`` (below) reads these keys.
#   - PANEL:    ``BrandMetadataPanel`` (web) renders props mapped from these keys.
#
# Adding a 7th field requires touching ALL THREE ends plus this constant and
# the ``CURATED_METADATA_FIELDS`` alignment test in ``tests/test_library_curated_seam.py``.
CURATED_METADATA_FIELDS: frozenset[str] = frozenset({
    "tier",
    "category",
    "design_principles",
    "commercial_signal",
    "mood",
    "applicable_to",
})


class CuratedMetadata(TypedDict, total=False):
    """Curated brand metadata extracted from an ``asset_versions.dtcg_json`` blob.

    Every field is optional: a key is present only when the DTCG envelope
    actually carried a usable value. A missing key means "not available for
    this row" (the envelope predates the Phase 3 seeder, or it is an organic
    extraction that was never enriched). Consumers must treat a missing key
    the same as ``None`` and degrade gracefully.

    Returned as a ``TypedDict`` rather than a positional tuple so that adding a
    field (Phase 4 added ``mood`` + ``applicable_to`` to the original four) does
    not silently shift positional unpacking at the call site.

    Field sources (all set at seed time by ``scripts/seed_from_drl.build_bundle``):

    - ``tier`` / ``category``: from ``StrippedEntry`` which reads ``corpus.json``.
    - ``design_principles`` / ``commercial_signal``: from
      ``systems/<slug>/system.json`` (Phase 3, 2026-06-08). Absent when that
      file did not exist at seed time for the brand.
    - ``mood`` / ``applicable_to``: curated design-behaviour tags from the DRL
      asset; always written by ``build_bundle`` but only surfaced from Phase 4
      (2026-06-08) onward. Part of Frank's locked D-C metadata set.
    """

    tier: str
    category: str
    design_principles: list[str]
    commercial_signal: str
    mood: list[str]
    applicable_to: list[str]


def _clean_str(raw: Any) -> str | None:
    """Return a non-empty stripped string, or ``None`` for any other input."""
    return str(raw) if isinstance(raw, str) and raw.strip() else None


def _clean_str_list(raw: Any) -> list[str] | None:
    """Return a list of the string members of ``raw``, or ``None`` when ``raw``
    is not a list. An all-non-string list collapses to ``[]`` (present but
    empty), which is distinct from ``None`` (key was absent on the wire)."""
    if not isinstance(raw, list):
        return None
    return [str(item) for item in raw if isinstance(item, str)]


def _extract_curated_metadata(dtcg_json: Any) -> CuratedMetadata:
    """Extract curated brand metadata from an ``asset_versions.dtcg_json`` blob.

    Returns a ``CuratedMetadata`` carrying only the keys that resolved to a
    usable value; absent keys signal "not available" (see ``CuratedMetadata``).
    Never raises on malformed input: a non-dict ``dtcg_json`` yields an empty
    result so a single bad row cannot 500 the brand page.
    """
    result: CuratedMetadata = {}
    if not isinstance(dtcg_json, dict):
        return result

    tier = _clean_str(dtcg_json.get("tier"))
    if tier is not None:
        result["tier"] = tier

    category = _clean_str(dtcg_json.get("category"))
    if category is not None:
        result["category"] = category

    design_principles = _clean_str_list(dtcg_json.get("design_principles"))
    if design_principles is not None:
        result["design_principles"] = design_principles

    commercial_signal = _clean_str(dtcg_json.get("commercial_signal"))
    if commercial_signal is not None:
        result["commercial_signal"] = commercial_signal

    mood = _clean_str_list(dtcg_json.get("mood"))
    if mood is not None:
        result["mood"] = mood

    applicable_to = _clean_str_list(dtcg_json.get("applicable_to"))
    if applicable_to is not None:
        result["applicable_to"] = applicable_to

    return result


def _page_to_data(
    page: LibraryPage,
    asset_version: AssetVersion,
    *,
    category_slug: str | None,
    version_label: str | None,
    asset_id: str | None,
    is_canonical: bool,
    is_version_snapshot: bool,
    related: list[RelatedItem],
) -> LibraryPageData:
    """Materialize the contract-shaped page payload from ORM rows."""
    missing_groups, captured_groups = _extract_page_manifest_fields(page.metadata_json)
    readiness = _public_readiness_status(
        category_slug=category_slug,
        rendered_html=page.rendered_html,
    )
    source_url = _public_source_url(asset_version.url, page.brand_slug)
    token_payload = build_library_token_payload(
        asset_version.dtcg_json,
        brand_slug=page.brand_slug,
        source_url=source_url,
    )
    token_exportable = readiness == "ready" and token_payload is not None
    payload = LibraryPageData(
        schema_version=LIBRARY_DATA_SCHEMA_VERSION,
        brand_slug=page.brand_slug,
        source_url=source_url,
        category_slug=category_slug,
        category_label=_title_case(category_slug) if category_slug else None,
        category_kind=_category_kind(category_slug) if category_slug else None,
        version_label=version_label,
        asset_id=asset_id,
        is_canonical=is_canonical,
        is_version_snapshot=is_version_snapshot,
        rendered_html=page.rendered_html or "",
        related=related,
        updated_at=_isoformat(asset_version.fetched_at),
        public_readiness_status=readiness,
        is_public_indexable=readiness == "ready",
        is_exportable=token_exportable,
        missing_groups=missing_groups,
        captured_groups=captured_groups,
    )
    # Merge curated metadata. ``_extract_curated_metadata`` returns ONLY the
    # keys that resolved to a usable value, so omitting an absent key (rather
    # than setting it to None) flows through naturally: web consumers can
    # distinguish "not seeded" (key absent) from "seeded but empty" (key
    # present, value []). Both TypedDicts use total=False so the update is type
    # safe and the merged keys are a subset of LibraryPageData's optional keys.
    if token_exportable and token_payload is not None:
        payload["library_token_export"] = token_payload
    payload.update(_extract_curated_metadata(asset_version.dtcg_json))
    return payload



def _category_version_lookup(
    session: Session,
    *,
    brand_slug: str,
    category_slug: str,
    version_label: str,
) -> tuple[LibraryPage, AssetVersion, str] | None:
    """Return a category-version row, accepting DRL class aliases.

    Category canonical lookup already accepts old DRL corpus class slugs such
    as heroes for the canonical hero row. Version-scoped and asset-scoped
    routes need the same fallback so Phase C can safely remove or reconcile
    stale plural rows without creating 404s for old oracle-style URLs.
    """
    for lookup_slug in category_lookup_slugs(category_slug):
        stmt = (
            select(LibraryPage, AssetVersion)
            .join(AssetVersion, AssetVersion.id == LibraryPage.asset_version_id)
            .where(LibraryPage.brand_slug == brand_slug)
            .where(LibraryPage.category_slug == lookup_slug)
            .where(LibraryPage.version_label == version_label)
            .where(AssetVersion.is_public.is_(True))
            .order_by(
                LibraryPage.is_canonical.desc(),
                AssetVersion.fetched_at.desc(),
                LibraryPage.id.desc(),
            )
            .limit(1)
        )
        row = session.execute(stmt).first()
        if row is not None:
            page, asset_version = row
            return page, asset_version, lookup_slug
    return None

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/library/brands")
def list_brands(
    page: int = Query(default=1, ge=1, description="1-indexed page number"),
    page_size: int = Query(
        default=DEFAULT_HUB_PAGE_SIZE, ge=1, le=MAX_HUB_PAGE_SIZE,
        description="Page size (default 25, max 100)",
    ),
    session: Session = Depends(get_db),
) -> JSONResponse:
    """List public-corpus brands for the ``/library/`` hub page.

    Returns ``page`` of size ``page_size`` ordered by brand_slug ascending,
    each row carrying the brand's most recent source_url (from any public
    asset_version owning a library_page for the brand) and the count of
    DISTINCT category pages currently live for the brand. Quality gate is
    enforced by the upstream indexer (library_pages is the only table read)
    plus ``asset_versions.is_public`` re-checked at the join.

    Note on canonical-flag handling
    -------------------------------
    The hub deliberately does NOT filter on ``is_canonical=True``. The
    canonical flag is owned by the indexer's reconciler and flips between
    rows of the same (brand_slug, category_slug) as new asset_versions
    arrive. For the hub's purpose (does this brand have ANY public library
    content?), requiring is_canonical=True would silently hide brands whose
    rows were seeded outside the indexer's reconciliation path (e.g. the
    DRL bootstrap) or whose canonical row was momentarily flipped FALSE
    during a reconcile window. We count DISTINCT category_slug so a brand
    with both canonical and non-canonical rows for the same category
    contributes one to category_count, matching the brand-detail page count.
    The detail endpoints still gate on is_canonical where the contract
    demands a single canonical row.
    """
    # Aggregate library pages per brand with DISTINCT category count and
    # representative asset_version (most recent public) per brand_slug.
    base = (
        select(
            LibraryPage.brand_slug.label("brand_slug"),
            func.count(func.distinct(LibraryPage.category_slug)).label("category_count"),
        )
        .join(AssetVersion, AssetVersion.id == LibraryPage.asset_version_id)
        .where(AssetVersion.is_public.is_(True))
        .group_by(LibraryPage.brand_slug)
        .order_by(LibraryPage.brand_slug.asc())
    )
    total = session.execute(
        select(func.count()).select_from(base.subquery())
    ).scalar_one()
    offset = (page - 1) * page_size
    rows = session.execute(base.limit(page_size).offset(offset)).all()
    featured: list[HubFeaturedRow] = []
    for brand_slug, category_count in rows:
        url = _source_url_for_brand(session, brand_slug) or ""
        meta = _hub_meta_for_brand(session, brand_slug)
        featured.append(HubFeaturedRow(
            brand_slug=brand_slug,
            source_url=_public_source_url(url, brand_slug),
            category_count=int(category_count),
            palette=meta["palette"],
            display_font=meta["display_font"],
            captured_count=meta["captured_count"],
            total_showcase_groups=meta["total_showcase_groups"],
        ))
    payload = LibraryHubData(
        schema_version=LIBRARY_DATA_SCHEMA_VERSION,
        featured=featured,
        page=page,
        page_size=page_size,
        total=int(total),
    )
    return _json(dict(payload), cache=CACHE_HUB)


@router.get("/library/brands/{brand_slug}")
def get_brand_canonical(
    brand_slug: str,
    session: Session = Depends(get_db),
) -> JSONResponse:
    """Return the canonical brand page (latest pull, alphabet type-specimen).

    Prefers category_slug='alphabet' per D19: the type-specimen is the intended
    featured artifact. All 18 template classes share identical fetched_at within
    one asset_version, so LIMIT 1 without this filter returns an arbitrary class.
    Falls back to any canonical page when no alphabet page exists (pre-v5 corpora,
    lightweight test fixtures). 404 only when no canonical page of any kind exists.
    """
    _validate_brand_slug(brand_slug)

    def _canonical_stmt(*, category_slug: str | None) -> Select[Any]:
        """Build the canonical-page query, optionally pinned to a category.

        category_slug=None drops the category filter so the brand's single
        most-recently-fetched canonical page is returned (fallback path for
        corpora that predate the v5 alphabet type-specimen).
        """
        base = (
            select(LibraryPage, AssetVersion)
            .join(AssetVersion, AssetVersion.id == LibraryPage.asset_version_id)
            .where(LibraryPage.brand_slug == brand_slug)
            .where(LibraryPage.is_canonical.is_(True))
            .where(AssetVersion.is_public.is_(True))
            .order_by(AssetVersion.fetched_at.desc())
            .limit(1)
        )
        if category_slug is not None:
            base = base.where(LibraryPage.category_slug == category_slug)
        return base

    row = session.execute(_canonical_stmt(category_slug="alphabet")).first()
    if row is None:
        row = session.execute(_canonical_stmt(category_slug=None)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="brand_not_found")
    page, asset_version = row
    data = _page_to_data(
        page, asset_version,
        category_slug=None, version_label=None, asset_id=None,
        is_canonical=True, is_version_snapshot=False,
        related=_related_for(session, brand_slug),
    )
    return _json(dict(data), cache=CACHE_PAGE)


@router.get("/library/brands/{brand_slug}/versions/{version_label}")
def get_brand_version_snapshot(
    brand_slug: str,
    version_label: str,
    session: Session = Depends(get_db),
) -> JSONResponse:
    """Return a brand-snapshot page for one version (no category filter)."""
    _validate_brand_slug(brand_slug)
    _validate_version_label(version_label)
    stmt = (
        select(LibraryPage, AssetVersion)
        .join(AssetVersion, AssetVersion.id == LibraryPage.asset_version_id)
        .where(LibraryPage.brand_slug == brand_slug)
        .where(LibraryPage.version_label == version_label)
        .where(AssetVersion.is_public.is_(True))
        .order_by(AssetVersion.fetched_at.desc())
        .limit(1)
    )
    row = session.execute(stmt).first()
    if row is None:
        raise HTTPException(status_code=404, detail="version_not_found")
    page, asset_version = row
    data = _page_to_data(
        page, asset_version,
        category_slug=None, version_label=version_label, asset_id=None,
        is_canonical=False, is_version_snapshot=True,
        related=_related_for(session, brand_slug),
    )
    return _json(dict(data), cache=CACHE_PAGE)


@router.get("/library/brands/{brand_slug}/categories/{category_slug}")
def get_brand_category_canonical(
    brand_slug: str,
    category_slug: str,
    session: Session = Depends(get_db),
) -> JSONResponse:
    """Return the canonical (latest-version) page for one category."""
    _validate_brand_slug(brand_slug)
    _validate_category_slug(category_slug)
    row = None
    matched_category_slug = category_slug
    for lookup_slug in category_lookup_slugs(category_slug):
        stmt = (
            select(LibraryPage, AssetVersion)
            .join(AssetVersion, AssetVersion.id == LibraryPage.asset_version_id)
            .where(LibraryPage.brand_slug == brand_slug)
            .where(LibraryPage.category_slug == lookup_slug)
            .where(LibraryPage.is_canonical.is_(True))
            .where(AssetVersion.is_public.is_(True))
            .order_by(AssetVersion.fetched_at.desc())
            .limit(1)
        )
        row = session.execute(stmt).first()
        if row is not None:
            matched_category_slug = lookup_slug
            break
    if row is None:
        raise HTTPException(status_code=404, detail="category_not_found")
    page, asset_version = row
    data = _page_to_data(
        page, asset_version,
        category_slug=category_slug, version_label=None, asset_id=None,
        is_canonical=True, is_version_snapshot=False,
        related=_related_for(session, brand_slug, exclude_category=matched_category_slug),
    )
    return _json(dict(data), cache=CACHE_PAGE)


@router.get(
    "/library/brands/{brand_slug}/categories/{category_slug}/{version_label}"
)
def get_brand_category_version(
    brand_slug: str,
    category_slug: str,
    version_label: str,
    session: Session = Depends(get_db),
) -> JSONResponse:
    """Return the page for one category scoped to one version."""
    _validate_brand_slug(brand_slug)
    _validate_category_slug(category_slug)
    _validate_version_label(version_label)
    row = _category_version_lookup(
        session,
        brand_slug=brand_slug,
        category_slug=category_slug,
        version_label=version_label,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="category_version_not_found")
    page, asset_version, matched_category_slug = row
    data = _page_to_data(
        page, asset_version,
        category_slug=category_slug, version_label=version_label, asset_id=None,
        is_canonical=False, is_version_snapshot=True,
        related=_related_for(session, brand_slug, exclude_category=matched_category_slug),
    )
    return _json(dict(data), cache=CACHE_PAGE)

@router.get(
    "/library/brands/{brand_slug}/categories/{category_slug}/{version_label}/{asset_id}"
)
def get_brand_category_version_asset(
    brand_slug: str,
    category_slug: str,
    version_label: str,
    asset_id: str,
    session: Session = Depends(get_db),
) -> JSONResponse:
    """Return the asset-instance page within a (category, version, brand)."""
    _validate_brand_slug(brand_slug)
    _validate_category_slug(category_slug)
    _validate_version_label(version_label)
    _validate_asset_id(asset_id)
    row = _category_version_lookup(
        session,
        brand_slug=brand_slug,
        category_slug=category_slug,
        version_label=version_label,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="asset_not_found")
    page, asset_version, matched_category_slug = row
    # The page row carries an aggregate compose render rather than per-asset
    # markup. The mock returns a single LibraryPageData whose asset_id
    # field is populated and rendered_html is the same compose output. We
    # mirror that contract here so the route tree renders end-to-end.
    data = _page_to_data(
        page, asset_version,
        category_slug=category_slug, version_label=version_label, asset_id=asset_id,
        is_canonical=False, is_version_snapshot=True,
        related=_related_for(session, brand_slug, exclude_category=matched_category_slug),
    )
    return _json(dict(data), cache=CACHE_PAGE)

@router.get("/library/sitemap")
def get_sitemap(session: Session = Depends(get_db)) -> JSONResponse:
    """Return every public library route as a flat list for sitemap.ts.

    Output mirrors the URL hierarchy: ``/library/``, ``/library/<brand>/``,
    ``/library/<brand>/<version>/``, ``/library/<brand>/<category>/``, and
    ``/library/<brand>/<category>/<version>/`` rows. Asset-instance rows are
    intentionally NOT emitted in v1 because the underlying compose render is
    one-per-category, not one-per-asset; emitting per-asset URLs would invite
    crawlers to discover identical content under N URLs.
    """
    stmt = (
        select(
            LibraryPage.brand_slug,
            LibraryPage.category_slug,
            LibraryPage.version_label,
            LibraryPage.rendered_html,
            AssetVersion.fetched_at,
        )
        .join(AssetVersion, AssetVersion.id == LibraryPage.asset_version_id)
        .where(AssetVersion.is_public.is_(True))
        .order_by(LibraryPage.brand_slug.asc(), LibraryPage.category_slug.asc())
    )
    rows = session.execute(stmt).all()
    now = _isoformat(None)
    entries: list[LibrarySitemapEntry] = [
        LibrarySitemapEntry(path="/library/", last_modified=now)
    ]
    seen_paths: set[str] = {"/library/"}

    def _add(path: str, last_modified: str) -> None:
        if path in seen_paths:
            return
        seen_paths.add(path)
        entries.append(LibrarySitemapEntry(path=path, last_modified=last_modified))

    for brand_slug, category_slug, version_label, rendered_html, fetched_at in rows:
        ts = _isoformat(fetched_at)
        canonical_category_slug = canonical_public_category_slug(category_slug)
        _add(f"/library/{brand_slug}/", ts)
        if canonical_category_slug is None:
            continue
        readiness = _public_readiness_status(
            category_slug=canonical_category_slug,
            rendered_html=rendered_html,
        )
        if readiness != "ready":
            continue
        _add(f"/library/{brand_slug}/{canonical_category_slug}/", ts)
        if version_label and not _is_internal_version_label(version_label):
            _add(f"/library/{brand_slug}/{version_label}/", ts)
            _add(f"/library/{brand_slug}/{canonical_category_slug}/{version_label}/", ts)
    payload = {
        "schema_version": LIBRARY_DATA_SCHEMA_VERSION,
        "entries": entries,
        "total": len(entries),
    }
    return _json(payload, cache=CACHE_SITEMAP)

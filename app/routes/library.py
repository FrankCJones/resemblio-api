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
from typing import Any, TypedDict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app.constants import SCHEMA_V1_1
from app.db import get_db
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
_BRAND_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_CATEGORY_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_VERSION_LABEL_RE = re.compile(r"^[0-9]{4}-[0-9]{2}$")
_ASSET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$", re.IGNORECASE)

RESERVED_BRAND_SLUGS = frozenset(
    {"app", "api", "about", "pricing", "contact", "library", "docs", "blog",
     "privacy", "terms", "admin", "_next"}
)


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


class HubFeaturedRow(TypedDict):
    """One row of the hub list."""

    brand_slug: str
    source_url: str
    category_count: int


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
    """Title-case a kebab slug for display."""
    return " ".join(p.capitalize() for p in slug.split("-") if p)


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


def _related_for(session: Session, brand_slug: str,
                 *, exclude_category: str | None = None) -> list[RelatedItem]:
    """Build the cross-link related list for a page.

    Same shape the mock emits: sibling categories of this brand first
    (capped at 4), then each known version label for the brand.
    """
    related: list[RelatedItem] = []
    cat_stmt = (
        select(LibraryPage.category_slug)
        .where(LibraryPage.brand_slug == brand_slug)
        .where(LibraryPage.is_canonical.is_(True))
        .distinct()
    )
    cats = sorted({row[0] for row in session.execute(cat_stmt).all()
                   if row[0] and row[0] != exclude_category})
    for cat in cats[:4]:
        related.append(RelatedItem(
            label=f"{_title_case(cat)} from {_title_case(brand_slug)}",
            href=f"/library/{brand_slug}/{cat}/",
        ))
    ver_stmt = (
        select(LibraryPage.version_label)
        .where(LibraryPage.brand_slug == brand_slug)
        .where(LibraryPage.version_label.is_not(None))
        .distinct()
    )
    versions = sorted({row[0] for row in session.execute(ver_stmt).all() if row[0]})
    for v in versions:
        related.append(RelatedItem(
            label=f"{_title_case(brand_slug)} design system in {v}",
            href=f"/library/{brand_slug}/{v}/",
        ))
    return related


def _isoformat(dt: datetime | None) -> str:
    """Return an ISO-8601 timestamp; falls back to now when dt is NULL."""
    target = dt if dt is not None else datetime.now(timezone.utc)
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return target.isoformat()


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
    return LibraryPageData(
        schema_version=LIBRARY_DATA_SCHEMA_VERSION,
        brand_slug=page.brand_slug,
        source_url=asset_version.url,
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
    )


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
        featured.append(HubFeaturedRow(
            brand_slug=brand_slug,
            source_url=url,
            category_count=int(category_count),
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
    """Return the canonical brand page (latest pull, no category filter).

    Picks the most-recently-fetched canonical page for the brand as the
    representative row. 404 when no canonical page exists.
    """
    _validate_brand_slug(brand_slug)
    stmt = (
        select(LibraryPage, AssetVersion)
        .join(AssetVersion, AssetVersion.id == LibraryPage.asset_version_id)
        .where(LibraryPage.brand_slug == brand_slug)
        .where(LibraryPage.is_canonical.is_(True))
        .where(AssetVersion.is_public.is_(True))
        .order_by(AssetVersion.fetched_at.desc())
        .limit(1)
    )
    row = session.execute(stmt).first()
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
    stmt = (
        select(LibraryPage, AssetVersion)
        .join(AssetVersion, AssetVersion.id == LibraryPage.asset_version_id)
        .where(LibraryPage.brand_slug == brand_slug)
        .where(LibraryPage.category_slug == category_slug)
        .where(LibraryPage.is_canonical.is_(True))
        .where(AssetVersion.is_public.is_(True))
        .order_by(AssetVersion.fetched_at.desc())
        .limit(1)
    )
    row = session.execute(stmt).first()
    if row is None:
        raise HTTPException(status_code=404, detail="category_not_found")
    page, asset_version = row
    data = _page_to_data(
        page, asset_version,
        category_slug=category_slug, version_label=None, asset_id=None,
        is_canonical=True, is_version_snapshot=False,
        related=_related_for(session, brand_slug, exclude_category=category_slug),
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
    stmt = (
        select(LibraryPage, AssetVersion)
        .join(AssetVersion, AssetVersion.id == LibraryPage.asset_version_id)
        .where(LibraryPage.brand_slug == brand_slug)
        .where(LibraryPage.category_slug == category_slug)
        .where(LibraryPage.version_label == version_label)
        .where(AssetVersion.is_public.is_(True))
        .limit(1)
    )
    row = session.execute(stmt).first()
    if row is None:
        raise HTTPException(status_code=404, detail="category_version_not_found")
    page, asset_version = row
    data = _page_to_data(
        page, asset_version,
        category_slug=category_slug, version_label=version_label, asset_id=None,
        is_canonical=False, is_version_snapshot=True,
        related=_related_for(session, brand_slug, exclude_category=category_slug),
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
    stmt = (
        select(LibraryPage, AssetVersion)
        .join(AssetVersion, AssetVersion.id == LibraryPage.asset_version_id)
        .where(LibraryPage.brand_slug == brand_slug)
        .where(LibraryPage.category_slug == category_slug)
        .where(LibraryPage.version_label == version_label)
        .where(AssetVersion.is_public.is_(True))
        .limit(1)
    )
    row = session.execute(stmt).first()
    if row is None:
        raise HTTPException(status_code=404, detail="asset_not_found")
    page, asset_version = row
    # The page row carries an aggregate compose render rather than per-asset
    # markup. The mock returns a single ``LibraryPageData`` whose asset_id
    # field is populated and rendered_html is the same compose output - we
    # mirror that contract here so the route tree renders end-to-end.
    data = _page_to_data(
        page, asset_version,
        category_slug=category_slug, version_label=version_label, asset_id=asset_id,
        is_canonical=False, is_version_snapshot=True,
        related=_related_for(session, brand_slug, exclude_category=category_slug),
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

    for brand_slug, category_slug, version_label, fetched_at in rows:
        ts = _isoformat(fetched_at)
        _add(f"/library/{brand_slug}/", ts)
        _add(f"/library/{brand_slug}/{category_slug}/", ts)
        if version_label:
            _add(f"/library/{brand_slug}/{version_label}/", ts)
            _add(f"/library/{brand_slug}/{category_slug}/{version_label}/", ts)
    payload = {
        "schema_version": LIBRARY_DATA_SCHEMA_VERSION,
        "entries": entries,
        "total": len(entries),
    }
    return _json(payload, cache=CACHE_SITEMAP)

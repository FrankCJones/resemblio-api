"""Suppress DRL seed utility brands from the public library brand list.

Purpose
-------
Some DRL (_shared/*) seed entries seeded library pages under generic brand slugs
(e.g. ``shared``) that are not curated brands. They should not appear in the
public ``/v1/library/brands`` hub. This script sets ``asset_versions.is_public=False``
for every asset_version row that backs a library_page for a suppressed slug.

The suppression is:
  - NOT a deletion. Rows are kept; they are hidden from public reads.
  - Reversible. Flip ``is_public=True`` to restore.
  - Idempotent. Re-running when already suppressed is a no-op.
  - Cross-brand safe. The script verifies no other brand's library_pages share
    the affected asset_versions before suppressing.

Run command (on prod, from /opt/resemblio-api)::

    sudo -u postgres psql resemblio -f scripts/suppress_seed_brands.sql

Or via Python (reads RESEMBLIO_DB_URL from the .env)::

    source .env && venv/bin/python scripts/suppress_seed_brands.py

First executed: 2026-06-14 (Phase 17.0b). Suppressed ``shared`` (81 asset_versions,
1458 library_pages). API brand count dropped from 41 to 40.

schema_version: suppress_seed_brands_v1
"""
from __future__ import annotations

import sys

from sqlalchemy import func, select, text, update

from app.config import get_settings, load_project_env
from app.db import create_db_engine, sessionmaker
from app.library_suppression import SUPPRESSED_SLUGS  # single source of truth (issue #19)
from app.models import AssetVersion, LibraryPage


def suppress_seed_brands(dry_run: bool = False) -> None:
    """Suppress all SUPPRESSED_SLUGS from the public library brand list.

    For each suppressed slug:
    1. Collect all distinct asset_version_ids backing that slug's library_pages.
    2. Verify none of those asset_versions back another brand's pages (cross-brand
       contamination guard).
    3. Set is_public=False on each affected asset_version.

    Args:
        dry_run: If True, print what would change without committing.
    """
    load_project_env()
    settings = get_settings()
    engine = create_db_engine(settings.database_url)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        for slug in SUPPRESSED_SLUGS:
            av_ids_query = (
                select(LibraryPage.asset_version_id)
                .where(LibraryPage.brand_slug == slug)
                .distinct()
            )
            av_ids = [r[0] for r in session.execute(av_ids_query).all()]

            if not av_ids:
                print(f"[{slug}] no library_pages found - already cleaned or never seeded")
                continue

            # Cross-brand contamination guard.
            other_brands = session.execute(
                select(LibraryPage.brand_slug)
                .where(
                    LibraryPage.asset_version_id.in_(av_ids),
                    LibraryPage.brand_slug != slug,
                )
                .distinct()
            ).scalars().all()

            if other_brands:
                print(
                    f"[{slug}] SKIP - asset_versions are shared with other brands: {other_brands}. "
                    "Manual review required."
                )
                continue

            # Count how many are already suppressed vs still public.
            public_count = session.execute(
                select(func.count()).select_from(AssetVersion).where(
                    AssetVersion.id.in_(av_ids),
                    AssetVersion.is_public.is_(True),
                )
            ).scalar_one()

            if public_count == 0:
                print(f"[{slug}] already suppressed ({len(av_ids)} asset_versions, all is_public=False)")
                continue

            if dry_run:
                print(
                    f"[{slug}] DRY RUN: would suppress {public_count} asset_versions "
                    f"({len(av_ids)} total, {len(av_ids) - public_count} already suppressed)"
                )
            else:
                session.execute(
                    update(AssetVersion)
                    .where(AssetVersion.id.in_(av_ids), AssetVersion.is_public.is_(True))
                    .values(is_public=False)
                )
                session.commit()
                print(
                    f"[{slug}] suppressed {public_count} asset_versions "
                    f"(total backing asset_versions: {len(av_ids)})"
                )

    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    suppress_seed_brands(dry_run=dry_run)

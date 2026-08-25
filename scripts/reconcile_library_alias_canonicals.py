"""Reconcile stale library canonical flags across category aliases.

Purpose
-------
Classify public library_pages rows where a generic canonical page is masking a
marker-backed DRL component row for the same public category. The first apply
mode only flips is_canonical flags. It does not delete pages, enqueue jobs,
reseed DRL, or rewrite rendered_html.

Run commands
------------

    python -m scripts.reconcile_library_alias_canonicals
    python -m scripts.reconcile_library_alias_canonicals --environment-label prod-dry-run
    python -m scripts.reconcile_library_alias_canonicals --apply --environment-label prod
    python -m scripts.reconcile_library_alias_canonicals --out 02-prd/phase-c/reconcile.json

Safety
------
Dry-run is the default. Apply requires --apply. The JSON report includes the
row ids promoted and demoted so a rollback can reverse the canonical flags.

Schema
------
schema_version: reconcile_library_alias_canonicals_v1
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent.parent))

from sqlalchemy import func, select, update  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.library_category_aliases import (  # noqa: E402
    DRL_COMPONENT_MARKER,
    canonical_public_category_slug,
)
from app.models import AssetVersion, LibraryIndexJob, LibraryPage  # noqa: E402

SCHEMA_VERSION = "reconcile_library_alias_canonicals_v1"
LOGGER = logging.getLogger("resemblio.reconcile_library_alias_canonicals")


class PromotionPlan(TypedDict):
    """One canonical-flag promotion proposed or applied by the script."""

    brand_slug: str
    public_category_slug: str
    category_slugs_seen: list[str]
    current_canonical_ids: list[int]
    promote_id: int
    promote_asset_version_id: int
    promote_category_slug: str
    demote_ids: list[int]
    marker_competitor_ids: list[int]
    reason: str


class NoMarkerGroup(TypedDict):
    """A generic canonical group that has no marker-backed competitor."""

    brand_slug: str
    public_category_slug: str
    category_slugs_seen: list[str]
    current_canonical_ids: list[int]
    current_canonical_categories: list[str]
    reason: str


class ReconcileReport(TypedDict):
    """Structured report emitted by dry-run and apply modes."""

    schema_version: str
    generated_at: str
    environment_label: str
    mode: str
    promotions: list[PromotionPlan]
    no_marker_competitor: list[NoMarkerGroup]
    job_status_counts: dict[str, int]
    counts: dict[str, int]


@dataclass(frozen=True)
class PageCandidate:
    """Library page candidate grouped by canonical public category."""

    id: int
    asset_version_id: int
    brand_slug: str
    category_slug: str
    public_category_slug: str
    version_label: str | None
    fetched_at: datetime | None
    rendered_length: int
    has_marker: bool
    is_canonical: bool


def _has_marker(rendered_html: str | None) -> bool:
    """Return True only when a page carries the real DRL component marker."""
    return bool(rendered_html and DRL_COMPONENT_MARKER in rendered_html)


def _rendered_length(rendered_html: str | None) -> int:
    """Return rendered HTML length with None treated as empty."""
    return len(rendered_html or "")


def _iso(dt: datetime | None) -> str | None:
    """Serialize a datetime for evidence JSON."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _rank(candidate: PageCandidate) -> tuple[int, str, int, int]:
    """Rank marker candidates the same way the indexer chooses winners."""
    fetched = _iso(candidate.fetched_at) or ""
    return (1 if candidate.rendered_length > 0 else 0, fetched, candidate.asset_version_id, candidate.id)


def _candidate_to_marker_id(candidate: PageCandidate) -> int:
    """Return the stable row id used in reports for marker competitors."""
    return candidate.id


def _job_status_counts(session: Session) -> dict[str, int]:
    """Return library_index_jobs counts by status for evidence and safety checks."""
    rows = session.execute(
        select(LibraryIndexJob.status, func.count(LibraryIndexJob.id)).group_by(
            LibraryIndexJob.status
        )
    ).all()
    return {str(status): int(count) for status, count in rows}


def collect_candidates(session: Session) -> list[PageCandidate]:
    """Load public library page rows and normalize them to public categories."""
    rows = session.execute(
        select(
            LibraryPage.id,
            LibraryPage.asset_version_id,
            LibraryPage.brand_slug,
            LibraryPage.category_slug,
            LibraryPage.version_label,
            LibraryPage.rendered_html,
            LibraryPage.is_canonical,
            AssetVersion.fetched_at,
        )
        .join(AssetVersion, AssetVersion.id == LibraryPage.asset_version_id)
        .where(AssetVersion.is_public.is_(True))
    ).all()
    candidates: list[PageCandidate] = []
    for row in rows:
        public_slug = canonical_public_category_slug(row.category_slug)
        if public_slug is None:
            continue
        candidates.append(
            PageCandidate(
                id=int(row.id),
                asset_version_id=int(row.asset_version_id),
                brand_slug=str(row.brand_slug),
                category_slug=str(row.category_slug),
                public_category_slug=public_slug,
                version_label=row.version_label,
                fetched_at=row.fetched_at,
                rendered_length=_rendered_length(row.rendered_html),
                has_marker=_has_marker(row.rendered_html),
                is_canonical=bool(row.is_canonical),
            )
        )
    return candidates


def build_report(
    session: Session,
    *,
    environment_label: str,
    mode: str,
) -> ReconcileReport:
    """Classify stale canonical groups and return a schema-versioned report."""
    candidates = collect_candidates(session)
    groups: dict[tuple[str, str], list[PageCandidate]] = defaultdict(list)
    for candidate in candidates:
        groups[(candidate.brand_slug, candidate.public_category_slug)].append(candidate)

    promotions: list[PromotionPlan] = []
    no_marker: list[NoMarkerGroup] = []
    for (brand_slug, public_slug), group in sorted(groups.items()):
        current = [candidate for candidate in group if candidate.is_canonical]
        stale_current = [candidate for candidate in current if not candidate.has_marker]
        if not stale_current:
            continue
        marker_candidates = [candidate for candidate in group if candidate.has_marker]
        categories_seen = sorted({candidate.category_slug for candidate in group})
        if not marker_candidates:
            no_marker.append(
                NoMarkerGroup(
                    brand_slug=brand_slug,
                    public_category_slug=public_slug,
                    category_slugs_seen=categories_seen,
                    current_canonical_ids=[candidate.id for candidate in current],
                    current_canonical_categories=sorted(
                        {candidate.category_slug for candidate in current}
                    ),
                    reason="canonical row has no marker-backed competitor",
                )
            )
            continue
        winner = sorted(marker_candidates, key=_rank, reverse=True)[0]
        promotions.append(
            PromotionPlan(
                brand_slug=brand_slug,
                public_category_slug=public_slug,
                category_slugs_seen=categories_seen,
                current_canonical_ids=[candidate.id for candidate in current],
                promote_id=winner.id,
                promote_asset_version_id=winner.asset_version_id,
                promote_category_slug=winner.category_slug,
                demote_ids=[candidate.id for candidate in current if candidate.id != winner.id],
                marker_competitor_ids=[
                    _candidate_to_marker_id(candidate) for candidate in marker_candidates
                ],
                reason="generic canonical row masks marker-backed component",
            )
        )

    return ReconcileReport(
        schema_version=SCHEMA_VERSION,
        generated_at=datetime.now(timezone.utc).isoformat(),
        environment_label=environment_label,
        mode=mode,
        promotions=promotions,
        no_marker_competitor=no_marker,
        job_status_counts=_job_status_counts(session),
        counts={
            "candidate_rows": len(candidates),
            "promotion_groups": len(promotions),
            "promote_rows": len({item["promote_id"] for item in promotions}),
            "demote_rows": sum(len(item["demote_ids"]) for item in promotions),
            "no_marker_groups": len(no_marker),
        },
    )


def apply_promotions(session: Session, promotions: list[PromotionPlan]) -> None:
    """Apply canonical flag flips for promotion plans in one transaction."""
    for promotion in promotions:
        session.execute(
            update(LibraryPage)
            .where(LibraryPage.id == promotion["promote_id"])
            .values(is_canonical=True)
        )
        if promotion["demote_ids"]:
            session.execute(
                update(LibraryPage)
                .where(LibraryPage.id.in_(promotion["demote_ids"]))
                .values(is_canonical=False)
            )
    session.commit()


def _write_report(path: Path, report: ReconcileReport) -> None:
    """Write a report JSON file for review artifacts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    """Build the command line parser."""
    parser = argparse.ArgumentParser(
        description="Classify and reconcile stale Resemblio library canonicals.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the canonical flag flips. Default is dry-run only.",
    )
    parser.add_argument(
        "--environment-label",
        default="local",
        help="Evidence label for the target environment.",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Optional path to write the JSON report.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the reconciler CLI and return a process exit code."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _build_parser().parse_args(argv)
    mode = "apply" if args.apply else "dry-run"
    with SessionLocal() as session:
        report = build_report(session, environment_label=args.environment_label, mode=mode)
        if args.apply:
            apply_promotions(session, report["promotions"])
            report = build_report(session, environment_label=args.environment_label, mode="post-apply")
    if args.out:
        _write_report(Path(args.out), report)
    print(json.dumps(report, indent=2, sort_keys=True))
    LOGGER.info(
        "mode=%s promotion_groups=%s no_marker_groups=%s",
        report["mode"],
        report["counts"]["promotion_groups"],
        report["counts"]["no_marker_groups"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""Orchestrate DRL library bootstrap (Resemblio v1.1 mission Phase 8).

The DRL ships two surfaces:

- ``corpus.json`` at the DRL root (41 systems, 955 component-level assets)
- ``_extractions/<brand>/`` (24 brand directories pre-composed into per-category
  renders by the upstream compose pipeline)

This orchestrator anchors brand discovery on ``_extractions/`` because those
are the brands the indexer can immediately compose into library pages. For
each discovered brand it invokes ``scripts.seed_from_drl.apply_seed`` with a
``--source-system <slug>`` filter so the seed writes only that brand's
component assets. The seed step is idempotent (``content_hash`` dedup on
``asset_versions``); re-running is safe.

The library indexer (mission Phase 4, separate worker) consumes the
``asset_versions`` rows enqueued by the seed and writes ``library_pages``.
This orchestrator does NOT run the indexer; verify with the companion
``scripts.verify_drl_bootstrap`` script after the indexer drains.

Usage
-----
::

    # Dry-run: list discovered brands, classify pending vs already-seeded.
    python -m scripts.bootstrap_drl_library

    # Bootstrap every discovered brand against prod.
    python -m scripts.bootstrap_drl_library --apply

    # Bootstrap only one brand.
    python -m scripts.bootstrap_drl_library --apply --single aeon

    # Stage rollout: first 3 brands only.
    python -m scripts.bootstrap_drl_library --apply --limit 3

    # Report DB state without seeding.
    python -m scripts.bootstrap_drl_library --verify-only

Authorization
-------------
- Local development: GREEN
- Production execution: requires Jim approval per Phase 9 quality gate.
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

_API_ROOT = Path(__file__).resolve().parents[1]
_path_text = str(_API_ROOT)
if _path_text not in sys.path:
    sys.path.insert(0, _path_text)

from app.constants import (
    ASSET_VERSIONS_SEED_SOURCE_LABEL,
    DRL_BOOTSTRAP_MIN_EXPECTED_BRANDS,
    DRL_EXTRACTIONS_DIRNAME,
)
from scripts.seed_from_drl import (  # noqa: E402 - sys.path mutation above
    DEFAULT_BATCH_SIZE,
    DEFAULT_DRL_ROOT,
    DRL_VERSION_LABEL_PREFIX,
    apply_seed,
    filter_assets,
    iter_assets,
    load_corpus,
    plan_only,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from scripts.seed_from_drl import StorageClient

LOG = logging.getLogger("bootstrap_drl_library")
LOG.propagate = True


# --- Constants ---------------------------------------------------------------

EXIT_OK = 0
EXIT_ERROR = 1


class BrandOutcome(TypedDict):
    """Per-brand outcome after one orchestrator pass."""

    brand_dir: str
    library_slug: str
    corpus_system_slug: str
    asset_count_planned: int
    inserted: int
    updated: int
    skipped: int
    status: str  # "ok" | "skipped" | "failed" | "dry-run"
    error: str | None


@dataclass(frozen=True)
class BootstrapArgs:
    """Parsed CLI arguments for the orchestrator."""

    apply: bool
    verify_only: bool
    drl_root: Path
    single: str | None
    limit: int | None
    seed_user_id: int
    batch_size: int


@dataclass
class BootstrapReport:
    """Aggregate outcome across every brand the orchestrator touched."""

    drl_root: str
    brands_discovered: int
    brands_processed: int
    outcomes: list[BrandOutcome] = field(default_factory=list)
    totals_inserted: int = 0
    totals_updated: int = 0
    totals_skipped: int = 0
    failed_brands: list[str] = field(default_factory=list)


# --- Brand-slug normalization ------------------------------------------------

def normalize_library_slug(brand_dir_name: str) -> str:
    """Map a DRL ``_extractions/`` dir name to the library URL slug.

    DRL dir names happen to already be lowercase-with-hyphens (``aeon``,
    ``craig-mod``, ``daring-fireball``, ``the-pudding``). The transformation
    is therefore identity-after-normalize: lowercase + strip + collapse
    whitespace + replace underscores with hyphens. The function exists so a
    future DRL author adding an underscore-named dir (``my_brand``) still
    surfaces a clean URL slug (``my-brand``) on the library side.
    """
    return brand_dir_name.strip().lower().replace("_", "-")


# --- DRL discovery -----------------------------------------------------------

def discover_brand_dirs(drl_root: Path) -> list[Path]:
    """List every brand directory under ``<drl_root>/_extractions/``.

    Returns the directories sorted alphabetically for deterministic output.
    Hidden dirs and the ``_INBOX`` staging area are filtered out so the
    orchestrator never tries to seed a half-authored brand.
    """
    extractions_root = drl_root / DRL_EXTRACTIONS_DIRNAME
    if not extractions_root.exists():
        raise FileNotFoundError(
            f"DRL extractions root not found at {extractions_root!s}. "
            f"Pass --drl-root."
        )
    dirs = []
    for child in sorted(extractions_root.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith("_") or child.name.startswith("."):
            continue
        dirs.append(child)
    return dirs


# --- CLI ---------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> BootstrapArgs:
    """Parse argv into a ``BootstrapArgs``. Dry-run is the default."""
    parser = argparse.ArgumentParser(
        description="Orchestrate DRL bootstrap for the Resemblio library v1.1."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually run the seed. Default is dry-run (no DB, no R2).",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Skip seeding; print current DB state for DRL bootstrap rows.",
    )
    parser.add_argument(
        "--drl-root",
        type=Path,
        default=DEFAULT_DRL_ROOT,
        help="Path to the Design Reference Library root.",
    )
    parser.add_argument(
        "--single",
        type=str,
        default=None,
        help="Process only the brand with this DRL dir name (e.g. 'aeon').",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of brands processed (staged rollout).",
    )
    parser.add_argument(
        "--seed-user-id",
        type=int,
        default=1,
        help="User id that owns seed rows. Defaults to 1 (bootstrap user).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Rows per DB transaction (default {DEFAULT_BATCH_SIZE}).",
    )
    namespace = parser.parse_args(argv)
    return BootstrapArgs(
        apply=bool(namespace.apply),
        verify_only=bool(namespace.verify_only),
        drl_root=Path(namespace.drl_root).resolve(),
        single=namespace.single,
        limit=namespace.limit,
        seed_user_id=int(namespace.seed_user_id),
        batch_size=int(namespace.batch_size),
    )


def select_brands(
    brand_dirs: list[Path],
    single: str | None,
    limit: int | None,
) -> list[Path]:
    """Apply ``--single`` and ``--limit`` filters to the discovered brand list."""
    selected = brand_dirs
    if single is not None:
        selected = [p for p in selected if p.name == single]
        if not selected:
            available = ", ".join(p.name for p in brand_dirs)
            raise ValueError(
                f"--single {single!r} matched no brand dir. Available: {available}"
            )
    if limit is not None:
        selected = selected[:limit]
    return selected


# --- Per-brand orchestration -------------------------------------------------

def process_brand_dry_run(
    brand_dir: Path,
    drl_root: Path,
    corpus: dict[str, object],
) -> BrandOutcome:
    """Build the per-brand plan WITHOUT writing anything.

    Returns a ``BrandOutcome`` with ``status='dry-run'`` and the count of
    assets the seed would touch.
    """
    library_slug = normalize_library_slug(brand_dir.name)
    pairs = list(filter_assets(iter_assets(corpus), brand_dir.name, None))
    plan = plan_only(iter(pairs), drl_root, None)
    return BrandOutcome(
        brand_dir=brand_dir.name,
        library_slug=library_slug,
        corpus_system_slug=brand_dir.name,
        asset_count_planned=len(plan),
        inserted=0,
        updated=0,
        skipped=0,
        status="dry-run" if pairs else "skipped",
        error=None if pairs else "no matching corpus system slug",
    )


def process_brand_apply(
    brand_dir: Path,
    drl_root: Path,
    corpus: dict[str, object],
    session: "Session",
    storage: "StorageClient",
    seed_user_id: int,
    batch_size: int,
) -> BrandOutcome:
    """Run the seed for one brand and return the per-brand counts."""
    library_slug = normalize_library_slug(brand_dir.name)
    captured_date = str(corpus.get("generated") or "unknown")
    pairs = list(filter_assets(iter_assets(corpus), brand_dir.name, None))
    if not pairs:
        return BrandOutcome(
            brand_dir=brand_dir.name,
            library_slug=library_slug,
            corpus_system_slug=brand_dir.name,
            asset_count_planned=0,
            inserted=0,
            updated=0,
            skipped=0,
            status="skipped",
            error="no matching corpus system slug",
        )
    try:
        counts = apply_seed(
            iter(pairs),
            drl_root,
            session,
            storage,
            seed_user_id=seed_user_id,
            batch_size=batch_size,
            captured_date=captured_date,
        )
    except Exception as exc:  # surface and continue with next brand
        LOG.exception("seed failed for brand %s", brand_dir.name)
        return BrandOutcome(
            brand_dir=brand_dir.name,
            library_slug=library_slug,
            corpus_system_slug=brand_dir.name,
            asset_count_planned=len(pairs),
            inserted=0,
            updated=0,
            skipped=0,
            status="failed",
            error=str(exc),
        )
    return BrandOutcome(
        brand_dir=brand_dir.name,
        library_slug=library_slug,
        corpus_system_slug=brand_dir.name,
        asset_count_planned=len(pairs),
        inserted=counts["inserted"],
        updated=counts["updated"],
        skipped=counts["skipped"],
        status="ok",
        error=None,
    )


def aggregate_report(
    drl_root: Path,
    discovered: int,
    outcomes: list[BrandOutcome],
) -> BootstrapReport:
    """Roll per-brand outcomes into a single ``BootstrapReport``."""
    report = BootstrapReport(
        drl_root=str(drl_root),
        brands_discovered=discovered,
        brands_processed=len(outcomes),
        outcomes=outcomes,
    )
    for outcome in outcomes:
        report.totals_inserted += outcome["inserted"]
        report.totals_updated += outcome["updated"]
        report.totals_skipped += outcome["skipped"]
        if outcome["status"] == "failed":
            report.failed_brands.append(outcome["brand_dir"])
    return report


# --- Verify-only -------------------------------------------------------------

def run_verify_only(session: "Session") -> dict[str, int]:
    """Query the DB for DRL bootstrap state without writing anything.

    Returns a counts dict with the DRL asset_versions row count, distinct
    brand-slug count (derived from extractions.source_id prefix), library
    page row count, and library_index_jobs status breakdown.
    """
    from sqlalchemy import func, select  # local import: dry-run safety

    from app.models import AssetVersion, Extraction, LibraryIndexJob, LibraryPage

    counts: dict[str, int] = {}
    counts["asset_versions_drl"] = session.execute(
        select(func.count(AssetVersion.id)).where(
            AssetVersion.version_label.like(f"{DRL_VERSION_LABEL_PREFIX}%")
        )
    ).scalar_one()
    counts["extractions_drl"] = session.execute(
        select(func.count(Extraction.id)).where(
            Extraction.seed_source == ASSET_VERSIONS_SEED_SOURCE_LABEL
        )
    ).scalar_one()
    counts["library_pages"] = session.execute(
        select(func.count(LibraryPage.id))
    ).scalar_one()
    for status_value in ("pending", "running", "complete", "failed"):
        counts[f"jobs_{status_value}"] = session.execute(
            select(func.count(LibraryIndexJob.id)).where(
                LibraryIndexJob.status == status_value
            )
        ).scalar_one()
    # Distinct brand-slug count derived from source_id prefix
    # ("<system>/<class>/<slug>"); first segment is the brand.
    source_ids = (
        session.execute(
            select(Extraction.source_id).where(
                Extraction.seed_source == ASSET_VERSIONS_SEED_SOURCE_LABEL
            )
        )
        .scalars()
        .all()
    )
    counts["distinct_brand_slugs"] = len(
        {sid.split("/", 1)[0] for sid in source_ids if sid}
    )
    return counts


# --- Logging -----------------------------------------------------------------

def _configure_logging() -> None:
    """Attach a stderr handler unless one is already present."""
    if not LOG.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)


def log_report(report: BootstrapReport, mode: str) -> None:
    """Emit the per-brand and aggregate lines for a completed run."""
    LOG.info(
        "%s: discovered=%d processed=%d inserted=%d updated=%d skipped=%d failed=%d",
        mode,
        report.brands_discovered,
        report.brands_processed,
        report.totals_inserted,
        report.totals_updated,
        report.totals_skipped,
        len(report.failed_brands),
    )
    for outcome in report.outcomes:
        LOG.info(
            "  brand=%s slug=%s status=%s planned=%d inserted=%d updated=%d skipped=%d err=%s",
            outcome["brand_dir"],
            outcome["library_slug"],
            outcome["status"],
            outcome["asset_count_planned"],
            outcome["inserted"],
            outcome["updated"],
            outcome["skipped"],
            outcome["error"] or "",
        )


# --- Entry point -------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    _configure_logging()
    args = parse_args(argv)
    LOG.info(
        "bootstrap_drl_library starting: apply=%s verify_only=%s drl_root=%s single=%s limit=%s",
        args.apply,
        args.verify_only,
        args.drl_root,
        args.single,
        args.limit,
    )

    if args.verify_only:
        # Lazy DB import: dry-run callers without DB access never pay the cost.
        from app.db import SessionLocal

        with SessionLocal() as session:
            counts = run_verify_only(session)
        LOG.info("verify-only state: %s", counts)
        if counts["distinct_brand_slugs"] < DRL_BOOTSTRAP_MIN_EXPECTED_BRANDS:
            LOG.warning(
                "distinct_brand_slugs=%d below expected floor %d",
                counts["distinct_brand_slugs"],
                DRL_BOOTSTRAP_MIN_EXPECTED_BRANDS,
            )
        return EXIT_OK

    brand_dirs = discover_brand_dirs(args.drl_root)
    selected = select_brands(brand_dirs, args.single, args.limit)
    LOG.info(
        "discovered %d brand dir(s); selected %d after filters",
        len(brand_dirs),
        len(selected),
    )
    corpus = load_corpus(args.drl_root)

    outcomes: list[BrandOutcome] = []
    if not args.apply:
        for brand_dir in selected:
            outcomes.append(process_brand_dry_run(brand_dir, args.drl_root, corpus))
        report = aggregate_report(args.drl_root, len(brand_dirs), outcomes)
        log_report(report, mode="DRY RUN")
        return EXIT_OK if not report.failed_brands else EXIT_ERROR

    # Apply path: lazy import so dry-run does not require DB reachability.
    from app.config import get_settings
    from app.db import SessionLocal

    from scripts.seed_from_drl import _R2SeedAdapter  # type: ignore[attr-defined]

    storage = _R2SeedAdapter(get_settings())
    with SessionLocal() as session:
        for brand_dir in selected:
            outcomes.append(
                process_brand_apply(
                    brand_dir,
                    args.drl_root,
                    corpus,
                    session,
                    storage,
                    args.seed_user_id,
                    args.batch_size,
                )
            )
    report = aggregate_report(args.drl_root, len(brand_dirs), outcomes)
    log_report(report, mode="APPLY")
    return EXIT_OK if not report.failed_brands else EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())

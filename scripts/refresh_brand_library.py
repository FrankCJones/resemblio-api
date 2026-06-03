"""Refresh one or every brand's library pages: drop + bootstrap + drain.

Why this script exists
----------------------
After a snapshot capture (`capture_all_button_snapshots.py`) writes
a new computed-style file for a brand, the existing `library_pages`
rows for that brand still hold the old composed HTML. The override
only applies during compose, not on read. To make the new tokens
visible we must:

1. Drop the brand's `library_pages` rows
2. Drop matching `library_index_jobs` rows (so the indexer re-enqueues
   work cleanly rather than thinking the job is already complete)
3. Re-bootstrap the brand via `scripts.bootstrap_drl_library` (which
   re-seeds `asset_versions` and enqueues fresh indexer jobs)
4. Drain the indexer in a loop until `jobs_run=0`

This script automates the loop end-to-end so a corpus-wide refresh
is one command instead of 24 manual sequences.

Per-brand isolation: a failing brand reports `status="failed"` and the
next brand still runs.

Run commands
------------
::

    # Dry-run for one brand.
    python -m scripts.refresh_brand_library --brand apple

    # Apply for one brand.
    python -m scripts.refresh_brand_library --brand apple --apply

    # Apply for every brand (uses the DRL root the bootstrap is configured for).
    python -m scripts.refresh_brand_library --all --apply

    # Override the DRL root passed through to the bootstrap subprocess.
    python -m scripts.refresh_brand_library --all --apply --drl-root /opt/resemblio-api/drl

Authorization
-------------
- Local development: GREEN
- Production execution: requires Frank approval per AUTHORITY.yml
  (DB mutations + indexer drain are production-state changes)

Quality floor: docstrings, TypedDict outcomes, schema_version on the
JSON output, named constants, subprocess error propagation.
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, TypedDict

_API_ROOT = Path(__file__).resolve().parents[1]
_path_text = str(_API_ROOT)
if _path_text not in sys.path:
    sys.path.insert(0, _path_text)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

LOG = logging.getLogger("refresh_brand_library")
LOG.propagate = True

# --- Constants ---------------------------------------------------------------

EXIT_OK = 0
EXIT_ERROR = 1

DRAIN_MAX_PASSES = 200
"""Hard cap on indexer drain passes (each pass = up to 10 jobs per OPS.md 8.7)."""

BOOTSTRAP_MODULE = "scripts.bootstrap_drl_library"
INDEXER_MODULE = "app.cli.library_indexer"

DRAIN_DONE_MARKER = "jobs_run=0"
"""Marker line emitted by the indexer when the queue is empty."""

REFRESH_SCHEMA_VERSION = 1
"""Bumped if the per-brand outcome JSON shape changes."""

# --- Typed shapes ------------------------------------------------------------


class RefreshOutcome(TypedDict):
    """Per-brand outcome of a refresh cycle."""

    brand: str
    status: str  # "ok" | "failed" | "dry-run"
    pages_deleted: int
    jobs_deleted: int
    bootstrap_exit: int
    drain_passes: int
    pages_after: int
    error: str | None


@dataclass
class RefreshReport:
    """Aggregate report across every brand the script touched."""

    brands_processed: int
    outcomes: list[RefreshOutcome] = field(default_factory=list)
    ok: int = 0
    failed: int = 0


@dataclass(frozen=True)
class RefreshArgs:
    """Parsed CLI arguments."""

    apply: bool
    brand: str | None
    all_brands: bool
    drl_root: Path | None
    drain_max_passes: int


# --- Brand discovery ---------------------------------------------------------


def list_brands_from_db(session: "Session") -> list[str]:
    """Return every distinct brand slug currently present in `library_pages`.

    Used as the source for ``--all``: only brands that already have rows
    are refreshed. New brands are picked up by `bootstrap_drl_library`
    on the next bootstrap pass; this script is for re-composing.
    """
    from sqlalchemy import select

    from app.models import LibraryPage

    rows = session.execute(select(LibraryPage.brand_slug).distinct()).scalars().all()
    return sorted({row for row in rows if row})


# --- Per-brand mutations -----------------------------------------------------


def delete_brand_rows(session: "Session", brand: str) -> tuple[int, int]:
    """Delete `library_pages` and matching `library_index_jobs` for one brand.

    Returns ``(pages_deleted, jobs_deleted)``. The job-deletion query
    matches via the `asset_versions.url LIKE '%/<brand>/%'` join the
    indexer uses to associate jobs with brands.
    """
    from sqlalchemy import delete, select

    from app.models import AssetVersion, LibraryIndexJob, LibraryPage

    pages_result = session.execute(
        delete(LibraryPage).where(LibraryPage.brand_slug == brand)
    )
    pages_deleted = int(pages_result.rowcount or 0)

    asset_ids = (
        session.execute(
            select(AssetVersion.id).where(AssetVersion.url.like(f"%/{brand}/%"))
        )
        .scalars()
        .all()
    )
    jobs_deleted = 0
    if asset_ids:
        jobs_result = session.execute(
            delete(LibraryIndexJob).where(LibraryIndexJob.asset_version_id.in_(asset_ids))
        )
        jobs_deleted = int(jobs_result.rowcount or 0)

    session.commit()
    return pages_deleted, jobs_deleted


def count_brand_pages(session: "Session", brand: str) -> int:
    """Row count of `library_pages` for one brand (post-refresh verification)."""
    from sqlalchemy import func, select

    from app.models import LibraryPage

    return int(
        session.execute(
            select(func.count(LibraryPage.id)).where(LibraryPage.brand_slug == brand)
        ).scalar_one()
    )


# --- Subprocess shells -------------------------------------------------------


SubprocessRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]
"""Signature of the subprocess runner (override in tests)."""


def _default_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and capture output as text. No shell."""
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def run_bootstrap(brand: str, drl_root: Path | None, runner: SubprocessRunner) -> int:
    """Run `bootstrap_drl_library --apply --single <brand>`. Returns exit code."""
    cmd = [sys.executable, "-m", BOOTSTRAP_MODULE, "--apply", "--single", brand]
    if drl_root is not None:
        cmd.extend(["--drl-root", str(drl_root)])
    result = runner(cmd)
    if result.stdout:
        LOG.info("bootstrap stdout: %s", result.stdout.strip()[:500])
    if result.stderr:
        LOG.info("bootstrap stderr: %s", result.stderr.strip()[:500])
    return int(result.returncode)


def drain_indexer(runner: SubprocessRunner, max_passes: int) -> int:
    """Drain `library_indexer` in a loop until `jobs_run=0` or cap reached.

    Returns the pass count executed. The OPS.md 8.7 pattern: each
    invocation processes up to 10 jobs; loop until the marker appears.
    """
    cmd = [sys.executable, "-m", INDEXER_MODULE]
    for i in range(1, max_passes + 1):
        result = runner(cmd)
        out = (result.stdout or "") + "\n" + (result.stderr or "")
        last_line = out.strip().splitlines()[-1] if out.strip() else ""
        LOG.info("drain pass %d: %s", i, last_line[:200])
        if DRAIN_DONE_MARKER in out:
            return i
    LOG.warning("drain hit max_passes=%d without seeing %s", max_passes, DRAIN_DONE_MARKER)
    return max_passes


# --- Per-brand orchestration -------------------------------------------------


def refresh_one_brand(
    brand: str,
    session: "Session",
    *,
    drl_root: Path | None,
    runner: SubprocessRunner,
    drain_max_passes: int,
) -> RefreshOutcome:
    """End-to-end refresh for one brand. Per-brand exception isolation."""
    try:
        pages_deleted, jobs_deleted = delete_brand_rows(session, brand)
    except Exception as exc:  # noqa: BLE001
        LOG.exception("delete failed for %s", brand)
        return RefreshOutcome(
            brand=brand,
            status="failed",
            pages_deleted=0,
            jobs_deleted=0,
            bootstrap_exit=-1,
            drain_passes=0,
            pages_after=-1,
            error=f"delete: {type(exc).__name__}: {str(exc)[:200]}",
        )
    bootstrap_exit = run_bootstrap(brand, drl_root, runner)
    if bootstrap_exit != 0:
        return RefreshOutcome(
            brand=brand,
            status="failed",
            pages_deleted=pages_deleted,
            jobs_deleted=jobs_deleted,
            bootstrap_exit=bootstrap_exit,
            drain_passes=0,
            pages_after=-1,
            error=f"bootstrap exit={bootstrap_exit}",
        )
    passes = drain_indexer(runner, drain_max_passes)
    try:
        pages_after = count_brand_pages(session, brand)
    except Exception as exc:  # noqa: BLE001
        LOG.exception("count failed for %s", brand)
        return RefreshOutcome(
            brand=brand,
            status="failed",
            pages_deleted=pages_deleted,
            jobs_deleted=jobs_deleted,
            bootstrap_exit=bootstrap_exit,
            drain_passes=passes,
            pages_after=-1,
            error=f"count: {type(exc).__name__}: {str(exc)[:200]}",
        )
    return RefreshOutcome(
        brand=brand,
        status="ok",
        pages_deleted=pages_deleted,
        jobs_deleted=jobs_deleted,
        bootstrap_exit=bootstrap_exit,
        drain_passes=passes,
        pages_after=pages_after,
        error=None,
    )


def aggregate(outcomes: list[RefreshOutcome]) -> RefreshReport:
    """Roll per-brand outcomes into a single ``RefreshReport``."""
    report = RefreshReport(brands_processed=len(outcomes), outcomes=outcomes)
    for o in outcomes:
        if o["status"] == "ok":
            report.ok += 1
        elif o["status"] == "failed":
            report.failed += 1
    return report


# --- CLI ---------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> RefreshArgs:
    """Parse argv into ``RefreshArgs``. Dry-run is the default."""
    parser = argparse.ArgumentParser(
        description="Drop + bootstrap + drain one or every brand's library pages."
    )
    parser.add_argument("--apply", action="store_true", help="Actually mutate. Default is dry-run.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--brand", type=str, default=None, help="Refresh one brand by slug.")
    group.add_argument("--all", dest="all_brands", action="store_true", help="Refresh every brand.")
    parser.add_argument(
        "--drl-root",
        type=Path,
        default=None,
        help="DRL root passed through to bootstrap_drl_library (default: bootstrap's own default).",
    )
    parser.add_argument(
        "--drain-max-passes",
        type=int,
        default=DRAIN_MAX_PASSES,
        help=f"Max drain loop iterations (default {DRAIN_MAX_PASSES}).",
    )
    namespace = parser.parse_args(argv)
    return RefreshArgs(
        apply=bool(namespace.apply),
        brand=namespace.brand,
        all_brands=bool(namespace.all_brands),
        drl_root=Path(namespace.drl_root).resolve() if namespace.drl_root else None,
        drain_max_passes=int(namespace.drain_max_passes),
    )


# --- Logging -----------------------------------------------------------------


def _configure_logging() -> None:
    """Attach a stderr handler unless one is already present."""
    if not LOG.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)


def log_report(report: RefreshReport, mode: str) -> None:
    """Emit per-brand + aggregate lines for a completed run."""
    LOG.info(
        "%s: processed=%d ok=%d failed=%d", mode, report.brands_processed, report.ok, report.failed
    )
    for o in report.outcomes:
        LOG.info(
            "  brand=%s status=%s pages_deleted=%d jobs_deleted=%d boot_exit=%d drain_passes=%d pages_after=%d err=%s",
            o["brand"],
            o["status"],
            o["pages_deleted"],
            o["jobs_deleted"],
            o["bootstrap_exit"],
            o["drain_passes"],
            o["pages_after"],
            o["error"] or "",
        )


# --- Entry point -------------------------------------------------------------


def run(
    args: RefreshArgs,
    *,
    session_factory: Callable[[], "Session"] | None = None,
    runner: SubprocessRunner | None = None,
    brand_lister: Callable[["Session"], list[str]] | None = None,
) -> RefreshReport:
    """End-to-end orchestration. Pure: tests inject runner + session_factory."""
    if not args.apply:
        # Dry-run uses a stub outcome; we still resolve targets so the log
        # shows what WOULD be touched.
        targets = [args.brand] if args.brand else ["<all-brands-resolved-at-apply-time>"]
        outcomes = [
            RefreshOutcome(
                brand=b or "",
                status="dry-run",
                pages_deleted=0,
                jobs_deleted=0,
                bootstrap_exit=0,
                drain_passes=0,
                pages_after=-1,
                error=None,
            )
            for b in targets
        ]
        return aggregate(outcomes)

    if session_factory is None:
        from app.db import SessionLocal

        session_factory = SessionLocal  # type: ignore[assignment]
    sub_runner = runner or _default_runner
    lister = brand_lister or list_brands_from_db

    outcomes: list[RefreshOutcome] = []
    with session_factory() as session:  # type: ignore[misc]
        if args.brand:
            brands = [args.brand]
        else:
            brands = lister(session)
            LOG.info("--all resolved to %d brands: %s", len(brands), ", ".join(brands))
        for brand in brands:
            outcomes.append(
                refresh_one_brand(
                    brand,
                    session,
                    drl_root=args.drl_root,
                    runner=sub_runner,
                    drain_max_passes=args.drain_max_passes,
                )
            )
    return aggregate(outcomes)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    _configure_logging()
    args = parse_args(argv)
    LOG.info(
        "refresh_brand_library starting: apply=%s brand=%s all=%s drl_root=%s",
        args.apply,
        args.brand,
        args.all_brands,
        args.drl_root,
    )
    report = run(args)
    log_report(report, mode="APPLY" if args.apply else "DRY RUN")
    # Emit a machine-readable summary for CI / parent automation.
    summary = {
        "schema_version": REFRESH_SCHEMA_VERSION,
        "ok": report.ok,
        "failed": report.failed,
        "brands": [o["brand"] for o in report.outcomes],
    }
    print(json.dumps(summary))
    return EXIT_OK if report.failed == 0 else EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())

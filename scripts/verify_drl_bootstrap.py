"""Verification harness for the DRL library bootstrap (mission Phase 8).

Queries the Postgres state populated by ``bootstrap_drl_library`` plus the
library indexer (Phase 4) and produces a structured Markdown report under
``projects/Resemblio/_handoff/inbox/claude/`` for Jim to read.

Exit code is non-zero when:

- The number of distinct DRL-tagged brand slugs is below the mission floor
  (``DRL_BOOTSTRAP_MIN_EXPECTED_BRANDS``).
- Any ``library_index_jobs`` row is in ``failed`` state.

Usage
-----
::

    python -m scripts.verify_drl_bootstrap
    python -m scripts.verify_drl_bootstrap --report-dir /tmp/reports
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

_API_ROOT = Path(__file__).resolve().parents[1]
_path_text = str(_API_ROOT)
if _path_text not in sys.path:
    sys.path.insert(0, _path_text)

from app.constants import (
    ASSET_VERSIONS_SEED_SOURCE_LABEL,
    DRL_BOOTSTRAP_EXPECTED_PAGES_PER_BRAND,
    DRL_BOOTSTRAP_MIN_EXPECTED_BRANDS,
    DRL_BOOTSTRAP_REPORT_SCHEMA_VERSION,
)
from scripts.seed_from_drl import DRL_VERSION_LABEL_PREFIX  # noqa: E402

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

LOG = logging.getLogger("verify_drl_bootstrap")
LOG.propagate = True

EXIT_OK = 0
EXIT_ERROR = 1

DEFAULT_REPORT_DIR = (
    Path(__file__).resolve().parents[3] / "_handoff" / "inbox" / "claude"
)
"""Default landing directory for the Markdown verification report."""


class VerifyResult(TypedDict):
    """Structured result the report renderer + tests consume."""

    schema_version: int
    generated_at: str
    asset_versions_drl: int
    extractions_drl: int
    distinct_brand_slugs: int
    library_pages_total: int
    library_pages_by_brand: dict[str, int]
    jobs_by_status: dict[str, int]
    quality_gate_eligible: int
    quality_gate_filtered: int
    expectations_met: bool
    expectation_failures: list[str]


@dataclass(frozen=True)
class VerifyArgs:
    """Parsed CLI arguments for the verifier."""

    report_dir: Path


def parse_args(argv: list[str] | None = None) -> VerifyArgs:
    """Parse argv into ``VerifyArgs``."""
    parser = argparse.ArgumentParser(
        description="Verify DRL library bootstrap state and write a Markdown report."
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=DEFAULT_REPORT_DIR,
        help="Directory the Markdown report is written to.",
    )
    namespace = parser.parse_args(argv)
    return VerifyArgs(report_dir=Path(namespace.report_dir).resolve())


def collect_state(session: "Session") -> VerifyResult:
    """Query the DB and return a structured ``VerifyResult``."""
    from sqlalchemy import func, select  # local import

    from app.models import AssetVersion, Extraction, LibraryIndexJob, LibraryPage

    asset_versions_drl = session.execute(
        select(func.count(AssetVersion.id)).where(
            AssetVersion.version_label.like(f"{DRL_VERSION_LABEL_PREFIX}%")
        )
    ).scalar_one()
    extractions_drl_rows = (
        session.execute(
            select(Extraction.source_id).where(
                Extraction.seed_source == ASSET_VERSIONS_SEED_SOURCE_LABEL
            )
        )
        .scalars()
        .all()
    )
    distinct_brand_slugs = {sid.split("/", 1)[0] for sid in extractions_drl_rows if sid}

    library_pages_total = session.execute(
        select(func.count(LibraryPage.id))
    ).scalar_one()
    pages_by_brand_rows = session.execute(
        select(LibraryPage.brand_slug, func.count(LibraryPage.id)).group_by(
            LibraryPage.brand_slug
        )
    ).all()
    library_pages_by_brand = {row[0]: int(row[1]) for row in pages_by_brand_rows}

    jobs_by_status: dict[str, int] = {}
    for status_value in ("pending", "running", "complete", "failed"):
        jobs_by_status[status_value] = session.execute(
            select(func.count(LibraryIndexJob.id)).where(
                LibraryIndexJob.status == status_value
            )
        ).scalar_one()

    # Quality-gate filter: a DRL-tagged asset_version is "eligible" when it
    # is the source of at least one LibraryPage row (i.e. the indexer ran +
    # the page passed the gate). "Filtered out" = a DRL asset_version with
    # no library_pages row attached.
    drl_asset_version_ids = (
        session.execute(
            select(AssetVersion.id).where(
                AssetVersion.version_label.like(f"{DRL_VERSION_LABEL_PREFIX}%")
            )
        )
        .scalars()
        .all()
    )
    ids_with_pages = (
        session.execute(
            select(LibraryPage.asset_version_id).where(
                LibraryPage.asset_version_id.in_(drl_asset_version_ids)
            )
        )
        .scalars()
        .all()
    ) if drl_asset_version_ids else []
    eligible = len(set(ids_with_pages))
    filtered = len(drl_asset_version_ids) - eligible

    failures: list[str] = []
    if len(distinct_brand_slugs) < DRL_BOOTSTRAP_MIN_EXPECTED_BRANDS:
        failures.append(
            f"distinct_brand_slugs={len(distinct_brand_slugs)} below floor "
            f"{DRL_BOOTSTRAP_MIN_EXPECTED_BRANDS}"
        )
    if jobs_by_status.get("failed", 0) > 0:
        failures.append(
            f"library_index_jobs.failed={jobs_by_status['failed']} > 0"
        )

    return VerifyResult(
        schema_version=DRL_BOOTSTRAP_REPORT_SCHEMA_VERSION,
        generated_at=datetime.now(timezone.utc).isoformat(),
        asset_versions_drl=int(asset_versions_drl),
        extractions_drl=len(extractions_drl_rows),
        distinct_brand_slugs=len(distinct_brand_slugs),
        library_pages_total=int(library_pages_total),
        library_pages_by_brand=library_pages_by_brand,
        jobs_by_status=jobs_by_status,
        quality_gate_eligible=eligible,
        quality_gate_filtered=filtered,
        expectations_met=not failures,
        expectation_failures=failures,
    )


def render_report(result: VerifyResult) -> str:
    """Render a ``VerifyResult`` as a Markdown report string."""
    lines: list[str] = []
    lines.append("# DRL bootstrap verification")
    lines.append("")
    lines.append(f"- schema_version: `{result['schema_version']}`")
    lines.append(f"- generated_at: `{result['generated_at']}`")
    lines.append(f"- expectations_met: `{result['expectations_met']}`")
    if result["expectation_failures"]:
        lines.append("")
        lines.append("## Expectation failures")
        for failure in result["expectation_failures"]:
            lines.append(f"- {failure}")
    lines.append("")
    lines.append("## Counts")
    lines.append("")
    lines.append(f"- asset_versions (DRL-tagged): **{result['asset_versions_drl']}**")
    lines.append(f"- extractions (seed_source=drl_v1): **{result['extractions_drl']}**")
    lines.append(f"- distinct brand slugs: **{result['distinct_brand_slugs']}**")
    lines.append(f"- library_pages total: **{result['library_pages_total']}**")
    lines.append("")
    lines.append("## Indexer jobs by status")
    lines.append("")
    for status_value, count in result["jobs_by_status"].items():
        lines.append(f"- {status_value}: {count}")
    lines.append("")
    lines.append("## Quality gate")
    lines.append("")
    lines.append(f"- eligible (asset_version has at least one library_page): {result['quality_gate_eligible']}")
    lines.append(f"- filtered out (asset_version with zero library_pages): {result['quality_gate_filtered']}")
    lines.append("")
    lines.append("## Library pages per brand")
    lines.append("")
    if not result["library_pages_by_brand"]:
        lines.append("_no library_pages rows yet — the indexer has not drained_")
    else:
        lines.append("| brand_slug | pages | meets floor |")
        lines.append("|---|---:|:---:|")
        for brand, count in sorted(result["library_pages_by_brand"].items()):
            meets = "yes" if count >= DRL_BOOTSTRAP_EXPECTED_PAGES_PER_BRAND else "no"
            lines.append(f"| {brand} | {count} | {meets} |")
    lines.append("")
    return "\n".join(lines)


def write_report(report_text: str, report_dir: Path) -> Path:
    """Write the Markdown report under ``report_dir`` with a dated filename."""
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = report_dir / f"{stamp}-drl-bootstrap-verify.md"
    path.write_text(report_text, encoding="utf-8")
    return path


def _configure_logging() -> None:
    """Attach a stderr handler unless one is already present."""
    if not LOG.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    _configure_logging()
    args = parse_args(argv)
    from app.db import SessionLocal  # lazy: tests inject session directly

    with SessionLocal() as session:
        result = collect_state(session)
    report_text = render_report(result)
    out_path = write_report(report_text, args.report_dir)
    LOG.info(
        "verify report written to %s; expectations_met=%s",
        out_path,
        result["expectations_met"],
    )
    if not result["expectations_met"]:
        for failure in result["expectation_failures"]:
            LOG.error("expectation failure: %s", failure)
        return EXIT_ERROR
    return EXIT_OK


# Re-export Counter so tests can patch counting paths if needed; reference
# guards against unused-import lint without functional impact.
_ = Counter

if __name__ == "__main__":
    raise SystemExit(main())

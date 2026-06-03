"""CLI wrapper for the Resemblio link-crawl smoke gate.

Purpose
-------
Standing PR gate that runs AFTER the deploy succeeds. Crawls every registered
surface in `projects/Resemblio/surfaces.yml`, asserts every internal link
returns 200 (or a documented 301), writes a JSON report, and exits 0 on
clean / 1 on any failure.

Why this gate exists
--------------------
On 2026-06-02 the Library v1.1 deploy went green and 500'd every metadata
route. The same shape bit Susann WP staging (nav links 404'd post-deploy
even though the homepage rendered clean). Status-only smoke (/v1/healthz +
/v1/readyz) cannot catch link-shape regressions because the smoke routes
themselves were healthy. This gate is the structural fix: a green deploy
is only green if every advertised surface and every link inside those
surfaces resolves.

Usage
-----
    python scripts/link_crawl_smoke.py [--surfaces PATH] [--report PATH]

Defaults: surfaces from `../surfaces.yml` (workspace-relative);
report written to `_smoke_logs/link_crawl_<UTC>.json`.

Exit codes
----------
- 0: every link passed; deploy is green by this gate.
- 1: at least one link failed; deploy is NOT green; the workflow's
     `needs:` chain halts subsequent steps and the failure surface is
     the printed report.
- 2: operator-actionable bug (surfaces.yml unreadable, dependency missing).

Schema
------
Report file: `schema_version=link_crawl_report_v1` (see
`app.monitoring.link_crawl.REPORT_SCHEMA_VERSION`).

Quality floor notes
-------------------
- Logger configured; this runs unattended in CI.
- Network retries via the underlying module's FETCH_RETRY_DELAYS_SEC.
- All path constants flow from env/CLI with safe defaults.
- Single dashes only; per workspace CLAUDE.md.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import sys
from pathlib import Path

from app.monitoring.link_crawl import (
    REPORT_SCHEMA_VERSION,
    crawl_surfaces,
    load_surfaces_yaml,
    report_to_dict,
)

# Default location of the canonical surfaces registry. Relative to this
# script's directory; the script lives at `code/api/scripts/`, the registry
# at `projects/Resemblio/surfaces.yml`, so go up 3 levels from this file.
DEFAULT_SURFACES_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent / "surfaces.yml"
)

# Default output dir for the JSON report. Matches the `_smoke_logs/` directory
# already in the api project root.
DEFAULT_REPORT_DIR = Path(__file__).resolve().parent.parent / "_smoke_logs"

logger = logging.getLogger("resemblio.link_crawl.cli")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Resemblio link-crawl smoke gate once.",
    )
    parser.add_argument(
        "--surfaces",
        type=Path,
        default=DEFAULT_SURFACES_PATH,
        help=f"Path to surfaces.yml (default: {DEFAULT_SURFACES_PATH})",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help=(
            "Path to write the JSON report. Default: "
            "_smoke_logs/link_crawl_<UTC>.json"
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Log per-link results to stdout as the crawl runs.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. See `Exit codes` in module docstring."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if not args.surfaces.exists():
        logger.error("surfaces.yml not found at %s", args.surfaces)
        return 2

    try:
        surfaces = load_surfaces_yaml(args.surfaces.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 -- operator surface; want full reason
        logger.error("failed to parse surfaces.yml: %s", exc)
        return 2

    if not surfaces:
        logger.error("no surfaces declared in %s", args.surfaces)
        return 2

    logger.info(
        "crawling %d surfaces (%d routes total)",
        len(surfaces),
        sum(len(s.routes) for s in surfaces),
    )

    report = crawl_surfaces(surfaces)

    # Determine output path; default carries a UTC timestamp so consecutive
    # runs do not clobber each other.
    if args.report is None:
        DEFAULT_REPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        report_path = DEFAULT_REPORT_DIR / f"link_crawl_{stamp}.json"
    else:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        report_path = args.report

    report_path.write_text(
        json.dumps(report_to_dict(report), indent=2, sort_keys=True),
        encoding="utf-8",
    )

    # Summary line for the CI log.
    logger.info(
        "link-crawl smoke: surfaces=%d found=%d internal=%d passed=%d failed=%d (schema=%s)",
        report.surfaces_crawled,
        report.total_links_found,
        report.total_internal_links,
        report.total_passed,
        report.total_failed,
        REPORT_SCHEMA_VERSION,
    )
    logger.info("report written to %s", report_path)

    # Print per-link failures so the CI log shows the diagnostic without
    # requiring an artifact download. Successes are summarized only.
    if report.total_failed > 0:
        print("=== link-crawl FAILURES ===", file=sys.stderr)
        for row in report.results:
            if not row.passed:
                print(
                    f"  source={row.source_url}\n"
                    f"  link={row.link_url}\n"
                    f"  status={row.status}  error={row.error}",
                    file=sys.stderr,
                )

    if args.verbose:
        for row in report.results:
            tag = "OK" if row.passed else "FAIL"
            print(f"[{tag}] {row.status} {row.link_url}")

    return report.exit_code


if __name__ == "__main__":  # pragma: no cover - module-as-script entry
    sys.exit(main())

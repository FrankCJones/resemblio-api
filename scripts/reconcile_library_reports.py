"""Read two saved LibraryAssertionReport JSON files, run the reconciliation engine,
and write a schema-versioned reconciliation.json + reconciliation.md to --out-dir.

Purpose
-------
Phase D of the Library v4 re-seed handoff: after the gated re-seed runs, call this
script with the offline preflight report (saved at Phase C step 3) and the live
assertion report produced by ``generate_library_assertion_report.py``.  Exit code 0
means the live library matches what preflight predicted (``reconciled=True``); exit
code 1 means the reports diverged - inspect ``reconciliation.json`` for the specific
discrepancies.

This script is the I/O companion to ``generate_library_assertion_report.py``.  It
has the same structural shape: load inputs, call a pure engine, write JSON + Markdown,
return a meaningful exit code.

Usage
-----
::

    python -m scripts.reconcile_library_reports \\
        --predicted 02-prd/2026-06-10-library-v4-live-assertion-report/preflight-predicted.json \\
        --actual   02-prd/2026-06-10-library-v4-live-assertion-report/assertion-report.json \\
        --out-dir  02-prd/2026-06-10-library-v4-live-assertion-report

Dependencies
------------
- ``app.library_reseed_verification`` (reconcile_reports, render_reconciliation_markdown)
- stdlib only (argparse, json, logging, pathlib, sys) - no network access

Output
------
``<out-dir>/reconciliation.json``
    Schema-versioned JSON result (``schema_version: library_reconciliation_v1``).
``<out-dir>/reconciliation.md``
    Markdown contact sheet for pasting into STATUS.md or a PR description.

Exit codes
----------
0 - ``reconciled: true`` (live library matches preflight prediction).
1 - ``reconciled: false`` (divergence detected; see reconciliation.json for details).
2 - IO or JSON error prevented the script from completing.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from app.library_reseed_verification import (
    reconcile_reports,
    render_reconciliation_markdown,
)

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    """Entry point.  Returns an exit code (0 reconciled, 1 diverged, 2 IO error)."""
    parser = argparse.ArgumentParser(
        description="Reconcile a preflight-predicted assertion report against a live actual report.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--predicted",
        required=True,
        help="Path to the offline preflight LibraryAssertionReport JSON file.",
    )
    parser.add_argument(
        "--actual",
        required=True,
        help="Path to the live post-re-seed LibraryAssertionReport JSON file.",
    )
    parser.add_argument(
        "--out-dir",
        default=".",
        help="Directory to write reconciliation.json and reconciliation.md (default: cwd).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    # Load predicted report.
    predicted_path = Path(args.predicted)
    try:
        predicted = json.loads(predicted_path.read_text(encoding="utf-8"))
    except OSError as exc:
        logger.error("Cannot read predicted report %s: %s", predicted_path, exc)
        return 2
    except json.JSONDecodeError as exc:
        logger.error("Malformed JSON in predicted report %s: %s", predicted_path, exc)
        return 2

    # Load actual report.
    actual_path = Path(args.actual)
    try:
        actual = json.loads(actual_path.read_text(encoding="utf-8"))
    except OSError as exc:
        logger.error("Cannot read actual report %s: %s", actual_path, exc)
        return 2
    except json.JSONDecodeError as exc:
        logger.error("Malformed JSON in actual report %s: %s", actual_path, exc)
        return 2

    # Run the reconciliation engine.
    result = reconcile_reports(predicted, actual)

    # A malformed_report note signals a wrong-shape input file (IO-class problem),
    # not a genuine divergence.  Map it to exit 2 to distinguish from verdicts and
    # to avoid a confusing "exit 1 / diverged" message for an operator mistake.
    if result["notes"].startswith("malformed_report:"):
        logger.error("Malformed report shape: %s", result["notes"])
        return 2

    # Write outputs.
    out_dir = Path(args.out_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("Cannot create output directory %s: %s", out_dir, exc)
        return 2

    json_path = out_dir / "reconciliation.json"
    md_path = out_dir / "reconciliation.md"

    try:
        json_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        md_path.write_text(render_reconciliation_markdown(result), encoding="utf-8")
    except OSError as exc:
        logger.error("Failed to write reconciliation output: %s", exc)
        return 2

    if result["reconciled"]:
        logger.info(
            "RECONCILED: predicted_count=%d actual_count=%d - live library matches preflight.",
            result["predicted_count"],
            result["actual_count"],
        )
        return 0

    logger.error(
        "DIVERGED: reconciled=False - verdict_drift=%d missing=%d unexpected=%d "
        "dup_predicted=%d dup_actual=%d notes=%r",
        len(result["verdict_drift"]),
        len(result["missing_in_actual"]),
        len(result["unexpected_in_actual"]),
        len(result["duplicate_in_predicted"]),
        len(result["duplicate_in_actual"]),
        result["notes"],
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())

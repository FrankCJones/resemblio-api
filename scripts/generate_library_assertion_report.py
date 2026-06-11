"""Fetch all brands from the live Resemblio Library API and produce a
schema-versioned assertion report (JSON + Markdown).

Purpose
-------
Phase 7 of the Library v4 re-seed handoff: run this script against the live
prod API after ``seed_from_drl --apply`` to prove the re-seed produced a
faithful library.  ``all_pass: True`` in the JSON output is the acceptance gate.

The engine (``app.library_assertion_report``) is fully tested offline against
synthetic fixtures in ``tests/test_library_assertion_report.py``.  This script
is the I/O harness that feeds the engine live data.

Usage
-----
::

    # Against prod (default)
    python -m scripts.generate_library_assertion_report \\
        --base-url https://api.resemblio.com \\
        --out-dir 02-prd/2026-06-10-library-v4-live-assertion-report

    # Against a local dev server
    python -m scripts.generate_library_assertion_report \\
        --base-url http://localhost:8000 \\
        --out-dir /tmp/assertion-test

Dependencies
------------
- ``RESEMBLIO_DB_URL`` and ``RESEMBLIO_KEY_PEPPER`` env vars are NOT required
  by this script; it reads only the public API, no DB access.
- ``requests`` (or ``httpx``) is NOT a declared dep; the script uses
  ``urllib.request`` so it works without any extra install.  Retry logic is
  implemented with ``urllib.error`` + exponential backoff.

Output
------
``<out-dir>/assertion-report.json``
    Schema-versioned JSON report (``schema_version: library_assertion_report_v1``).
``<out-dir>/assertion-report.md``
    Markdown contact sheet for pasting into STATUS.md or a PR description.

Exit codes
----------
0 - ``all_pass: True`` (no broken pages).
1 - ``all_pass: False`` (at least one broken page found).
2 - Network or I/O error prevented the report from being completed.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from app.library_assertion_report import build_report, render_markdown

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Number of times to retry a failed HTTP request before giving up.
_MAX_RETRIES: int = 3

#: Initial backoff in seconds; doubles on each retry.
_INITIAL_BACKOFF_S: float = 2.0

#: Timeout in seconds for each individual HTTP request.
_REQUEST_TIMEOUT_S: int = 15


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fetch_json(url: str) -> dict:
    """Fetch ``url`` and return the parsed JSON body.

    Retries up to ``_MAX_RETRIES`` times with exponential backoff on
    transient errors (HTTP 5xx or network failure).  Raises ``RuntimeError``
    if all retries are exhausted.

    Parameters
    ----------
    url:
        Full URL to fetch.  Must return a JSON body.

    Returns
    -------
    dict
        Parsed JSON response body.

    Raises
    ------
    RuntimeError
        When all retries are exhausted or a non-retriable HTTP error occurs.
    """
    backoff = _INITIAL_BACKOFF_S
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                # 4xx errors are not retriable.
                raise RuntimeError(f"HTTP {exc.code} fetching {url}") from exc
            logger.warning("HTTP %s fetching %s (attempt %d/%d)", exc.code, url, attempt, _MAX_RETRIES)
            last_exc = exc
        except (urllib.error.URLError, OSError) as exc:
            logger.warning("Network error fetching %s (attempt %d/%d): %s", url, attempt, _MAX_RETRIES, exc)
            last_exc = exc

        if attempt < _MAX_RETRIES:
            logger.info("Retrying in %.1fs...", backoff)
            time.sleep(backoff)
            backoff *= 2

    raise RuntimeError(f"All {_MAX_RETRIES} retries exhausted for {url}") from last_exc


def _fetch_all_brands(base_url: str) -> list[dict]:
    """Fetch every brand from the library hub and return individual brand responses.

    First calls ``GET /v1/library/brands`` to get the hub list (``data.featured``),
    then fetches ``GET /v1/library/brands/{slug}`` for each brand individually
    so the assertion engine receives the full per-brand response shape.

    Parameters
    ----------
    base_url:
        API base URL, e.g. ``"https://api.resemblio.com"``.  No trailing slash.

    Returns
    -------
    list[dict]
        One response dict per brand (the full ``GET /v1/library/brands/{slug}``
        response), in hub-list order.
    """
    hub_url = f"{base_url.rstrip('/')}/v1/library/brands"
    logger.info("Fetching hub brand list from %s", hub_url)
    hub = _fetch_json(hub_url)

    data = hub.get("data", {})
    featured = data.get("featured", []) if isinstance(data, dict) else []
    slugs = [b["brand_slug"] for b in featured if isinstance(b, dict) and b.get("brand_slug")]
    logger.info("Hub lists %d brands", len(slugs))

    responses: list[dict] = []
    for slug in slugs:
        url = f"{base_url.rstrip('/')}/v1/library/brands/{slug}"
        logger.info("Fetching brand %s", slug)
        responses.append(_fetch_json(url))

    return responses


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point.  Returns an exit code (0 = all_pass, 1 = failures found,
    2 = script-level error).
    """
    parser = argparse.ArgumentParser(
        description="Generate a schema-versioned library assertion report.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--base-url",
        default="https://api.resemblio.com",
        help="Resemblio API base URL (default: https://api.resemblio.com)",
    )
    parser.add_argument(
        "--out-dir",
        default="02-prd/library-assertion-report",
        help="Directory to write assertion-report.json and assertion-report.md",
    )
    parser.add_argument(
        "--source",
        default="prod",
        choices=("prod", "fixture"),
        help="Label for report.source field (default: prod)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    out_dir = Path(args.out_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("Cannot create output directory %s: %s", out_dir, exc)
        return 2

    try:
        responses = _fetch_all_brands(args.base_url)
    except RuntimeError as exc:
        logger.error("Failed to fetch brand data: %s", exc)
        return 2

    report = build_report(responses, source=args.source)

    json_path = out_dir / "assertion-report.json"
    md_path = out_dir / "assertion-report.md"

    try:
        json_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        md_path.write_text(render_markdown(report), encoding="utf-8")
    except OSError as exc:
        logger.error("Failed to write report: %s", exc)
        return 2

    logger.info(
        "Report written to %s (all_pass=%s, brand_count=%d)",
        out_dir,
        report["all_pass"],
        report["brand_count"],
    )

    if not report["all_pass"]:
        broken = [a["brand_slug"] for a in report["assertions"] if a["verdict"] == "page_broken"]
        logger.error(
            "FAIL: %d brand(s) are page_broken: %s",
            len(broken),
            ", ".join(broken),
        )
        return 1

    logger.info("PASS: all %d brands are faithful or cleanly absent.", report["brand_count"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

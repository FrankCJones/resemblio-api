"""Capture harness: screenshot all library brands x surfaces x viewports.

Smoke test / script. Consumes build_capture_plan and shells out to the
Page to Image Utility CLI for each target, writing PNGs to a dated output
directory. Never imported by the offline test suite; run directly or via
pytest --smoke.

Typical invocations
-------------------
# Full corpus capture (41 brands x 2 surfaces x 2 viewports = 164 images):
python -m tests.render.capture_harness --output-dir 02-prd/2026-06-12-visual-baseline

# Single brand:
python -m tests.render.capture_harness --single stripe --output-dir /tmp/harness-stripe

# First N brands (cheap iteration during development):
python -m tests.render.capture_harness --limit 3 --output-dir /tmp/harness-first3

# With Resemblio basic auth (needed when staging is password-gated):
python -m tests.render.capture_harness --auth-env LIBRARY_BASIC_AUTH \
    --output-dir 02-prd/2026-06-12-visual-baseline

Dependencies
------------
- Page to Image Utility installed (pip install -e <workspace>/Page to Image Utility/code)
- Playwright Chromium: playwright install chromium
- Network access to resemblio.com (or override via --base-url)

Decision reference: D16 in
projects/OptSus Team/missions/resemblio-library-public-view-readiness-tdd-plan-v5.md

Schema: harness_capture_log_v1 (written as capture-log.json in output-dir)
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from tests.render.capture_plan import (
    ALL_SURFACES,
    ALL_VIEWPORTS,
    CaptureTarget,
    build_capture_plan,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default Resemblio base URL.
DEFAULT_BASE_URL = "https://resemblio.com"

#: Hub API endpoint; page_size=100 returns all brands in one call.
HUB_API_URL = "https://api.resemblio.com/v1/library/brands?page_size=100"

#: Retry budget for each capture: up to MAX_RETRIES attempts with backoff.
MAX_RETRIES = 3

#: Initial backoff in seconds; doubles on each retry.
BACKOFF_INITIAL = 2.0

#: Page to Image Utility default wait (ms) per capture.
DEFAULT_WAIT_MS = 3000

_log = logging.getLogger("capture_harness")

SCHEMA_VERSION = "harness_capture_log_v1"


# ---------------------------------------------------------------------------
# Brand list fetch
# ---------------------------------------------------------------------------


def fetch_brand_slugs(api_url: str = HUB_API_URL) -> list[str]:
    """Fetch the live brand slug list from the hub API.

    Returns a sorted list of brand slugs. Raises RuntimeError on network
    failure or unexpected response shape.
    """
    try:
        import urllib.request
        with urllib.request.urlopen(api_url, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        raise RuntimeError(
            f"Could not fetch brand list from {api_url}: {exc}"
        ) from exc

    featured = data.get("data", {}).get("featured")
    if not isinstance(featured, list):
        raise RuntimeError(
            f"Unexpected hub response shape; expected data.featured list, "
            f"got: {type(featured)!r}"
        )
    slugs = [item["brand_slug"] for item in featured if "brand_slug" in item]
    if not slugs:
        raise RuntimeError("Hub returned zero brand slugs; aborting capture")
    return sorted(slugs)


# ---------------------------------------------------------------------------
# Single-target capture with retry
# ---------------------------------------------------------------------------


def capture_target(
    target: CaptureTarget,
    *,
    output_dir: pathlib.Path,
    page_to_image_module: str = "page_to_image",
    wait_ms: int = DEFAULT_WAIT_MS,
    auth_env: Optional[str] = None,
    base_url: str = DEFAULT_BASE_URL,
) -> dict[str, object]:
    """Capture one screenshot target via Page to Image Utility.

    Args:
        target:               The CaptureTarget to capture.
        output_dir:           Directory to write the PNG into.
        page_to_image_module: Python module path for the utility.
        wait_ms:              Navigation wait in milliseconds.
        auth_env:             Env var name containing 'USER:PASS' for basic auth.
        base_url:             Base URL for the library (allows staging override).

    Returns:
        A dict with keys: filename, status ('ok'|'failed'), attempts, error.

    Retry policy: up to MAX_RETRIES attempts with exponential backoff starting
    at BACKOFF_INITIAL seconds. Retries are appropriate because network
    timeouts and transient Playwright launch failures occur on congested
    systems. We do NOT retry HTTP 403/404 (structural failures).
    """
    output_path = output_dir / target.output_filename
    viewport = f"{target.width}x{target.height}"

    # Build the Page to Image Utility invocation.
    cmd = [
        sys.executable, "-m", page_to_image_module,
        "--url", target.url,
        "--output", str(output_path),
        "--viewport", viewport,
        "--wait-ms", str(wait_ms),
        "--quiet",
    ]
    if auth_env:
        cmd += ["--auth-env", auth_env]

    last_error: Optional[str] = None
    backoff = BACKOFF_INITIAL

    for attempt in range(1, MAX_RETRIES + 1):
        _log.info(
            "capture %s attempt %d/%d: %s",
            target.output_filename, attempt, MAX_RETRIES, target.url,
        )
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=90,
            )
            if result.returncode == 0 and output_path.is_file():
                return {
                    "filename": target.output_filename,
                    "status": "ok",
                    "attempts": attempt,
                    "error": None,
                }
            last_error = (
                result.stderr.strip() or result.stdout.strip()
                or f"exit code {result.returncode}"
            )
        except subprocess.TimeoutExpired:
            last_error = "subprocess timed out after 90s"
        except Exception as exc:
            last_error = str(exc)

        if attempt < MAX_RETRIES:
            _log.warning(
                "capture %s attempt %d failed (%s); retrying in %.1fs",
                target.output_filename, attempt, last_error, backoff,
            )
            time.sleep(backoff)
            backoff *= 2.0

    _log.error(
        "capture %s FAILED after %d attempts: %s",
        target.output_filename, MAX_RETRIES, last_error,
    )
    return {
        "filename": target.output_filename,
        "status": "failed",
        "attempts": MAX_RETRIES,
        "error": last_error,
    }


# ---------------------------------------------------------------------------
# Full corpus capture
# ---------------------------------------------------------------------------


def run_capture(
    brands: list[str],
    *,
    output_dir: pathlib.Path,
    single: Optional[str] = None,
    limit: Optional[int] = None,
    base_url: str = DEFAULT_BASE_URL,
    wait_ms: int = DEFAULT_WAIT_MS,
    auth_env: Optional[str] = None,
) -> dict[str, object]:
    """Capture the full brand corpus (or a subset) to output_dir.

    Args:
        brands:     Full ordered brand list (from fetch_brand_slugs).
        output_dir: Directory to write PNGs and the capture log into.
        single:     If set, capture only this brand slug.
        limit:      If set, capture only the first N brands.
        base_url:   Override for library base URL.
        wait_ms:    Navigation wait per capture.
        auth_env:   Env var name for basic auth.

    Returns:
        A capture log dict with schema_version, timestamp, brand_list, results.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Filter the brand list per flags.
    if single:
        if single not in brands:
            raise ValueError(f"--single {single!r} not in brand list {brands!r}")
        filtered = [single]
    elif limit is not None:
        filtered = brands[:limit]
    else:
        filtered = brands

    plan = build_capture_plan(filtered, base_url=base_url)
    total = len(plan)
    _log.info(
        "Starting capture: %d brands, %d targets, output=%s",
        len(filtered), total, output_dir,
    )

    results: list[dict[str, object]] = []
    ok_count = 0
    fail_count = 0

    for i, target in enumerate(plan, 1):
        _log.info("[%d/%d] %s", i, total, target.output_filename)
        result = capture_target(
            target,
            output_dir=output_dir,
            wait_ms=wait_ms,
            auth_env=auth_env,
            base_url=base_url,
        )
        results.append(result)
        if result["status"] == "ok":
            ok_count += 1
        else:
            fail_count += 1

    log = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "brand_count": len(filtered),
        "total_targets": total,
        "ok_count": ok_count,
        "fail_count": fail_count,
        "brands": filtered,
        "results": results,
    }

    log_path = output_dir / "capture-log.json"
    log_path.write_text(json.dumps(log, indent=2), encoding="utf-8")
    _log.info(
        "Capture complete: %d ok, %d failed. Log: %s",
        ok_count, fail_count, log_path,
    )
    return log


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Capture screenshots of all Resemblio library brands. "
            "Writes PNGs and a capture-log.json to --output-dir."
        ),
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory to write PNGs and capture-log.json",
    )
    parser.add_argument(
        "--single",
        metavar="SLUG",
        default=None,
        help="Capture only this brand slug (cheap test mode)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Capture only the first N brands",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Override library base URL (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--wait-ms",
        type=int,
        default=DEFAULT_WAIT_MS,
        help=f"Navigation wait per capture in ms (default: {DEFAULT_WAIT_MS})",
    )
    parser.add_argument(
        "--auth-env",
        metavar="VARNAME",
        default=None,
        help="Env var holding USER:PASS for HTTP Basic Auth",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point. Returns exit code: 0=all ok, 1=some failed."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")

    output_dir = pathlib.Path(args.output_dir).resolve()

    _log.info("Fetching brand list from %s", HUB_API_URL)
    brands = fetch_brand_slugs()
    _log.info("Found %d brands", len(brands))

    capture_log = run_capture(
        brands,
        output_dir=output_dir,
        single=args.single,
        limit=args.limit,
        base_url=args.base_url,
        wait_ms=args.wait_ms,
        auth_env=args.auth_env,
    )

    if capture_log["fail_count"]:
        _log.warning(
            "%d target(s) failed; see capture-log.json for details",
            capture_log["fail_count"],
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

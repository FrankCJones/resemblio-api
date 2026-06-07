"""Capture button computed-style snapshots from pinned HTML fixtures.

Why this script exists
----------------------
Some brands are live-blocked by bot-detection walls (Cloudflare Turnstile,
Vercel security checkpoint) that Playwright cannot pass. If the brand has a
real-markup HTML fixture on disk (not the wall page itself), this script
captures its button styles by loading that fixture via ``page.set_content``
(zero network), using the same ``capture_computed_styles(html=...)`` call path
that the D4 opt-in browser proof validated.

The resulting snapshot is written to a caller-supplied output directory so it
can be committed to the git-tracked seed tree
(``_vendored/drl/drl/_data/computed_styles/``). Committing to the seed tree is
correct here - unlike live captures (which are non-reproducible network I/O
and belong in the runtime root), fixture-based captures are deterministic and
reproducible from the committed fixture, making them a build artifact.

This script is NOT a replacement for the live-capture pipeline. It is a
surgical fallback for the specific case of "live-blocked + real markup exists."
See ``extractor/fixture_capture_registry.py`` for the registry of brands in
that category and the criteria for adding one.

Dependencies
------------
- ``extractor.fixture_capture_registry``: brand -> fixture registry
- ``extractor.computed_styles.capture_computed_styles``: Playwright render path
  (required for the real render; an injectable ``capture_fn`` is accepted so
  dep-free unit tests can pass a fake without needing chromium)

Run commands
------------
::

    # Generate openai's seed snapshot (writes to seed tree for commit):
    python -m scripts.capture_button_snapshot_from_fixture \\
        --brand openai \\
        --out-dir _vendored/drl/drl/_data/computed_styles

    # Write to a custom dir (e.g. the runtime root for local verification):
    python -m scripts.capture_button_snapshot_from_fixture \\
        --brand openai \\
        --out-dir /var/lib/resemblio/computed_styles

Why --out-dir, not the runtime root by default?
Because the PRIMARY use of this script is generating a committed seed snapshot;
that belongs in the git-tracked seed tree. The live-capture script
(``capture_all_button_snapshots.py``) writes to the runtime root by default
because live captures are not reproducible build artifacts. Here the default
would be ambiguous, so the caller is required to be explicit.

Quality floor: docstrings, TypedDict shapes, named constants, logger for
unattended use, retry-less (network-free render; no retry needed).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

_API_ROOT = Path(__file__).resolve().parents[1]
_path_text = str(_API_ROOT)
if _path_text not in sys.path:
    sys.path.insert(0, _path_text)

if TYPE_CHECKING:
    from extractor.computed_styles import ComputedStyleReport
    from extractor.fixture_capture_registry import FixtureCaptureSpec

LOG = logging.getLogger("capture_button_snapshot_from_fixture")
LOG.propagate = True

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_ERROR = 1

SNAPSHOT_SCHEMA_VERSION = 1
"""Bumped if the on-disk snapshot envelope shape changes. Mirrors
``capture_all_button_snapshots.SNAPSHOT_SCHEMA_VERSION``."""

# Acceptance gate constants for the D7 field-count check.
# Canonical definitions live in ``tests/test_button_corpus_coverage.py``.
# Duplicated here so the production script does not import from the test
# package. A future refactor may extract these to a shared constants module.
_TRACKED_BUTTON_FIELDS: tuple[str, ...] = (
    "border-radius",
    "padding",
    "font-family",
    "background-color",
    "color",
    "border",
)
"""CSS fields whose values the button-override loader consumes."""

_DEFAULT_PLACEHOLDER_VALUES: frozenset[str] = frozenset({
    "",
    "0px",
    "0px 0px",
    "0px none rgb(0, 0, 0)",
    "none",
    "normal",
    "auto",
})
"""Sentinel values written when a capture slot matched nothing or was skipped.
A field carrying one of these is treated as "default" (not real captured data)."""

REQUIRED_NON_DEFAULT_FIELDS: int = 4
"""Minimum number of TRACKED_BUTTON_FIELDS that must carry a non-default value
for the snapshot to be accepted. Mirrors OPENAI_REQUIRED_NON_DEFAULT_FIELDS in
the corpus test. If a capture yields fewer real fields, the D7 gate fires and
the script refuses to write the snapshot."""

# ---------------------------------------------------------------------------
# Type alias
# ---------------------------------------------------------------------------

CaptureFn = Callable[
    [str | None, str | None, int, str | None],
    "ComputedStyleReport",
]
"""Signature of ``capture_computed_styles(html, url, timeout_ms, brand_slug)``.

Injectable for dep-free testing: pass a fake that returns a synthetic
ComputedStyleReport without requiring Playwright or chromium.
"""

# ---------------------------------------------------------------------------
# Envelope builder
# ---------------------------------------------------------------------------


def build_fixture_snapshot_envelope(
    report: dict[str, Any],
    brand_slug: str,
    spec: "FixtureCaptureSpec",
) -> dict[str, Any]:
    """Build the on-disk JSON envelope for a fixture-derived snapshot.

    Extends the ``ComputedStyleReport`` dict with capture-source provenance
    keys. The shape is compatible with what ``capture_all_button_snapshots.py``
    writes for live captures (same top-level ``ComputedStyleReport`` keys +
    ``captured_url`` + ``envelope_schema_version``), plus three extra keys that
    identify the fixture as the source.

    Args:
        report: ``ComputedStyleReport`` dict returned by ``capture_fn``.
            Must have ``status == "ok"``. The top-level keys (``status``,
            ``signals``, ``error``, ``schema_version``) are preserved
            verbatim so the snapshot loader can cast the dict directly to
            ``ComputedStyleReport`` without knowing about the provenance keys.
        brand_slug: Brand slug (e.g. ``"openai"``). Used to build the
            ``fixture_path`` provenance value.
        spec: The brand's ``FixtureCaptureSpec`` from
            ``extractor.fixture_capture_registry.FIXTURE_CAPTURE_BRANDS``.

    Returns:
        A dict carrying all ``ComputedStyleReport`` keys plus:

        - ``captured_url``: ``spec["canonical_url"]``
        - ``envelope_schema_version``: ``SNAPSHOT_SCHEMA_VERSION``
        - ``capture_source``: ``"fixture"`` (distinguishes from live captures)
        - ``fixture_path``: repo-relative path to the fixture HTML
        - ``fixture_captured_at``: ISO date the fixture was saved
        - ``capture_reason``: why live capture is unavailable

    No side effects. Pure function; safe to call in tests.
    """
    from extractor.fixture_capture_registry import fixture_path  # noqa: PLC0415

    fixture_abs = fixture_path(brand_slug)
    # Store a repo-relative path (more readable in diffs than an absolute path
    # that differs across machines). Fall back to the absolute path if the
    # relative resolution fails (e.g. when tests override fixture_dir).
    try:
        fixture_rel = str(fixture_abs.relative_to(_API_ROOT))
    except ValueError:
        fixture_rel = str(fixture_abs)

    return {
        # ComputedStyleReport core keys (preserved verbatim for loader compat)
        **report,
        # Live-capture provenance (same as capture_all_button_snapshots)
        "captured_url": spec["canonical_url"],
        "envelope_schema_version": SNAPSHOT_SCHEMA_VERSION,
        # Fixture-capture-specific provenance (additional keys)
        "capture_source": "fixture",
        "fixture_path": fixture_rel,
        "fixture_captured_at": spec["fixture_captured_at"],
        "capture_reason": spec["capture_reason"],
    }


# ---------------------------------------------------------------------------
# Field-count gate (D7)
# ---------------------------------------------------------------------------


def _count_non_default_cta_fields(report: dict[str, Any]) -> int:
    """Count how many TRACKED_BUTTON_FIELDS carry non-default values in the cta slot.

    Returns 0 when the cta slot is absent, the signals list is empty, or all
    captured properties are default/placeholder values.
    """
    signals = report.get("signals")
    if not isinstance(signals, list):
        return 0
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        if signal.get("slot") != "cta":
            continue
        props = signal.get("properties")
        if not isinstance(props, dict):
            return 0
        count = 0
        for field_name in _TRACKED_BUTTON_FIELDS:
            value = str(props.get(field_name, "")).strip()
            if value and value not in _DEFAULT_PLACEHOLDER_VALUES:
                count += 1
        return count
    return 0


# ---------------------------------------------------------------------------
# Core capture function
# ---------------------------------------------------------------------------


def run_fixture_capture(
    brand_slug: str,
    out_dir: Path,
    *,
    capture_fn: CaptureFn | None = None,
    timeout_ms: int = 15_000,
    force: bool = False,
) -> Path:
    """Capture a brand's button snapshot from its pinned HTML fixture.

    Reads the fixture HTML from disk, calls ``capture_fn`` (or the real
    ``capture_computed_styles``) with ``html=<fixture>`` (zero network), builds
    the provenance envelope, enforces the D7 field-count gate, and writes the
    snapshot to ``out_dir/<brand_slug>.json``.

    Args:
        brand_slug: Must be a key in ``FIXTURE_CAPTURE_BRANDS``. Raises
            ``KeyError`` for unknown slugs.
        out_dir: Directory to write the snapshot to. Created with
            ``parents=True, exist_ok=True`` if it does not exist.
        capture_fn: Injectable capture function for dep-free testing. Defaults
            to the real ``capture_computed_styles``. Signature:
            ``(html, url, timeout_ms, brand_slug) -> ComputedStyleReport``.
        timeout_ms: Playwright render timeout in milliseconds.
        force: When ``False`` (default), raises ``FileExistsError`` if the
            output file already exists. When ``True``, overwrites.

    Returns:
        The ``Path`` of the written snapshot file.

    Raises:
        KeyError: ``brand_slug`` not in ``FIXTURE_CAPTURE_BRANDS``.
        FileNotFoundError: The fixture HTML file does not exist on disk.
        FileExistsError: Output file exists and ``force=False``.
        RuntimeError: ``capture_fn`` returned a non-"ok" status
            (unavailable, error) - Playwright missing or render failed.
        ValueError: The capture yielded fewer than ``REQUIRED_NON_DEFAULT_FIELDS``
            non-default CTA fields (D7 gate). The snapshot is NOT written.
    """
    from extractor.fixture_capture_registry import FIXTURE_CAPTURE_BRANDS, fixture_path  # noqa: PLC0415

    spec = FIXTURE_CAPTURE_BRANDS[brand_slug]  # KeyError if unknown
    fixture_html_path = fixture_path(brand_slug)
    if not fixture_html_path.exists():
        raise FileNotFoundError(
            f"Fixture for {brand_slug!r} not found at {fixture_html_path}. "
            "Commit the HTML fixture to tests/fixtures/button_capture/ first."
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{brand_slug}.json"
    if out_file.exists() and not force:
        raise FileExistsError(
            f"Snapshot already exists at {out_file}. Pass force=True to overwrite."
        )

    fixture_html = fixture_html_path.read_text(encoding="utf-8", errors="replace")
    LOG.info("brand=%s fixture=%s bytes=%d", brand_slug, fixture_html_path.name, len(fixture_html))

    fn = capture_fn or _default_capture_fn()
    try:
        report = fn(fixture_html, None, timeout_ms, brand_slug)
    except Exception as exc:  # noqa: BLE001 - surface all failures clearly
        raise RuntimeError(
            f"capture_fn raised for brand {brand_slug!r}: {type(exc).__name__}: {exc}"
        ) from exc

    if not isinstance(report, dict) or report.get("status") != "ok":
        status = (report or {}).get("status") if isinstance(report, dict) else "invalid"
        err = (report or {}).get("error") if isinstance(report, dict) else None
        raise RuntimeError(
            f"Capture returned status={status!r} for brand {brand_slug!r}. "
            f"error={err!r}. "
            "Ensure Playwright + chromium are installed and the fixture HTML is valid."
        )

    # D7 gate: enforce the minimum non-default field count.
    non_default = _count_non_default_cta_fields(report)  # type: ignore[arg-type]
    if non_default < REQUIRED_NON_DEFAULT_FIELDS:
        raise ValueError(
            f"D7 gate: brand={brand_slug!r} fixture capture yielded only "
            f"{non_default} non-default cta fields "
            f"(required >= {REQUIRED_NON_DEFAULT_FIELDS}). "
            "Do NOT commit a weak snapshot. "
            "Check the selector override and fixture validity. "
            "Fall back to DOCUMENTED_SKIP_BRANDS if the fixture cannot yield real styles."
        )

    LOG.info("brand=%s non_default_cta_fields=%d (gate=%d) PASSED", brand_slug, non_default, REQUIRED_NON_DEFAULT_FIELDS)

    envelope = build_fixture_snapshot_envelope(
        report=report,  # type: ignore[arg-type]
        brand_slug=brand_slug,
        spec=spec,
    )
    out_file.write_text(json.dumps(envelope, indent=2, sort_keys=True), encoding="utf-8")
    LOG.info("brand=%s wrote snapshot to %s", brand_slug, out_file)
    return out_file


# ---------------------------------------------------------------------------
# Default capture function
# ---------------------------------------------------------------------------


def _default_capture_fn() -> CaptureFn:
    """Return the real ``capture_computed_styles`` wrapped with our call signature."""
    from extractor.computed_styles import capture_computed_styles  # noqa: PLC0415

    def _wrapped(
        html: str | None,
        url: str | None,
        timeout_ms: int,
        brand_slug: str | None,
    ) -> "ComputedStyleReport":
        return capture_computed_styles(
            html=html,
            url=url,
            timeout_ms=timeout_ms,
            brand_slug=brand_slug,
        )

    return _wrapped


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Capture button computed-style snapshots from pinned HTML fixtures. "
            "For brands live-blocked by bot-detection where a real-markup fixture exists."
        )
    )
    parser.add_argument(
        "--brand",
        required=True,
        help="Brand slug to capture (must be in FIXTURE_CAPTURE_BRANDS).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help=(
            "Directory to write the snapshot JSON. "
            "For a committed seed snapshot, use: _vendored/drl/drl/_data/computed_styles"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing snapshot at out-dir/<brand>.json.",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=15_000,
        help="Playwright render timeout in milliseconds (default 15000).",
    )
    return parser.parse_args(argv)


def _configure_logging() -> None:
    """Attach a stderr handler unless one is already present."""
    if not LOG.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    _configure_logging()
    args = parse_args(argv)
    LOG.info(
        "capture_button_snapshot_from_fixture: brand=%s out_dir=%s force=%s",
        args.brand,
        args.out_dir,
        args.force,
    )

    from extractor.fixture_capture_registry import FIXTURE_CAPTURE_BRANDS  # noqa: PLC0415

    if args.brand not in FIXTURE_CAPTURE_BRANDS:
        available = ", ".join(sorted(FIXTURE_CAPTURE_BRANDS.keys()))
        LOG.error(
            "Unknown brand %r. Registered fixture-capture brands: %s",
            args.brand,
            available,
        )
        return EXIT_ERROR

    try:
        out_file = run_fixture_capture(
            args.brand,
            out_dir=Path(args.out_dir),
            timeout_ms=args.timeout_ms,
            force=args.force,
        )
    except (KeyError, FileNotFoundError, FileExistsError, RuntimeError, ValueError) as exc:
        LOG.error("capture failed for brand=%s: %s", args.brand, exc)
        return EXIT_ERROR

    LOG.info("SUCCESS: snapshot written to %s", out_file)
    LOG.info(
        "Next step (after verifying >= %d non-default fields): "
        "commit %s to the seed tree and push.",
        REQUIRED_NON_DEFAULT_FIELDS,
        out_file,
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

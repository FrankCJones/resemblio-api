"""Capture R3.1 computed-style snapshots for every brand in the DRL corpus.

Why this script exists
----------------------
The Hybrid Path B button-fidelity fix (CTO 2026-06-02,
`projects/OptSus Team/cto-reviews/2026-06-02-resemblio-button-fidelity-fix.md`)
overrides the DRL's generic `.b-btn` chiclet at compose time using
brand-specific tokens derived from a computed-style snapshot. The override
loader (`app/library_indexer.py::_load_button_tokens`) reads snapshots
from `<api_root>/_vendored/drl/drl/_data/computed_styles/<brand_slug>.json`;
when no snapshot exists the DRL default ships untouched.

Apple has a snapshot; the other 23 brands do not. Until every brand has
one, the library renders 23 default chiclets and 1 Apple pill. This
script is the system-level fix: discover every brand in the DRL, resolve
each brand's canonical URL from its `extraction.json` provenance, call
`extractor.computed_styles.capture_computed_styles(url=...)`, and write
the report to disk as the loader expects.

Idempotent + per-brand-isolated by design. A failing brand (network
timeout, JS-heavy site, Playwright unavailable) logs and continues; the
end-of-run summary names every failure. Re-running with the same flags
is a no-op for already-captured brands unless `--force` is passed.

Run commands
------------
::

    # Dry-run: list discovered brands; no captures, no writes.
    python -m scripts.capture_all_button_snapshots --drl-root /opt/resemblio-api/drl

    # Capture every missing snapshot.
    python -m scripts.capture_all_button_snapshots --apply --drl-root /opt/resemblio-api/drl

    # Single brand.
    python -m scripts.capture_all_button_snapshots --apply --single apple --drl-root /opt/resemblio-api/drl

    # Stage rollout (first 3 brands).
    python -m scripts.capture_all_button_snapshots --apply --limit 3 --drl-root /opt/resemblio-api/drl

    # Force overwrite an existing snapshot.
    python -m scripts.capture_all_button_snapshots --apply --single apple --force --drl-root /opt/resemblio-api/drl

Dependencies
------------
- `extractor.computed_styles.capture_computed_styles` (Playwright; degrades
  to status="unavailable" without it)
- The brand's `<drl-root>/_extractions/<brand>/extraction.json` carrying
  `sections.<section>.inspired_by[].url` provenance

Quality floor: docstrings, TypedDict for the per-brand spec, schema_version
on output, named constants. Logger-based output so a future cron/systemd
caller picks up structured lines.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, TypedDict
from urllib.parse import urlparse

_API_ROOT = Path(__file__).resolve().parents[1]
_path_text = str(_API_ROOT)
if _path_text not in sys.path:
    sys.path.insert(0, _path_text)

if TYPE_CHECKING:
    from extractor.computed_styles import ComputedStyleReport

LOG = logging.getLogger("capture_all_button_snapshots")
LOG.propagate = True

# --- Constants ---------------------------------------------------------------

EXIT_OK = 0
EXIT_ERROR = 1

DRL_EXTRACTIONS_DIRNAME = "_extractions"
"""Subdirectory of the DRL root that lists brand directories."""

EXTRACTION_FILENAME = "extraction.json"
"""Per-brand provenance file inside each `_extractions/<brand>/`."""

DEFAULT_OUT_DIR_REL = Path("_vendored") / "drl" / "drl" / "_data" / "computed_styles"
"""Default output dir, expressed relative to the api package root."""

DEFAULT_OUT_DIR = (_API_ROOT / DEFAULT_OUT_DIR_REL).resolve()
"""Resolved default: `<api>/_vendored/drl/drl/_data/computed_styles/`."""

DEFAULT_DRL_ROOT = Path("/opt/resemblio-api/drl")
"""Prod DRL root. Tests + local runs must pass --drl-root explicitly."""

SNAPSHOT_SCHEMA_VERSION = 1
"""Bumped if the on-disk snapshot envelope shape changes."""

# --- Typed shapes ------------------------------------------------------------


class BrandSpec(TypedDict):
    """Per-brand input for the capture loop.

    Fields:
    - slug: DRL directory name (e.g. ``apple``, ``the-pudding``).
    - url: canonical homepage URL resolved from the brand's extraction.json.
      ``None`` when no provenance URL could be resolved; the brand will be
      reported as ``status="skipped"`` with ``error="no canonical url"``.
    """

    slug: str
    url: str | None


class BrandOutcome(TypedDict):
    """Per-brand result after one capture pass."""

    slug: str
    url: str | None
    out_path: str
    status: str  # "captured" | "skipped" | "failed" | "dry-run"
    error: str | None


@dataclass
class CaptureReport:
    """Aggregate roll-up across every brand the script touched."""

    drl_root: str
    out_dir: str
    brands_discovered: int
    brands_processed: int
    outcomes: list[BrandOutcome] = field(default_factory=list)
    captured: int = 0
    skipped: int = 0
    failed: int = 0


@dataclass(frozen=True)
class CaptureArgs:
    """Parsed CLI arguments."""

    apply: bool
    force: bool
    drl_root: Path
    out_dir: Path
    single: str | None
    limit: int | None
    timeout_ms: int


# --- DRL discovery -----------------------------------------------------------


def discover_brand_dirs(drl_root: Path) -> list[Path]:
    """List brand directories under ``<drl_root>/_extractions/``.

    Filters hidden dirs and any name beginning with an underscore (the DRL
    uses ``_INBOX`` for staging). Returns sorted for deterministic output.
    """
    extractions_root = drl_root / DRL_EXTRACTIONS_DIRNAME
    if not extractions_root.exists():
        raise FileNotFoundError(
            f"DRL extractions root not found at {extractions_root!s}. Pass --drl-root."
        )
    dirs: list[Path] = []
    for child in sorted(extractions_root.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith("_") or child.name.startswith("."):
            continue
        dirs.append(child)
    return dirs


def resolve_brand_url(brand_dir: Path) -> str | None:
    """Resolve a brand's canonical homepage URL from its extraction.json.

    The DRL's ExtractionRecord places source URLs in
    ``sections.<section>.inspired_by[].url``. We prefer the alphabet
    section's first entry because the alphabet always points at the
    brand's marketing root; we fall back to scanning every section's
    first inspired_by URL and returning the shortest path (closest to
    homepage).

    Returns ``None`` when the file is missing, malformed, or carries no
    inspired_by URLs. The caller treats ``None`` as "skip this brand and
    report it" rather than guessing.
    """
    path = brand_dir / EXTRACTION_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sections = data.get("sections") if isinstance(data, dict) else None
    if not isinstance(sections, dict):
        return None

    candidates: list[str] = []
    # Preferred: alphabet section first inspired_by url.
    alphabet = sections.get("alphabet")
    preferred = _first_inspired_url(alphabet)
    if preferred:
        candidates.append(preferred)
    # Fallback: every other section's first inspired_by url.
    for name, section in sections.items():
        if name == "alphabet":
            continue
        url = _first_inspired_url(section)
        if url:
            candidates.append(url)

    if not candidates:
        return None
    # Prefer the shortest path (closest to homepage). Stable tiebreak on
    # the candidate order so the alphabet preference wins on tie.
    candidates_sorted = sorted(candidates, key=lambda u: (_path_depth(u), candidates.index(u)))
    return candidates_sorted[0]


def _first_inspired_url(section: object) -> str | None:
    """Return the first inspired_by URL from a section, or None."""
    if not isinstance(section, dict):
        return None
    inspired = section.get("inspired_by")
    if not isinstance(inspired, list):
        return None
    for entry in inspired:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        if isinstance(url, str) and url.strip().lower().startswith(("http://", "https://")):
            return url.strip()
    return None


def _path_depth(url: str) -> int:
    """Path-segment count for the URL, used to favor homepage URLs."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return 99
    return len([seg for seg in parsed.path.split("/") if seg])


def build_brand_specs(brand_dirs: list[Path]) -> list[BrandSpec]:
    """Pair every brand dir with its resolved URL (or None)."""
    return [
        BrandSpec(slug=d.name, url=resolve_brand_url(d))
        for d in brand_dirs
    ]


# --- Filtering ---------------------------------------------------------------


def select_brands(
    specs: list[BrandSpec],
    single: str | None,
    limit: int | None,
) -> list[BrandSpec]:
    """Apply ``--single`` and ``--limit`` filters to the brand spec list."""
    selected = specs
    if single is not None:
        selected = [s for s in selected if s["slug"] == single]
        if not selected:
            available = ", ".join(s["slug"] for s in specs)
            raise ValueError(
                f"--single {single!r} matched no brand. Available: {available}"
            )
    if limit is not None:
        selected = selected[:limit]
    return selected


# --- Per-brand capture -------------------------------------------------------


CaptureFn = Callable[[str | None, str | None, int, str | None], "ComputedStyleReport"]
"""Signature of ``capture_computed_styles(html, url, timeout_ms, brand_slug)``.

The brand_slug arg threads through to ``extractor.computed_styles`` so its
``BRAND_SELECTOR_OVERRIDES`` map can replace the default CTA selector for
brands whose first-`<button>` is a junk nav stub (openai) or whose CTAs
hydrate client-side after navigation (aeon). See the diagnosis at
``_handoff/inbox/claude/2026-06-02-openai-aeon-capture-diagnosis.md``.
The wait-strategy lever is controlled out-of-band via the
``RESEMBLIO_CAPTURE_WAIT_STRATEGY`` env var so prod re-captures can flip
SPA-tolerant mode on without a code change.
"""


def _default_capture_fn() -> CaptureFn:
    """Lazy import of the real capture function (keeps tests Playwright-free)."""
    from extractor.computed_styles import capture_computed_styles

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


def capture_one_brand(
    spec: BrandSpec,
    out_dir: Path,
    *,
    force: bool,
    timeout_ms: int,
    capture_fn: CaptureFn,
) -> BrandOutcome:
    """Capture one brand's snapshot and write it to disk.

    Per-brand isolation: returns a ``BrandOutcome`` with ``status="failed"``
    and a short error string on any failure rather than raising. The
    caller continues with the next brand regardless.
    """
    out_path = out_dir / f"{spec['slug']}.json"
    if not spec["url"]:
        return BrandOutcome(
            slug=spec["slug"],
            url=None,
            out_path=str(out_path),
            status="skipped",
            error="no canonical url in extraction.json",
        )
    if out_path.exists() and not force:
        return BrandOutcome(
            slug=spec["slug"],
            url=spec["url"],
            out_path=str(out_path),
            status="skipped",
            error="snapshot exists (pass --force to overwrite)",
        )
    try:
        report = capture_fn(None, spec["url"], timeout_ms, spec["slug"])
    except Exception as exc:  # noqa: BLE001 - per-brand isolation
        LOG.exception("capture raised for brand %s", spec["slug"])
        return BrandOutcome(
            slug=spec["slug"],
            url=spec["url"],
            out_path=str(out_path),
            status="failed",
            error=f"{type(exc).__name__}: {str(exc)[:200]}",
        )
    if not isinstance(report, dict) or report.get("status") != "ok":
        err = (report or {}).get("error") if isinstance(report, dict) else None
        return BrandOutcome(
            slug=spec["slug"],
            url=spec["url"],
            out_path=str(out_path),
            status="failed",
            error=f"capture status={ (report or {}).get('status') if isinstance(report, dict) else 'invalid'} err={err}",
        )
    # Preserve the ComputedStyleReport shape at the top level (the loader
    # casts the dict directly to ComputedStyleReport). Attach provenance
    # as extra keys; TypedDict-cast ignores them and downstream consumers
    # that want provenance can read them explicitly.
    envelope = {
        **report,  # status, signals, error, schema_version (report's own)
        "captured_url": spec["url"],
        "envelope_schema_version": SNAPSHOT_SCHEMA_VERSION,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(envelope, indent=2, sort_keys=True), encoding="utf-8")
    return BrandOutcome(
        slug=spec["slug"],
        url=spec["url"],
        out_path=str(out_path),
        status="captured",
        error=None,
    )


# --- Aggregate ---------------------------------------------------------------


def aggregate_report(
    drl_root: Path,
    out_dir: Path,
    discovered: int,
    outcomes: list[BrandOutcome],
) -> CaptureReport:
    """Roll per-brand outcomes into a single ``CaptureReport``."""
    report = CaptureReport(
        drl_root=str(drl_root),
        out_dir=str(out_dir),
        brands_discovered=discovered,
        brands_processed=len(outcomes),
        outcomes=outcomes,
    )
    for outcome in outcomes:
        if outcome["status"] == "captured":
            report.captured += 1
        elif outcome["status"] == "skipped":
            report.skipped += 1
        elif outcome["status"] == "failed":
            report.failed += 1
    return report


# --- CLI ---------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> CaptureArgs:
    """Parse argv into ``CaptureArgs``. Dry-run is the default."""
    parser = argparse.ArgumentParser(
        description="Capture R3.1 computed-style snapshots for every DRL brand."
    )
    parser.add_argument("--apply", action="store_true", help="Actually capture. Default is dry-run.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing snapshots.")
    parser.add_argument(
        "--drl-root",
        type=Path,
        default=DEFAULT_DRL_ROOT,
        help=f"Path to the DRL root (default {DEFAULT_DRL_ROOT}).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"Snapshot output dir (default {DEFAULT_OUT_DIR}).",
    )
    parser.add_argument("--single", type=str, default=None, help="Capture one brand only.")
    parser.add_argument("--limit", type=int, default=None, help="Cap number of brands processed.")
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=15_000,
        help="Per-page Playwright timeout (default 15000).",
    )
    namespace = parser.parse_args(argv)
    return CaptureArgs(
        apply=bool(namespace.apply),
        force=bool(namespace.force),
        drl_root=Path(namespace.drl_root).resolve(),
        out_dir=Path(namespace.out_dir).resolve(),
        single=namespace.single,
        limit=namespace.limit,
        timeout_ms=int(namespace.timeout_ms),
    )


# --- Logging -----------------------------------------------------------------


def _configure_logging() -> None:
    """Attach a stderr handler unless one is already present."""
    if not LOG.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)


def log_report(report: CaptureReport, mode: str) -> None:
    """Emit per-brand + aggregate lines for a completed run."""
    LOG.info(
        "%s: discovered=%d processed=%d captured=%d skipped=%d failed=%d out_dir=%s",
        mode,
        report.brands_discovered,
        report.brands_processed,
        report.captured,
        report.skipped,
        report.failed,
        report.out_dir,
    )
    for outcome in report.outcomes:
        LOG.info(
            "  brand=%s status=%s url=%s out=%s err=%s",
            outcome["slug"],
            outcome["status"],
            outcome["url"] or "",
            outcome["out_path"],
            outcome["error"] or "",
        )


# --- Orchestration -----------------------------------------------------------


def run(
    args: CaptureArgs,
    *,
    capture_fn: CaptureFn | None = None,
) -> CaptureReport:
    """End-to-end orchestration. Pure: tests inject ``capture_fn``."""
    brand_dirs = discover_brand_dirs(args.drl_root)
    specs = build_brand_specs(brand_dirs)
    selected = select_brands(specs, args.single, args.limit)
    LOG.info(
        "discovered=%d selected=%d apply=%s out_dir=%s",
        len(specs),
        len(selected),
        args.apply,
        args.out_dir,
    )

    outcomes: list[BrandOutcome] = []
    if not args.apply:
        for spec in selected:
            out_path = args.out_dir / f"{spec['slug']}.json"
            outcomes.append(
                BrandOutcome(
                    slug=spec["slug"],
                    url=spec["url"],
                    out_path=str(out_path),
                    status="dry-run" if spec["url"] else "skipped",
                    error=None if spec["url"] else "no canonical url in extraction.json",
                )
            )
        return aggregate_report(args.drl_root, args.out_dir, len(specs), outcomes)

    fn = capture_fn or _default_capture_fn()
    for spec in selected:
        outcomes.append(
            capture_one_brand(
                spec,
                args.out_dir,
                force=args.force,
                timeout_ms=args.timeout_ms,
                capture_fn=fn,
            )
        )
    return aggregate_report(args.drl_root, args.out_dir, len(specs), outcomes)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    _configure_logging()
    args = parse_args(argv)
    LOG.info(
        "capture_all_button_snapshots starting: apply=%s drl_root=%s out_dir=%s single=%s limit=%s",
        args.apply,
        args.drl_root,
        args.out_dir,
        args.single,
        args.limit,
    )
    report = run(args)
    log_report(report, mode="APPLY" if args.apply else "DRY RUN")
    return EXIT_OK if report.failed == 0 else EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())

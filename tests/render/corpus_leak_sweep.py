"""Full-corpus trademark wordmark-leak sweep for the Resemblio Library.

Checks every prod brand's live hub page (`/library/{slug}`) for forbidden
trademark wordmark / logo substrings, using the same evaluator as the Phase 11+
visual-fidelity gate. HTML-only; no Playwright, no reference PNGs.

Phase 15 scope (what this IS):
  - All 41 prod brands, one fetch per brand (hub page only).
  - Reuses `assertion_eval.evaluate_all_assertions_against_live_html` verbatim.
  - Produces a GO / NO-GO `CorpusLeakReport` mirroring the Phase 14 readiness
    pattern.

Phase 15 scope (what this IS NOT):
  - NOT the avatar/PII (`avatar_photo_leak`) sweep. That requires Playwright per
    brand and is deferred to Phase 16. Every report surface in this module
    explicitly states that the PII sweep is still pending.
  - NOT per-category page sweeps. Hub page only for v1; category depth is a
    Phase 16 extension.
  - NOT a rewrite of `assertion_eval.py`. The evaluator is imported unchanged.

Hard-vs-soft split:
  - LEAK found        -> hard NO-GO (leaked=True in BrandLeakFinding; go=False).
  - FETCH ERROR       -> hard NO-GO (error set; brand unverified; go=False).
  - universal-only    -> soft WARNING (clean today but weaker coverage; brand
                         listed in coverage_only_universal; does NOT block go).

Schema: corpus_leak_sweep_v1
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

import yaml

from tests.render.assertion_eval import (
    NO_LEAK_ID_MARKER,
    evaluate_all_assertions_against_live_html,
    forbidden_tokens_from_evaluator,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "corpus_leak_sweep_v1"

#: Default retry count for live HTTP fetches.
DEFAULT_RETRY_COUNT = 3

#: Base backoff seconds between retries (doubles each attempt).
RETRY_BACKOFF_BASE = 1.0

#: HTTP fetch timeout in seconds.
FETCH_TIMEOUT_SECONDS = 20


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BrandLeakFinding:
    """Per-brand trademark-leak result over one live page fetch.

    `leaked` is True iff a forbidden wordmark/logo substring was found in the
    brand's live HTML. `leaked_tokens` names which forbidden substrings hit
    (empty when clean). `had_per_brand_rules` records whether this brand had a
    per-brand entry in trademark_strip_targets.yml (False means only universal
    rules applied, giving weaker coverage - surfaced even when clean).
    `live_status` is the HTTP status of the fetch; `error` is set when the
    fetch failed. A fetch failure is NOT a clean pass - it is surfaced as an
    unknown and blocks the GO verdict (go=False when error_count > 0).
    """

    brand_slug: str
    leaked: bool
    leaked_tokens: List[str]
    had_per_brand_rules: bool
    live_status: Optional[int]
    error: Optional[str]


@dataclass(frozen=True)
class CorpusLeakReport:
    """Aggregate trademark-leak sweep over the full prod brand corpus.

    `go` is True iff ZERO brands leaked AND zero fetch errors left a brand
    unverified. A conservative posture: an unverified brand is not a clean
    pass. `coverage_only_universal` lists brands that passed using only the
    universal rules (no per-brand targets) - clean today, but the operator
    should know which brands lean on universal coverage alone. This list does
    NOT block go.

    The PII (avatar) all-corpus sweep is explicitly NOT included here; it is
    deferred to Phase 16 (requires Playwright per brand). Every report surface
    carries a footer stating this.
    """

    schema_version: str
    generated_at_utc: str
    resemblio_base: str
    total_brands: int
    brands_swept: int
    leak_count: int
    error_count: int
    coverage_only_universal: List[str]
    go: bool
    findings: List[BrandLeakFinding]


# ---------------------------------------------------------------------------
# Pure core functions
# ---------------------------------------------------------------------------


def forbidden_for_brand(
    brand_slug: str, targets: Dict
) -> Tuple[List[str], bool]:
    """Return (combined_forbidden_substrings, had_per_brand_rules).

    Merges the universal forbidden substrings from `targets` with any
    per-brand `forbidden_image_substrings` for `brand_slug`. Returns
    `had_per_brand_rules=True` when a per-brand entry exists in targets.

    Edge cases:
    - Brand not found in targets['brands']: returns (universal_only, False).
    - `targets['brands']` absent or empty: returns (universal_only, False).
    - Per-brand entry has no `forbidden_image_substrings`: still counts as a
      per-brand entry (had_per_brand_rules=True), but adds no extra tokens.

    Pure function; no network, no os.environ access.
    """
    universal: List[str] = targets.get("universal_forbidden_substrings", [])
    per_brand_tokens: List[str] = []
    had_per_brand_rules = False

    for entry in targets.get("brands", []):
        if entry.get("slug") == brand_slug:
            had_per_brand_rules = True
            per_brand_tokens = entry.get("forbidden_image_substrings", [])
            break

    return universal + per_brand_tokens, had_per_brand_rules


def build_no_leak_assertion(brand_slug: str, forbidden_substrings: List[str]) -> Dict:
    """Build a no-wordmark-logo-leak assertion dict in the exact evaluator shape.

    The returned dict matches the vendored fidelity-spec shape used by
    `evaluate_all_assertions_against_live_html`:

        {
          "id": "<brand_slug>-corpus-no-wordmark-logo-leak",
          "evaluate": "(() => { ... const forbidden = [...]; return forbidden.every(...) })()",
          "expected": True,
        }

    The `id` MUST contain `NO_LEAK_ID_MARKER` ("no-wordmark-logo-leak") so that
    `evaluate_all_assertions_against_live_html` classifies a failure as
    `wordmark_leak=True`.

    The evaluator string uses the `const forbidden = [...]` form that
    `forbidden_tokens_from_evaluator` can parse, and the `forbidden.every` form
    that `evaluate_assertion_against_live_html` dispatches on.

    Pure function; no network, no os.environ access.
    """
    forbidden_literal = "[" + ", ".join(f"'{t}'" for t in forbidden_substrings) + "]"
    evaluate = (
        f"(() => {{ const html = document.documentElement.outerHTML.toLowerCase(); "
        f"const forbidden = {forbidden_literal}; "
        f"return forbidden.every(s => !html.includes(s)); }})()"
    )
    return {
        "id": f"{brand_slug}-corpus-{NO_LEAK_ID_MARKER}",
        "evaluate": evaluate,
        "expected": True,
    }


def assess_brand_html(
    brand_slug: str, live_html: str, targets: Dict
) -> BrandLeakFinding:
    """Evaluate one brand's live HTML for trademark wordmark/logo leaks.

    Builds the no-wordmark assertion for the brand (using `forbidden_for_brand`
    + `build_no_leak_assertion`), then runs
    `evaluate_all_assertions_against_live_html`. Maps the sweep result to a
    `BrandLeakFinding`.

    `leaked=True` when any forbidden token appears in the HTML (case-insensitive
    via the evaluator's `.toLowerCase()` + `assertion_eval`'s own lowercasing).
    `leaked_tokens` names which specific substrings triggered the leak.

    This is the pure-core function the bulk of the unit tests target.
    `live_status` and `error` are always None when called directly; the
    orchestrator `run_corpus_leak_sweep` sets them after the fetch.

    Pure function; no network, no os.environ access.
    """
    forbidden, had_per_brand = forbidden_for_brand(brand_slug, targets)
    assertion = build_no_leak_assertion(brand_slug, forbidden)
    result = evaluate_all_assertions_against_live_html([assertion], live_html)

    leaked = result.wordmark_leak

    # Identify which specific tokens were found in the HTML.
    leaked_tokens: List[str] = []
    if leaked:
        haystack = live_html.lower()
        leaked_tokens = [t for t in forbidden if t.lower() in haystack]

    return BrandLeakFinding(
        brand_slug=brand_slug,
        leaked=leaked,
        leaked_tokens=leaked_tokens,
        had_per_brand_rules=had_per_brand,
        live_status=None,
        error=None,
    )


def audit_coverage(prod_slugs: List[str], targets: Dict) -> List[str]:
    """Return the prod slugs that have no per-brand entry in targets.

    These brands rely entirely on the universal forbidden substrings - they are
    clean today (if `run_corpus_leak_sweep` returns go=True), but they lack
    brand-specific logo/wordmark rules. The operator can use this list to
    decide which brands need per-brand entries added.

    Pure function; no network, no os.environ access.
    """
    per_brand_slugs = {entry["slug"] for entry in targets.get("brands", [])}
    return [s for s in prod_slugs if s not in per_brand_slugs]


def run_corpus_leak_sweep(
    prod_slugs: List[str],
    targets: Dict,
    resemblio_base: str,
    fetch_html: Callable[[str], Tuple[Optional[str], Optional[int], Optional[str]]],
) -> CorpusLeakReport:
    """Sweep all prod brand slugs for trademark wordmark/logo leaks.

    `fetch_html` is an injected callable `(brand_slug) -> (html, status, error)`.
    No network lives in this function; all I/O goes through the injected fetcher.
    This keeps the orchestrator fully unit-testable with a synthetic fetcher.

    `go=True` iff leak_count == 0 AND error_count == 0. An unverified brand
    (fetch error) is NOT a clean pass - the conservative posture mirrors Phase 14.

    `coverage_only_universal` lists slugs that pass using only universal rules
    (had_per_brand_rules=False). These are surfaced for operator visibility;
    they do NOT block go when they are clean.

    Args:
        prod_slugs: All brand slugs to sweep (canonical list from the API probe).
        targets: Parsed trademark_strip_targets.yml dict.
        resemblio_base: Base URL (e.g. "https://resemblio.com") for the report.
        fetch_html: Injected fetcher; returns (html_str, http_status, error_str).
                    On success: (html, 200, None). On error: (None, None, msg).

    Returns:
        CorpusLeakReport with go=True only when zero leaks and zero errors.
    """
    findings: List[BrandLeakFinding] = []
    uncovered = set(audit_coverage(prod_slugs, targets))

    for slug in prod_slugs:
        html, status, error = fetch_html(slug)

        if error is not None or html is None:
            findings.append(
                BrandLeakFinding(
                    brand_slug=slug,
                    leaked=False,
                    leaked_tokens=[],
                    had_per_brand_rules=(slug not in uncovered),
                    live_status=status,
                    error=error or "fetch returned None with no error message",
                )
            )
            continue

        base_finding = assess_brand_html(slug, html, targets)
        findings.append(
            BrandLeakFinding(
                brand_slug=slug,
                leaked=base_finding.leaked,
                leaked_tokens=base_finding.leaked_tokens,
                had_per_brand_rules=base_finding.had_per_brand_rules,
                live_status=status,
                error=None,
            )
        )

    leak_count = sum(1 for f in findings if f.leaked)
    error_count = sum(1 for f in findings if f.error is not None)
    coverage_only_universal = [f.brand_slug for f in findings if not f.had_per_brand_rules and f.error is None]

    return CorpusLeakReport(
        schema_version=SCHEMA_VERSION,
        generated_at_utc=datetime.now(tz=timezone.utc).isoformat(),
        resemblio_base=resemblio_base,
        total_brands=len(prod_slugs),
        brands_swept=len(findings),
        leak_count=leak_count,
        error_count=error_count,
        coverage_only_universal=coverage_only_universal,
        go=(leak_count == 0 and error_count == 0),
        findings=findings,
    )


def render_corpus_leak_markdown(report: CorpusLeakReport) -> str:
    """Render a CorpusLeakReport as a human-readable Markdown string.

    Produces:
      - Top-line GO / NO-GO headline.
      - Summary counts (brands swept, leaks, errors).
      - Per-leaking-brand lines (or "0 leaks across N brands").
      - "Coverage (universal-only)" section listing brands without per-brand
        trademark rules; these are clean but have weaker coverage.
      - Honesty footer: PII (avatar_photo_leak) all-corpus sweep is still
        pending Phase 16.

    A GO report looks like:
        ## Corpus leak sweep: GO

    A NO-GO report looks like:
        ## Corpus leak sweep: NO-GO
    """
    verdict_label = "GO" if report.go else "NO-GO"
    lines: List[str] = [
        f"## Corpus leak sweep: {verdict_label}",
        "",
        f"- Generated: {report.generated_at_utc}",
        f"- Base: {report.resemblio_base}",
        f"- Schema: {report.schema_version}",
        f"- Brands swept: {report.brands_swept} / {report.total_brands}",
        f"- Leaks: {report.leak_count}",
        f"- Fetch errors: {report.error_count}",
        "",
        "### Trademark wordmark-leak results",
        "",
    ]

    leaking = [f for f in report.findings if f.leaked]
    if not leaking:
        lines.append(f"0 leaks across {report.brands_swept} brands. All clean.")
    else:
        for f in leaking:
            lines.append(f"- LEAK: `{f.brand_slug}` - forbidden tokens: {f.leaked_tokens}")

    errors = [f for f in report.findings if f.error is not None]
    if errors:
        lines.append("")
        lines.append("### Fetch errors (brand unverified - counts as NO-GO)")
        lines.append("")
        for f in errors:
            lines.append(f"- ERROR: `{f.brand_slug}` - {f.error}")

    lines += [
        "",
        "### Coverage (universal-only brands)",
        "",
        "These brands passed using only the 10 universal forbidden substrings.",
        "They have no per-brand trademark_strip_targets.yml entry.",
        "They are clean today but have weaker coverage than the 6 per-brand-covered brands.",
        "",
    ]
    if report.coverage_only_universal:
        for slug in report.coverage_only_universal:
            lines.append(f"- `{slug}` (universal rules only)")
    else:
        lines.append("All swept brands have per-brand trademark rules.")

    lines += [
        "",
        "---",
        "",
        "**Phase 16 pending:** avatar/PII (`avatar_photo_leak`) sweep across all",
        f"{report.total_brands} brands is NOT included here. That check requires",
        "Playwright per-brand DOM evaluation and is deferred to Phase 16.",
        "This report covers ONLY the trademark wordmark-leak check (HTML substring).",
        "The inspirado-no-copiado + no-PII guarantee is not corpus-complete until",
        "Phase 16 completes.",
    ]

    if report.go:
        lines += [
            "",
            "**GO: 0 wordmark leaks and 0 fetch errors across all prod brands.**",
            "Trademark guarantee is now corpus-complete (41/41 brands verified).",
            "Phase 7 (Frank's CTA flip) remains gated on Phase 16 (PII) + tolerance ratification.",
        ]
    else:
        issues = []
        if report.leak_count > 0:
            issues.append(f"{report.leak_count} brand(s) leak a trademarked wordmark/logo")
        if report.error_count > 0:
            issues.append(f"{report.error_count} brand(s) could not be verified (fetch error)")
        lines += [
            "",
            f"**NO-GO: {'; '.join(issues)}. Resolve before Phase 7.**",
        ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Real fetcher (network seam - not used in unit tests)
# ---------------------------------------------------------------------------


def default_fetch_html(
    base: str,
    slug: str,
    timeout: int = FETCH_TIMEOUT_SECONDS,
    retry_count: int = DEFAULT_RETRY_COUNT,
    backoff_base: float = RETRY_BACKOFF_BASE,
) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    """Fetch the brand hub page HTML with retry/backoff.

    Returns (html_str, http_status, None) on success or
    (None, None, error_message) after all retries are exhausted.

    Uses urllib (stdlib); no external dependencies. Retries on connection
    errors, timeouts, and HTTP 5xx responses. Does not retry HTTP 4xx (client
    errors indicate the page is absent or auth-gated, not a transient failure).

    Args:
        base: Base URL without trailing slash (e.g. "https://resemblio.com").
        slug: Brand slug (e.g. "apple").
        timeout: Per-attempt HTTP timeout in seconds.
        retry_count: Total attempts including the first (so 3 means 1 try + 2 retries).
        backoff_base: Seconds to sleep before the first retry; doubles each attempt.

    Returns:
        (html, status, None) on success; (None, None, error_str) on failure.
    """
    url = f"{base}/library/{slug}"
    last_error: Optional[str] = None
    delay = backoff_base

    for attempt in range(retry_count):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "resemblio-phase15-sweep"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status: int = resp.status
                if status >= 500:
                    last_error = f"HTTP {status} on attempt {attempt + 1}"
                    if attempt < retry_count - 1:
                        time.sleep(delay)
                        delay *= 2
                    continue
                html = resp.read().decode("utf-8", "replace")
                return html, status, None
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return None, exc.code, f"HTTP {exc.code} (non-retryable)"
            last_error = f"HTTP {exc.code} on attempt {attempt + 1}"
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_error = f"{type(exc).__name__}: {exc} on attempt {attempt + 1}"

        if attempt < retry_count - 1:
            time.sleep(delay)
            delay *= 2

    return None, None, f"all {retry_count} attempts failed - last error: {last_error}"


def make_live_fetcher(
    base: str,
    timeout: int = FETCH_TIMEOUT_SECONDS,
    retry_count: int = DEFAULT_RETRY_COUNT,
) -> Callable[[str], Tuple[Optional[str], Optional[int], Optional[str]]]:
    """Return a fetch_html callable bound to `base` for use with run_corpus_leak_sweep.

    Usage:
        fetcher = make_live_fetcher("https://resemblio.com")
        report = run_corpus_leak_sweep(prod_slugs, targets, base, fetcher)
    """
    def _fetch(slug: str) -> Tuple[Optional[str], Optional[int], Optional[str]]:
        return default_fetch_html(base, slug, timeout=timeout, retry_count=retry_count)
    return _fetch


# ---------------------------------------------------------------------------
# Live execution helper (called by the Phase 15.6 run script)
# ---------------------------------------------------------------------------


def run_live_sweep_and_write_report(
    prod_slugs: List[str],
    targets: Dict,
    resemblio_base: str,
    output_dir: "pathlib.Path",
) -> CorpusLeakReport:
    """Run the full 41-brand live sweep and write JSON + Markdown reports.

    This is the Phase 15.6 execution entry point. Not called from unit tests.

    Writes two files to `output_dir`:
      - `corpus_leak_report.json` (CorpusLeakReport as dict, JSON-serializable)
      - `corpus_leak_report.md` (Markdown render)

    Args:
        prod_slugs: All 41 prod slugs from the API probe (Probe 1).
        targets: Parsed trademark_strip_targets.yml.
        resemblio_base: "https://resemblio.com"
        output_dir: pathlib.Path to an existing directory for report output.
                    MUST be outside the git repo (workspace _verification/ tree).

    Returns:
        The CorpusLeakReport (also written to disk).
    """
    import pathlib

    output_dir = pathlib.Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    fetcher = make_live_fetcher(resemblio_base)
    report = run_corpus_leak_sweep(prod_slugs, targets, resemblio_base, fetcher)

    # Serialize findings list (frozen dataclasses are not JSON-serializable by default)
    findings_dicts = [
        {
            "brand_slug": f.brand_slug,
            "leaked": f.leaked,
            "leaked_tokens": f.leaked_tokens,
            "had_per_brand_rules": f.had_per_brand_rules,
            "live_status": f.live_status,
            "error": f.error,
        }
        for f in report.findings
    ]
    report_dict = {
        "schema_version": report.schema_version,
        "generated_at_utc": report.generated_at_utc,
        "resemblio_base": report.resemblio_base,
        "total_brands": report.total_brands,
        "brands_swept": report.brands_swept,
        "leak_count": report.leak_count,
        "error_count": report.error_count,
        "coverage_only_universal": report.coverage_only_universal,
        "go": report.go,
        "findings": findings_dicts,
    }

    json_path = output_dir / "corpus_leak_report.json"
    md_path = output_dir / "corpus_leak_report.md"

    json_path.write_text(json.dumps(report_dict, indent=2), encoding="utf-8")
    md_path.write_text(render_corpus_leak_markdown(report), encoding="utf-8")

    return report

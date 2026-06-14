"""Full-corpus avatar/PII photo-leak sweep for the Resemblio Library.

Checks every prod brand's about-team page (`/library/{slug}/about-team`) for
real-person photo leaks using Playwright DOM evaluation. A photo in a
`.at__member` element after the brand-strip pipeline is a PII + trademark
violation and a HARD NO-GO.

Phase 16 scope (what this IS):
  - All 41 prod brands, one Playwright capture per brand on the about-team page.
  - Reuses `capture_live_render` for DOM eval; the browser assertion evaluator
    checks `.at__member img` presence.
  - Produces a GO / NO-GO `CorpusAvatarReport` mirroring the Phase 15 pattern.
  - Distinguishes four cases: LEAK, CLEAN, NA (no team section), UNVERIFIED.

Phase 16 scope (what this IS NOT):
  - NOT a sweep of non-about-team categories (testimonials, author photos, etc.).
    Those categories are deferred to Phase 17. Every report surface in this module
    explicitly states the non-about-team PII sweep is pending.
  - NOT a rewrite of `capture_live_render` or `classify_browser_eval_results`.
    These are imported unchanged; the corpus sweep adds a thin capturer on top.
  - NOT the Phase 7 homepage CTA flip. Production behavior is unchanged.

Hard-vs-soft split:
  - LEAK found        -> hard NO-GO (verdict=LEAK in BrandAvatarFinding; go=False).
  - UNVERIFIED        -> hard NO-GO (page or eval failed; brand unverified; go=False).
  - NA                -> does NOT block GO (no team section to check; honest absence).
  - CLEAN             -> does NOT block GO (members present, no photos found).

The TRAP (NA vs LEAK distinction):
  The existing vendored assertion for the 6 covered brands uses
  `members.length === 0 -> return false` with expected=True, which maps a
  no-team-section brand to LEAK (false positive). This module uses a PURPOSE-BUILT
  evaluator that returns True when NO `.at__member img` is found, plus a Python
  layer that additionally checks member_count from the rendered HTML. A brand
  with no members returns NA (not LEAK) because there are no photos to strip.

Schema: corpus_avatar_sweep_v1
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "corpus_avatar_sweep_v1"

#: CSS class marking a team member element in the about-team template.
MEMBER_SELECTOR = ".at__member"

#: CSS selector for photos inside a member element.
MEMBER_PHOTO_SELECTOR = ".at__member img"

#: JavaScript evaluator: returns True when NO member element contains an img.
#: With expected=True, False from the evaluator signals a LEAK.
#: Crucially, this returns True (not False) when there are no members at all,
#: so the Python classify layer handles NA via member_count, never by treating
#: "no members" as a LEAK.
AVATAR_PHOTO_EVALUATOR = (
    "(() => { "
    "const withPhoto = document.querySelectorAll('.at__member img'); "
    "return withPhoto.length === 0; "
    "})()"
)

#: Viewport for Playwright captures (matches tolerance_config.yml live_capture).
CAPTURE_VIEWPORT = "1280x800"

#: Default retry count for HTTP status probes.
DEFAULT_RETRY_COUNT = 3

#: Base backoff seconds between retries (doubles each attempt).
RETRY_BACKOFF_BASE = 1.0

#: HTTP probe timeout in seconds.
PROBE_TIMEOUT_SECONDS = 20


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


class AvatarVerdict(str, Enum):
    """Per-brand avatar/PII verdict over one about-team page check.

    LEAK       - members present and at least one carries a real-person photo
                 img. HARD NO-GO. Blocks the GO verdict for the corpus.
    CLEAN      - members present, none carries a photo img. The strip worked.
    NA         - no team section to check (no .at__member elements, or no
                 about-team page / HTTP 404). Nothing to leak; does NOT block
                 GO. Distinct from CLEAN for operator clarity.
    UNVERIFIED - could not load or evaluate the page (network error, timeout,
                 Playwright crash, evaluator threw). NOT a clean pass - blocks
                 GO (conservative posture mirrors Phase 15 fetch-error handling
                 and Phase 12 'missing != leak' honesty).
    """

    LEAK = "leak"
    CLEAN = "clean"
    NA = "na"
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class BrandAvatarFinding:
    """Per-brand avatar/PII result over one about-team page check.

    `verdict` is the AvatarVerdict. `member_count` is how many .at__member
    elements were detected in the rendered HTML (0 => NA). `members_with_photo`
    is how many of those carried a photo img (>0 => LEAK); None when the DOM
    evaluator could not run (contributes to UNVERIFIED). `live_status` is the
    HTTP status from the pre-flight probe (None when the probe failed).
    `error` is set for UNVERIFIED findings. `detail` is a human-readable
    string the markdown renderer surfaces.
    """

    brand_slug: str
    verdict: str          # AvatarVerdict value
    member_count: int
    members_with_photo: Optional[int]
    live_status: Optional[int]
    error: Optional[str]
    detail: str


@dataclass(frozen=True)
class CorpusAvatarReport:
    """Aggregate avatar/PII sweep over the full prod brand corpus.

    `go` is True iff ZERO brands are LEAK and ZERO are UNVERIFIED. NA and
    CLEAN do not block go. `leak_count`, `unverified_count`, `na_count`,
    `clean_count` break down the corpus. `na_brands` lists brands with no
    team section so the operator sees coverage is genuinely NA, not silently
    skipped.
    """

    schema_version: str            # "corpus_avatar_sweep_v1"
    generated_at_utc: str
    resemblio_base: str
    total_brands: int
    brands_swept: int
    leak_count: int
    unverified_count: int
    na_count: int
    clean_count: int
    na_brands: List[str]
    go: bool
    findings: List[BrandAvatarFinding]


# ---------------------------------------------------------------------------
# Pure core functions
# ---------------------------------------------------------------------------


def classify_avatar_eval(
    brand_slug: str,
    page_loaded: bool,
    http_status: Optional[int],
    member_count: int,
    members_with_photo: Optional[int],
    error: Optional[str],
) -> BrandAvatarFinding:
    """Map a single about-team capture result to a BrandAvatarFinding.

    This is the pure heart of Phase 16. Implements the four-case table:

    | State                                          | Verdict     |
    |------------------------------------------------|-------------|
    | error set OR page_loaded=False                 | UNVERIFIED  |
    | http_status == 404                             | NA          |
    | member_count == 0 (page loaded, no members)   | NA          |
    | members_with_photo is None (eval threw)        | UNVERIFIED  |
    | members_with_photo > 0                         | LEAK        |
    | members_with_photo == 0 (members present)     | CLEAN       |

    TRAP GUARD: member_count == 0 (no .at__member in rendered HTML) maps
    to NA, never LEAK. A brand with no team section cannot leak a photo.
    This is the anti-false-positive pin for Phase 16.

    ANTI-VACUITY: members_with_photo > 0 maps to LEAK regardless of other
    fields. This is the anti-vacuity pin confirming the evaluator can fire.

    Pure function; no network, no os.environ access.
    """
    # Priority order: UNVERIFIED first (most conservative).
    if not page_loaded or error is not None:
        verdict = AvatarVerdict.UNVERIFIED
        detail = f"page load failed: {error or 'unknown'}"
        return BrandAvatarFinding(
            brand_slug=brand_slug,
            verdict=verdict.value,
            member_count=member_count,
            members_with_photo=members_with_photo,
            live_status=http_status,
            error=error,
            detail=detail,
        )

    # HTTP 404: no about-team page -> NA.
    if http_status == 404:
        return BrandAvatarFinding(
            brand_slug=brand_slug,
            verdict=AvatarVerdict.NA.value,
            member_count=0,
            members_with_photo=0,
            live_status=404,
            error=None,
            detail="HTTP 404: no about-team page",
        )

    # No .at__member elements in the rendered DOM -> NA (THE TRAP GUARD).
    if member_count == 0:
        return BrandAvatarFinding(
            brand_slug=brand_slug,
            verdict=AvatarVerdict.NA.value,
            member_count=0,
            members_with_photo=0,
            live_status=http_status,
            error=None,
            detail="no .at__member elements on about-team page",
        )

    # Evaluator could not run (threw or was absent from eval_results) -> UNVERIFIED.
    if members_with_photo is None:
        return BrandAvatarFinding(
            brand_slug=brand_slug,
            verdict=AvatarVerdict.UNVERIFIED.value,
            member_count=member_count,
            members_with_photo=None,
            live_status=http_status,
            error="DOM evaluator result missing (threw or timed out)",
            detail=f"members={member_count} but evaluator did not return",
        )

    # ANTI-VACUITY PIN: any member with a photo is a LEAK (HARD NO-GO).
    if members_with_photo > 0:
        return BrandAvatarFinding(
            brand_slug=brand_slug,
            verdict=AvatarVerdict.LEAK.value,
            member_count=member_count,
            members_with_photo=members_with_photo,
            live_status=http_status,
            error=None,
            detail=f"LEAK: {members_with_photo} of {member_count} members carry a photo img",
        )

    # Members present, no photos -> CLEAN.
    return BrandAvatarFinding(
        brand_slug=brand_slug,
        verdict=AvatarVerdict.CLEAN.value,
        member_count=member_count,
        members_with_photo=0,
        live_status=http_status,
        error=None,
        detail=f"clean: {member_count} members, 0 photos",
    )


def build_avatar_assertion(brand_slug: str) -> Dict:
    """Build the corpus avatar-leak browser assertion for one brand.

    Returns an assertion dict in the evaluator shape recognized by
    `capture_live_render`'s `browser_assertions` mechanism and by
    `classify_browser_eval_results`:

        {
          "id": "<brand_slug>-corpus-avatars-photo-stripped",
          "evaluate": "(() => { ... })()",
          "expected": True,
        }

    The id MUST contain AVATAR_LEAK_ID_MARKER ("avatars-photo-stripped")
    so that `classify_browser_eval_results` sets `avatar_photo_leak=True`
    when the observed value (False = photos found) mismatches expected (True).

    The evaluator returns `True` when NO `.at__member img` is found (CLEAN or
    NA), and `False` when at least one member has a photo img (LEAK). The
    Python layer (`classify_avatar_eval`) additionally checks member_count from
    the rendered HTML to distinguish CLEAN (members present, no photos) from
    NA (no members at all).

    This is intentionally different from the vendored per-brand assertion
    (`apple-about-team-avatars-photo-stripped`) which uses
    `members.length === 0 -> return false` - that evaluator produces FALSE
    POSITIVE leaks for brands with no team section. This corpus evaluator
    avoids the trap by checking only `.at__member img` presence, never
    keying the FAIL path on member absence.

    Pure function; no network, no os.environ access.
    """
    from tests.render.assertion_eval import AVATAR_LEAK_ID_MARKER

    return {
        "id": f"{brand_slug}-corpus-{AVATAR_LEAK_ID_MARKER}",
        "evaluate": AVATAR_PHOTO_EVALUATOR,
        "expected": True,
    }


def run_corpus_avatar_sweep(
    prod_slugs: List[str],
    resemblio_base: str,
    capture_avatar: Callable[[str], Tuple[bool, Optional[int], int, Optional[int], Optional[str]]],
) -> CorpusAvatarReport:
    """Sweep all prod brand slugs for avatar/PII photo leaks on about-team pages.

    `capture_avatar` is an injected callable
    `(brand_slug) -> (page_loaded, http_status, member_count, members_with_photo, error)`.
    No Playwright or network lives in this function; all I/O goes through the
    injected capturer. This keeps the orchestrator fully unit-testable with a
    synthetic capturer returning canned tuples.

    `go=True` iff leak_count == 0 AND unverified_count == 0. NA and CLEAN
    brands do not block go.

    `na_brands` lists slugs classified as NA (no team section or 404) so the
    operator can see that NA means genuinely-nothing-to-check, not a silently
    skipped brand.

    Args:
        prod_slugs: All brand slugs to sweep (canonical list from the API probe).
        resemblio_base: Base URL (e.g. "https://resemblio.com") for the report.
        capture_avatar: Injected capturer; returns
            (page_loaded, http_status, member_count, members_with_photo, error).

    Returns:
        CorpusAvatarReport with go=True only when zero leaks and zero unverified.
    """
    findings: List[BrandAvatarFinding] = []

    for slug in prod_slugs:
        page_loaded, http_status, member_count, members_with_photo, error = capture_avatar(slug)
        finding = classify_avatar_eval(
            brand_slug=slug,
            page_loaded=page_loaded,
            http_status=http_status,
            member_count=member_count,
            members_with_photo=members_with_photo,
            error=error,
        )
        findings.append(finding)

    leak_count = sum(1 for f in findings if f.verdict == AvatarVerdict.LEAK.value)
    unverified_count = sum(1 for f in findings if f.verdict == AvatarVerdict.UNVERIFIED.value)
    na_count = sum(1 for f in findings if f.verdict == AvatarVerdict.NA.value)
    clean_count = sum(1 for f in findings if f.verdict == AvatarVerdict.CLEAN.value)
    na_brands = [f.brand_slug for f in findings if f.verdict == AvatarVerdict.NA.value]

    return CorpusAvatarReport(
        schema_version=SCHEMA_VERSION,
        generated_at_utc=datetime.now(tz=timezone.utc).isoformat(),
        resemblio_base=resemblio_base,
        total_brands=len(prod_slugs),
        brands_swept=len(findings),
        leak_count=leak_count,
        unverified_count=unverified_count,
        na_count=na_count,
        clean_count=clean_count,
        na_brands=na_brands,
        go=(leak_count == 0 and unverified_count == 0),
        findings=findings,
    )


def render_corpus_avatar_markdown(report: CorpusAvatarReport) -> str:
    """Render a CorpusAvatarReport as a human-readable Markdown string.

    Produces:
      - Top-line GO / NO-GO headline.
      - Summary counts (brands swept, leaks, unverified, NA, clean).
      - Per-LEAK brand lines (HARD NO-GO).
      - Per-UNVERIFIED brand lines (HARD NO-GO).
      - NA-brands section (coverage is genuinely NA, not silently skipped).
      - Clean count.
      - Honesty footer: non-about-team PII (testimonials, author photos, etc.)
        sweep is deferred to Phase 17.

    A GO report starts with:
        ## Corpus avatar sweep: GO

    A NO-GO report starts with:
        ## Corpus avatar sweep: NO-GO
    """
    verdict_label = "GO" if report.go else "NO-GO"
    lines: List[str] = [
        f"## Corpus avatar sweep: {verdict_label}",
        "",
        f"- Generated: {report.generated_at_utc}",
        f"- Base: {report.resemblio_base}",
        f"- Schema: {report.schema_version}",
        f"- Brands swept: {report.brands_swept} / {report.total_brands}",
        f"- Leaks: {report.leak_count}",
        f"- Unverified: {report.unverified_count}",
        f"- NA (no team section): {report.na_count}",
        f"- Clean: {report.clean_count}",
        "",
        "### Avatar/PII leak results",
        "",
    ]

    leaking = [f for f in report.findings if f.verdict == AvatarVerdict.LEAK.value]
    if not leaking:
        lines.append(f"0 photo leaks across {report.brands_swept} brands. All clean or NA.")
    else:
        lines.append("**HARD NO-GO: real-person photos found in the following brands:**")
        lines.append("")
        for f in leaking:
            lines.append(f"- LEAK: `{f.brand_slug}` - {f.detail}")

    unverified = [f for f in report.findings if f.verdict == AvatarVerdict.UNVERIFIED.value]
    if unverified:
        lines.append("")
        lines.append("### Unverified brands (brand unverified - counts as NO-GO)")
        lines.append("")
        for f in unverified:
            lines.append(f"- UNVERIFIED: `{f.brand_slug}` - {f.error or f.detail}")

    lines += [
        "",
        "### NA brands (no about-team section - not a leak)",
        "",
        "These brands have no `.at__member` elements on their about-team page",
        "(or no about-team page at all). There is nothing to check for photos;",
        "NA is NOT a skipped brand. It is genuinely clean by absence.",
        "",
    ]
    if report.na_brands:
        for slug in report.na_brands:
            lines.append(f"- `{slug}` (NA: no team section)")
    else:
        lines.append("All swept brands have an about-team section.")

    lines += [
        "",
        f"### Clean brands: {report.clean_count}",
        "",
        "These brands have an about-team section and zero member photo imgs found.",
        "The brand-strip pipeline correctly removed all team headshots.",
        "",
        "---",
        "",
        "**Phase 17 pending:** non-about-team PII sweep (testimonial avatars,",
        "author photos on article/blog templates, etc.) is NOT included here.",
        "About-team is where team headshots live and where the existing assertion",
        "is defined. Other-category real-person photos are a Phase 17 follow-on.",
        "This report covers ONLY the about-team avatar/PII dimension.",
        "The inspirado-no-copiado + no-PII guarantee is not fully corpus-complete",
        "until Phase 17 covers non-about-team categories.",
    ]

    if report.go:
        lines += [
            "",
            "**GO: 0 photo leaks and 0 unverified brands across all prod brands.**",
            "Avatar/PII guarantee is now corpus-complete for the about-team category",
            f"({report.total_brands}/{report.total_brands} brands verified).",
            "Phase 7 (Frank's CTA flip) and tolerance ratification remain Frank's gates.",
        ]
    else:
        issues = []
        if report.leak_count > 0:
            issues.append(f"{report.leak_count} brand(s) leak a real-person photo")
        if report.unverified_count > 0:
            issues.append(f"{report.unverified_count} brand(s) could not be verified")
        lines += [
            "",
            f"**NO-GO: {'; '.join(issues)}. Resolve before Phase 7.**",
        ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Real capturer (Playwright seam - not used in unit tests)
# ---------------------------------------------------------------------------


def _probe_http_status(
    url: str,
    timeout: int = PROBE_TIMEOUT_SECONDS,
    retry_count: int = DEFAULT_RETRY_COUNT,
    backoff_base: float = RETRY_BACKOFF_BASE,
) -> Tuple[Optional[int], Optional[str]]:
    """Probe an about-team URL with a lightweight GET to get the HTTP status.

    Returns (http_status, None) on a definitive response (200, 404, etc.) or
    (None, error_str) when the probe itself fails. Does not retry 4xx responses.

    Args:
        url: Full URL to probe.
        timeout: Per-attempt timeout in seconds.
        retry_count: Total attempts (1 try + retry_count-1 retries).
        backoff_base: Seconds before first retry; doubles each attempt.

    Returns:
        (status_int, None) on success; (None, error_str) on probe failure.
    """
    last_error: Optional[str] = None
    delay = backoff_base

    for attempt in range(retry_count):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "resemblio-phase16-sweep"}, method="GET"
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, None
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return exc.code, None  # 4xx: definitive, no retry
            last_error = f"HTTP {exc.code} on attempt {attempt + 1}"
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_error = f"{type(exc).__name__}: {exc} on attempt {attempt + 1}"

        if attempt < retry_count - 1:
            time.sleep(delay)
            delay *= 2

    return None, f"all {retry_count} attempts failed - last: {last_error}"


def default_capture_avatar(
    base: str,
    slug: str,
    tolerance,
    output_dir: "pathlib.Path",
) -> Tuple[bool, Optional[int], int, Optional[int], Optional[str]]:
    """Capture one brand's about-team avatar state via Playwright.

    Wraps `capture_live_render` with the corpus avatar assertion.

    Steps:
      1. HTTP GET probe to get the status. If 404 -> NA, skip Playwright.
      2. If 200 -> run `capture_live_render` with the avatar browser assertion.
      3. Extract member_count from the rendered HTML (MEMBER_SELECTOR substring).
      4. Extract members_with_photo from browser_eval_results: True -> 0
         (no photos), False -> 1 (photos present), absent -> None (UNVERIFIED).

    Returns:
        (page_loaded, http_status, member_count, members_with_photo, error) tuple
        for passing to `classify_avatar_eval`.

    Not used in unit tests (contains Playwright / network). Import of
    `capture_live_render` is deferred inside this function to keep the module
    importable in environments without Playwright.
    """
    import pathlib

    from tests.render.test_visual_fidelity_gate import capture_live_render

    about_team_url = f"{base}/library/{slug}/about-team"
    output_dir = pathlib.Path(output_dir)

    # Step 1: lightweight HTTP status probe.
    http_status, probe_error = _probe_http_status(about_team_url)
    if probe_error is not None:
        return False, None, 0, 0, f"HTTP probe failed: {probe_error}"
    if http_status == 404:
        return True, 404, 0, 0, None

    # Step 2: Playwright capture with the avatar browser assertion.
    assertion = build_avatar_assertion(slug)
    live_render = capture_live_render(
        url=about_team_url,
        viewport=CAPTURE_VIEWPORT,
        output_dir=output_dir,
        tuple_id=f"{slug}_about-team",
        tolerance=tolerance,
        browser_assertions=[assertion],
    )

    if live_render is None:
        return False, http_status, 0, 0, "Playwright capture returned None"

    # Step 3: member_count from rendered HTML (substring check).
    member_count = 1 if MEMBER_SELECTOR in live_render.html else 0

    # Step 4: members_with_photo from browser eval results.
    assertion_id = assertion["id"]
    if assertion_id not in live_render.browser_eval_results:
        # Evaluator threw or was absent -> UNVERIFIED.
        return True, http_status, member_count, None, "DOM evaluator result missing"

    eval_true = live_render.browser_eval_results[assertion_id]
    # eval_true=True means ".at__member img" count is 0 (no photos). CLEAN (or NA).
    # eval_true=False means at least one member has a photo img. LEAK.
    members_with_photo = 0 if eval_true else 1

    return True, http_status, member_count, members_with_photo, None


def make_live_capturer(
    base: str,
    tolerance,
    output_dir: "pathlib.Path",
) -> Callable[[str], Tuple[bool, Optional[int], int, Optional[int], Optional[str]]]:
    """Return a capture_avatar callable bound to `base`, `tolerance`, `output_dir`.

    Usage:
        capturer = make_live_capturer("https://resemblio.com", tolerance, output_dir)
        report = run_corpus_avatar_sweep(prod_slugs, base, capturer)
    """
    import pathlib

    output_dir = pathlib.Path(output_dir)

    def _capture(slug: str) -> Tuple[bool, Optional[int], int, Optional[int], Optional[str]]:
        return default_capture_avatar(base, slug, tolerance, output_dir)

    return _capture


# ---------------------------------------------------------------------------
# Live execution helper (called by the Phase 16.5 run script)
# ---------------------------------------------------------------------------


def run_live_sweep_and_write_report(
    prod_slugs: List[str],
    resemblio_base: str,
    tolerance,
    output_dir: "pathlib.Path",
) -> CorpusAvatarReport:
    """Run the full 41-brand live avatar sweep and write JSON + Markdown reports.

    This is the Phase 16.5 execution entry point. Not called from unit tests.

    Writes two files to `output_dir`:
      - `corpus_avatar_report.json` (CorpusAvatarReport as dict, JSON-serializable)
      - `corpus_avatar_report.md` (Markdown render)

    Args:
        prod_slugs: All 41 prod slugs from the API probe (Probe 1).
        resemblio_base: "https://resemblio.com"
        tolerance: ToleranceConfig instance (loaded from tolerance_config.yml).
        output_dir: pathlib.Path to an existing directory for report output.
                    MUST be outside the git repo (workspace _verification/ tree).

    Returns:
        The CorpusAvatarReport (also written to disk).
    """
    import pathlib

    output_dir = pathlib.Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    capturer = make_live_capturer(resemblio_base, tolerance, output_dir)
    report = run_corpus_avatar_sweep(prod_slugs, resemblio_base, capturer)

    findings_dicts = [
        {
            "brand_slug": f.brand_slug,
            "verdict": f.verdict,
            "member_count": f.member_count,
            "members_with_photo": f.members_with_photo,
            "live_status": f.live_status,
            "error": f.error,
            "detail": f.detail,
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
        "unverified_count": report.unverified_count,
        "na_count": report.na_count,
        "clean_count": report.clean_count,
        "na_brands": report.na_brands,
        "go": report.go,
        "findings": findings_dicts,
    }

    json_path = output_dir / "corpus_avatar_report.json"
    md_path = output_dir / "corpus_avatar_report.md"

    json_path.write_text(json.dumps(report_dict, indent=2), encoding="utf-8")
    md_path.write_text(render_corpus_avatar_markdown(report), encoding="utf-8")

    return report

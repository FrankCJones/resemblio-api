"""Non-about-team PII sweep for the Resemblio Library.

Checks (brand, category) pairs for non-team-page PII using HTML img src
pattern matching. Covers testimonials, article-layout, and news-list - the
three categories with meaningful real-person-photo risk outside of about-team.

Phase 17 scope (what this IS):
  - All 40 prod brands (after shared suppression) times 3 target categories.
  - HTML-based scan: extract img src attributes, match against known-clean and
    suspicious-person-photo URL patterns.
  - Produces a GO / NO-GO NonTeamPIIReport. UNVERIFIED (not LEAK) is the
    conservative verdict for suspicious paths - it means "needs Playwright or
    human confirmation before declaring LEAK."
  - Categories explicitly NOT swept are named in the report (buttons, hero,
    footer, etc.) with a rationale note.

Phase 17 scope (what this IS NOT):
  - NOT a Playwright-DOM sweep (that is Phase 16's method for about-team).
    HTML-only is appropriate here because the risk is diffuse and the key
    question (does an img src path suggest a real-person photo?) is answerable
    from the static markup.
  - NOT a definitive LEAK detector. UNVERIFIED means "suspicious path found;
    use Playwright or human review to confirm before reporting LEAK."
  - NOT a sweep of about-team pages (Phase 16 covers those).
  - NOT the Phase 7 homepage CTA flip. Production behavior is unchanged.

Classification model:
  - HTTP 404 or no <img> tags   -> NA   (does not block GO)
  - All img srcs match clean    -> CLEAN (does not block GO)
  - Any img src is suspicious   -> UNVERIFIED (blocks GO; needs follow-up)
  - Fetch error                 -> UNVERIFIED (conservative; blocks GO)

UNVERIFIED is the right posture here because: (1) the HTML scan cannot read
pixels; (2) a suspicious URL pattern in stripped HTML may be a remnant src
attribute value that is overridden by CSS/JS and never loads; (3) the cost of
a false positive is a Playwright/human review step, not a product change.

Schema: nonteam_pii_sweep_v1
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "nonteam_pii_sweep_v1"

#: Target categories for this sweep. Other categories (buttons, hero, footer,
#: forms, etc.) are structural/component categories with negligible real-person
#: photo risk and are not swept in Phase 17.
TARGET_CATEGORIES = ["testimonials", "article-layout", "news-list"]

#: Categories explicitly excluded from Phase 17 with rationale.
#: Listed in the report so operators know the scope boundary.
EXCLUDED_CATEGORIES_NOTE = (
    "buttons, alphabet, badges, cards, inputs, navigation, pricing, forms, "
    "form-fields, how-it-works, hero, footer, feature-grid, cta-block, "
    "pricing-table, process-steps, library"
)

#: Img src substrings that indicate a KNOWN-CLEAN image (logo, icon, etc.).
#: A path matching one of these is not a person photo.
KNOWN_CLEAN_IMAGE_PATTERNS: List[str] = [
    "/logo",
    "/wordmark",
    "/icon",
    "/favicon",
    "/badge",
    "/brand",
    "/product",
    "/screenshot",
    "/illustration",
    "/graphic",
    "/hero",
    "/banner",
    "/bg",
    "/background",
    "/pattern",
    "/texture",
]

#: Img src substrings that suggest a real-person photo.
#: A path matching one of these triggers UNVERIFIED (not LEAK - needs Playwright
#: or human confirmation to confirm that the image actually loads and shows PII).
SUSPICIOUS_PERSON_PATTERNS: List[str] = [
    "/avatar",
    "/headshot",
    "/author",
    "/profile",
    "/person",
    "/user/",
    "/gravatar",
    "/member",
    "/portrait",
    "/photo/",
    "/face",
    "github.com/users",
    "avatars.githubusercontent",
    "gravatar.com",
    "pbs.twimg.com/profile",
    "lh3.googleusercontent",
]

#: Default retry count for HTTP fetches.
DEFAULT_RETRY_COUNT = 3

#: Base backoff seconds between retries (doubles each attempt).
RETRY_BACKOFF_BASE = 1.0

#: HTTP probe timeout in seconds.
PROBE_TIMEOUT_SECONDS = 20

#: Regex to extract img src attribute values from HTML.
#: Handles single and double quotes; matches across common attribute orderings.
_IMG_SRC_RE = re.compile(r'<img[^>]+\bsrc=["\']([^"\']+)["\']', re.IGNORECASE)


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


class NonTeamPIIVerdict(str, Enum):
    """Per-(brand, category) non-about-team PII verdict.

    CLEAN      - no suspicious person-photo img patterns found in HTML.
    NA         - category page 404 or no <img> tags in HTML. Nothing to flag.
    UNVERIFIED - suspicious img path found; needs Playwright or human confirm
                 before classifying as LEAK. Does NOT mean a photo was confirmed.
    """

    CLEAN = "clean"
    NA = "na"
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class NonTeamPIIFinding:
    """Per-(brand, category) non-about-team PII check result.

    `brand_slug` + `category_slug` identify the pair. `verdict` is the
    NonTeamPIIVerdict value (string). `suspicious_paths` lists the img src
    values that triggered UNVERIFIED (empty for CLEAN and NA). `live_status`
    is the HTTP status (None on fetch error). `error` is set for fetch
    failures. `detail` is a human-readable summary surfaced in the markdown
    report.
    """

    brand_slug: str
    category_slug: str
    verdict: str          # NonTeamPIIVerdict value
    suspicious_paths: List[str]
    live_status: Optional[int]
    error: Optional[str]
    detail: str


@dataclass(frozen=True)
class NonTeamPIIReport:
    """Aggregate non-about-team PII sweep result.

    `go` is True iff ZERO (brand, category) pairs are UNVERIFIED. CLEAN and
    NA do not block. `unverified_count` is how many pairs need follow-up.
    `unverified_findings` is the shortlist of UNVERIFIED pairs (with their
    `suspicious_paths`) for operator review. `na_count` and `clean_count`
    break down the non-blocking results.
    """

    schema_version: str             # "nonteam_pii_sweep_v1"
    generated_at_utc: str
    resemblio_base: str
    categories_swept: List[str]
    total_pairs: int
    pairs_swept: int
    unverified_count: int
    na_count: int
    clean_count: int
    go: bool
    findings: List[NonTeamPIIFinding]


# ---------------------------------------------------------------------------
# Pure core functions
# ---------------------------------------------------------------------------


def extract_img_srcs(html: str) -> List[str]:
    """Extract all img src attribute values from raw HTML.

    Handles single-quoted and double-quoted src attributes. Does not load the
    images; returns only the raw src strings as they appear in the markup.

    Args:
        html: Raw HTML string (may be empty).

    Returns:
        List of src values found; empty list if no <img src=...> tags.
    """
    if not html:
        return []
    return _IMG_SRC_RE.findall(html)


def classify_img_srcs(img_srcs: List[str]) -> Tuple[bool, List[str]]:
    """Classify a list of img src values for person-photo PII risk.

    Returns (has_suspicious, suspicious_paths) where:
      - has_suspicious is True when any src matches a SUSPICIOUS_PERSON_PATTERN.
      - suspicious_paths is the list of src values that matched.

    A src matching a KNOWN_CLEAN_IMAGE_PATTERN is never flagged as suspicious,
    even if it incidentally contains a substring in SUSPICIOUS_PERSON_PATTERNS.
    An unknown path (matching neither list) passes through as clean.

    ANTI-VACUITY: any src containing "/avatar" triggers suspicious=True.
    ANTI-FALSE-POSITIVE: any src containing "/logo" returns clean.

    Pure function; no network, no os.environ access.

    Args:
        img_srcs: List of img src values to classify.

    Returns:
        (has_suspicious, suspicious_paths) tuple.
    """
    suspicious: List[str] = []
    for src in img_srcs:
        src_lower = src.lower()
        # Known-clean patterns take precedence.
        if any(p in src_lower for p in KNOWN_CLEAN_IMAGE_PATTERNS):
            continue
        if any(p in src_lower for p in SUSPICIOUS_PERSON_PATTERNS):
            suspicious.append(src)
    return bool(suspicious), suspicious


def assess_brand_category_html(
    brand_slug: str,
    category_slug: str,
    html: Optional[str],
    http_status: Optional[int],
    error: Optional[str] = None,
) -> NonTeamPIIFinding:
    """Map a single (brand, category) HTML fetch to a NonTeamPIIFinding.

    Implements the four-case table:

    | State                                        | Verdict     |
    |----------------------------------------------|-------------|
    | error set OR html is None                    | UNVERIFIED  |
    | http_status == 404                           | NA          |
    | html has no <img> tags                       | NA          |
    | all img srcs are known-clean                 | CLEAN       |
    | any img src is suspicious                    | UNVERIFIED  |

    UNVERIFIED does NOT mean LEAK. It means "suspicious pattern found in HTML;
    use Playwright or human review to confirm the image actually loads as a
    real-person photo before reporting LEAK."

    Pure function; no network, no os.environ access.

    Args:
        brand_slug: The brand being checked.
        category_slug: The category being checked (e.g. "testimonials").
        html: Raw HTML from the category page (None on fetch error).
        http_status: HTTP status from the fetch (None on connection error).
        error: Error string when fetch itself failed (None on success).

    Returns:
        NonTeamPIIFinding with verdict and (for UNVERIFIED) suspicious_paths.
    """
    # Fetch error: conservative UNVERIFIED.
    if error is not None or html is None:
        return NonTeamPIIFinding(
            brand_slug=brand_slug,
            category_slug=category_slug,
            verdict=NonTeamPIIVerdict.UNVERIFIED.value,
            suspicious_paths=[],
            live_status=http_status,
            error=error or "html is None (fetch error)",
            detail=f"fetch error: {error or 'html is None'}",
        )

    # HTTP 404: no category page -> NA.
    if http_status == 404:
        return NonTeamPIIFinding(
            brand_slug=brand_slug,
            category_slug=category_slug,
            verdict=NonTeamPIIVerdict.NA.value,
            suspicious_paths=[],
            live_status=404,
            error=None,
            detail="HTTP 404: no category page",
        )

    # Extract img srcs.
    img_srcs = extract_img_srcs(html)

    # No <img> tags -> NA (nothing to check).
    if not img_srcs:
        return NonTeamPIIFinding(
            brand_slug=brand_slug,
            category_slug=category_slug,
            verdict=NonTeamPIIVerdict.NA.value,
            suspicious_paths=[],
            live_status=http_status,
            error=None,
            detail="no <img> tags in HTML",
        )

    # Classify the srcs.
    has_suspicious, suspicious_paths = classify_img_srcs(img_srcs)

    if has_suspicious:
        return NonTeamPIIFinding(
            brand_slug=brand_slug,
            category_slug=category_slug,
            verdict=NonTeamPIIVerdict.UNVERIFIED.value,
            suspicious_paths=suspicious_paths,
            live_status=http_status,
            error=None,
            detail=(
                f"UNVERIFIED: {len(suspicious_paths)} suspicious img path(s) found; "
                "needs Playwright or human confirmation before reporting LEAK"
            ),
        )

    return NonTeamPIIFinding(
        brand_slug=brand_slug,
        category_slug=category_slug,
        verdict=NonTeamPIIVerdict.CLEAN.value,
        suspicious_paths=[],
        live_status=http_status,
        error=None,
        detail=f"clean: {len(img_srcs)} img(s), 0 suspicious paths",
    )


def run_nonteam_pii_sweep(
    prod_slugs: List[str],
    categories: List[str],
    resemblio_base: str,
    fetch_html: Callable[[str], Tuple[Optional[str], Optional[int], Optional[str]]],
) -> NonTeamPIIReport:
    """Sweep all (brand, category) pairs for non-about-team PII.

    Fetches each (brand_slug, category_slug) pair's HTML via the injected
    `fetch_html` callable, then classifies it with `assess_brand_category_html`.
    No network or Playwright lives in this function; all I/O goes through the
    injected fetcher.

    `go=True` iff unverified_count == 0. NA and CLEAN do not block go.

    Args:
        prod_slugs: All prod brand slugs to sweep.
        categories: Category slugs to sweep per brand (e.g. ["testimonials"]).
        resemblio_base: Base URL (e.g. "https://resemblio.com") for report.
        fetch_html: Injected fetcher - `(url) -> (html, http_status, error)`.
            html is None on fetch failure. error is None on success.

    Returns:
        NonTeamPIIReport with go=True only when unverified_count == 0.
    """
    findings: List[NonTeamPIIFinding] = []

    for slug in prod_slugs:
        for cat in categories:
            url = f"{resemblio_base}/library/{slug}/{cat}"
            html, http_status, error = fetch_html(url)
            finding = assess_brand_category_html(slug, cat, html, http_status, error)
            findings.append(finding)

    unverified_count = sum(1 for f in findings if f.verdict == NonTeamPIIVerdict.UNVERIFIED.value)
    na_count = sum(1 for f in findings if f.verdict == NonTeamPIIVerdict.NA.value)
    clean_count = sum(1 for f in findings if f.verdict == NonTeamPIIVerdict.CLEAN.value)
    total_pairs = len(prod_slugs) * len(categories)

    return NonTeamPIIReport(
        schema_version=SCHEMA_VERSION,
        generated_at_utc=datetime.now(tz=timezone.utc).isoformat(),
        resemblio_base=resemblio_base,
        categories_swept=list(categories),
        total_pairs=total_pairs,
        pairs_swept=len(findings),
        unverified_count=unverified_count,
        na_count=na_count,
        clean_count=clean_count,
        go=(unverified_count == 0),
        findings=findings,
    )


def render_nonteam_pii_markdown(report: NonTeamPIIReport) -> str:
    """Render a NonTeamPIIReport as a human-readable Markdown string.

    Produces:
      - Top-line GO / NO-GO headline.
      - Summary counts (pairs swept, unverified, na, clean).
      - Per-UNVERIFIED pair lines with suspicious img paths (NO-GO cases).
      - Scope note: categories swept + explicitly excluded categories.
      - Playwright/human follow-up note for UNVERIFIED findings.

    A GO report starts with:
        ## Non-team PII sweep: GO

    A NO-GO report starts with:
        ## Non-team PII sweep: NO-GO
    """
    verdict_label = "GO" if report.go else "NO-GO"
    lines: List[str] = [
        f"## Non-team PII sweep: {verdict_label}",
        "",
        f"- Generated: {report.generated_at_utc}",
        f"- Base: {report.resemblio_base}",
        f"- Schema: {report.schema_version}",
        f"- Categories swept: {', '.join(report.categories_swept)}",
        f"- Total (brand x category) pairs: {report.total_pairs}",
        f"- Pairs swept: {report.pairs_swept}",
        f"- Unverified: {report.unverified_count}",
        f"- NA (no page or no img tags): {report.na_count}",
        f"- Clean: {report.clean_count}",
        "",
    ]

    unverified = [f for f in report.findings if f.verdict == NonTeamPIIVerdict.UNVERIFIED.value]
    if not unverified:
        lines += [
            "### Result",
            "",
            f"0 suspicious img paths across {report.pairs_swept} (brand, category) pairs. "
            "All pairs are CLEAN or NA.",
            "",
        ]
    else:
        lines += [
            "### UNVERIFIED pairs - Playwright or human review required",
            "",
            "These (brand, category) pairs contain img src patterns that MAY indicate a real-person",
            "photo. UNVERIFIED does NOT mean confirmed LEAK. Confirm with Playwright DOM eval or",
            "manual inspection before reporting these as PII failures.",
            "",
        ]
        for f in unverified:
            lines.append(f"- UNVERIFIED: `{f.brand_slug}` / `{f.category_slug}`")
            if f.error:
                lines.append(f"  - fetch error: {f.error}")
            for path in f.suspicious_paths:
                lines.append(f"  - suspicious img src: `{path}`")
        lines.append("")

    lines += [
        "---",
        "",
        "### Phase 17 scope",
        "",
        f"**Categories swept:** {', '.join(report.categories_swept)}",
        "",
        "These are the three categories with meaningful real-person-photo risk outside of",
        "about-team: testimonials (customer headshots), article-layout (author photos),",
        "and news-list (contributor photos).",
        "",
        f"**Categories NOT swept (low PII risk):** {EXCLUDED_CATEGORIES_NOTE}",
        "",
        "Component and structural categories (buttons, hero, footer, forms, etc.) do not carry",
        "real-person photos by design. They are excluded from Phase 17 intentionally.",
        "",
        "**About-team** was covered in Phase 16 (Playwright DOM sweep, all 40 brands CLEAN).",
        "Phase 17 adds the HTML-scan layer for non-team page types.",
        "",
        "**UNVERIFIED vs LEAK:** UNVERIFIED means a suspicious img src URL pattern was found",
        "in the static HTML. The pattern alone does not confirm the image loads or shows PII.",
        "Any UNVERIFIED finding requires Playwright DOM evaluation or human review before",
        "it can be upgraded to LEAK. There are no confirmed LEAK findings in this sweep.",
    ]

    if report.go:
        lines += [
            "",
            f"**GO: 0 UNVERIFIED pairs across {report.pairs_swept} (brand, category) pairs.**",
            "Non-team PII sweep is clear. Phase 7 (Frank's CTA flip) and tolerance",
            "ratification remain Frank's gates.",
        ]
    else:
        lines += [
            "",
            f"**NO-GO: {report.unverified_count} UNVERIFIED pair(s). "
            "Playwright or human review required before Phase 7.**",
        ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Real fetcher (network seam - not used in unit tests)
# ---------------------------------------------------------------------------


def _default_fetch_html(
    url: str,
    timeout: int = PROBE_TIMEOUT_SECONDS,
    retry_count: int = DEFAULT_RETRY_COUNT,
    backoff_base: float = RETRY_BACKOFF_BASE,
) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    """Fetch HTML from a URL with retry/backoff.

    Returns (html, http_status, None) on success or a definitive HTTP status
    (including 4xx). Returns (None, None, error_str) on persistent network
    failure. Does not retry 4xx responses.

    Args:
        url: Full URL to fetch.
        timeout: Per-attempt timeout in seconds.
        retry_count: Total attempts (1 try + retry_count-1 retries).
        backoff_base: Seconds before first retry; doubles each attempt.

    Returns:
        (html, http_status, error) tuple.
    """
    last_error: Optional[str] = None
    delay = backoff_base

    for attempt in range(retry_count):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "resemblio-phase17-pii-sweep"}, method="GET"
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace"), resp.status, None
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return None, exc.code, None  # 4xx: definitive, no retry
            last_error = f"HTTP {exc.code} on attempt {attempt + 1}"
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_error = f"{type(exc).__name__}: {exc} on attempt {attempt + 1}"

        if attempt < retry_count - 1:
            time.sleep(delay)
            delay *= 2

    return None, None, f"all {retry_count} attempts failed - last: {last_error}"


def run_live_sweep_and_write_report(
    prod_slugs: List[str],
    resemblio_base: str,
    categories: List[str],
    output_dir: "pathlib.Path",
) -> NonTeamPIIReport:
    """Run the full-corpus live non-team PII sweep and write JSON + Markdown.

    This is the Phase 17.5 execution entry point. Not called from unit tests.

    Writes two files to `output_dir`:
      - ``nonteam_pii_report.json`` (NonTeamPIIReport as dict)
      - ``nonteam_pii_report.md`` (Markdown render)

    Args:
        prod_slugs: All 40 prod slugs (after shared suppression).
        resemblio_base: "https://resemblio.com"
        categories: e.g. ["testimonials", "article-layout", "news-list"]
        output_dir: pathlib.Path to an existing directory for report output.
                    MUST be outside the git repo (workspace _verification/ tree).

    Returns:
        The NonTeamPIIReport (also written to disk).
    """
    import pathlib

    output_dir = pathlib.Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    report = run_nonteam_pii_sweep(
        prod_slugs=prod_slugs,
        categories=categories,
        resemblio_base=resemblio_base,
        fetch_html=_default_fetch_html,
    )

    findings_dicts = [
        {
            "brand_slug": f.brand_slug,
            "category_slug": f.category_slug,
            "verdict": f.verdict,
            "suspicious_paths": f.suspicious_paths,
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
        "categories_swept": report.categories_swept,
        "total_pairs": report.total_pairs,
        "pairs_swept": report.pairs_swept,
        "unverified_count": report.unverified_count,
        "na_count": report.na_count,
        "clean_count": report.clean_count,
        "go": report.go,
        "findings": findings_dicts,
    }

    json_path = output_dir / "nonteam_pii_report.json"
    md_path = output_dir / "nonteam_pii_report.md"

    json_path.write_text(json.dumps(report_dict, indent=2), encoding="utf-8")
    md_path.write_text(render_nonteam_pii_markdown(report), encoding="utf-8")

    return report

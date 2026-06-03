"""Link-crawl smoke for Resemblio deployed surfaces.

Why this module exists
======================
On 2026-06-02 the Library v1.1 deploy returned green CI but every metadata
route in the rendered HTML resolved to a 500 page; the existing smoke gates
(`/v1/healthz`, `/v1/readyz`) returned 200 because those routes themselves
were healthy. The actual user experience was broken because LINKS in the
rendered HTML pointed at routes that 500'd. The same failure shape bit the
Susann WP staging on 2026-06-02 (nav links 404'd post-deploy even though the
homepage rendered clean).

This module is the standing PR gate that catches that failure shape: after a
deploy declares itself green, crawl the rendered HTML of every advertised
surface and assert every internal link returns 200 OR an expected 301 to a
known target. If any link 404s or 500s, the deploy is NOT green; the gate
exits non-zero so the workflow surfaces the regression before declaring
success.

What the smoke does
===================
1. Reads `projects/Resemblio/surfaces.yml` for the surface registry.
2. For each route, fetches the rendered HTML at `<base_url><route>`.
3. Parses the HTML via `html.parser` (stdlib) and extracts every `href` on
   `<a>` and every `src` on `<img>`, `<script>`, `<link>`. DOM parse, not
   regex; regex over HTML is a recurring failure shape (CTO 2026-06-02).
4. Classifies each extracted URL as `internal` (same registered host) or
   `external` (different host). External links are NOT crawled; the gate
   only asserts on the surface Resemblio owns.
5. Fetches each internal link with retry+backoff. Asserts 200, or 301 to a
   known-target documented in `surfaces.yml` (currently no known 301s; the
   plumbing exists for when the redirects middleware ships).
6. Writes a JSON report (`schema_version=link_crawl_report_v1`) with totals,
   per-link results, and an aggregate exit code.

Scope boundaries
================
- Does NOT log in. Authenticated surfaces (`/app/extractions`, `/app/account`
  per `surfaces.yml`) are crawled at the surface itself; their internal links
  that require auth are expected to return 200 (the page) and the gate only
  asserts on links visible in the unauthenticated render.
- Does NOT crawl recursively. Only links FOUND IN the registered surfaces
  are checked, one hop deep. Recursive crawling is out of scope for the v1
  PR gate; the registered surfaces are the contract.
- Does NOT validate content. That is the synthetic_probe module's job
  (body markers, URN leak detection). This module's job is link integrity.

Testability
===========
The HTTP fetcher is injected as a callable so unit tests can pass synthetic
fixtures with zero network. The DOM-parse and link-extract helpers are pure
functions with their own synthetic-HTML test coverage.

Schema
======
- Report JSON: `schema_version=link_crawl_report_v1`
- Each per-link result row: see `LinkResult` dataclass below.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import html.parser
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Literal

# --------------------------------------------------------------------------- #
# Schema versions + tuning constants                                          #
# --------------------------------------------------------------------------- #

REPORT_SCHEMA_VERSION = "link_crawl_report_v1"

# Retry delays for the HTTP fetcher (seconds). Three attempts total per URL.
# Same shape as `synthetic_probe.PROBE_RETRY_DELAYS_SEC`; keeps the workspace
# pattern consistent. Network blips during the post-deploy window are real;
# a single curl-equivalent miss should not red the gate.
FETCH_RETRY_DELAYS_SEC: tuple[float, ...] = (0.5, 1.5, 3.0)

# Per-request timeout. The standing `/v1/readyz` probe uses 8 retries with 5s
# sleeps for a 40s envelope; the link crawl exercises far more URLs so each
# one must be tight to keep total runtime bounded.
FETCH_TIMEOUT_SEC: float = 10.0

# User-Agent header. Identifying the gate distinctly so prod access logs
# distinguish smoke traffic from real users in the event of an investigation.
USER_AGENT: str = "Resemblio-LinkCrawlSmoke/1 (+https://resemblio.com)"

# Status codes considered "pass" for an internal link without an expected
# redirect target. 200 is the canonical success; 204 included for completeness
# on potential JSON endpoints that don't render a body.
PASS_STATUSES: frozenset[int] = frozenset({200, 204})

# Tags + attributes the HTML parser harvests. Centralized so the lint surface
# expands by editing this constant, not the parser body.
LINK_TAG_ATTRS: dict[str, str] = {
    "a": "href",
    "link": "href",
    "img": "src",
    "script": "src",
    "iframe": "src",
    "form": "action",
}

logger = logging.getLogger("resemblio.link_crawl")


# --------------------------------------------------------------------------- #
# Data shapes                                                                 #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Surface:
    """One row from the surfaces.yml registry.

    Attributes
    ----------
    name : str
        Human-readable surface name (e.g. `resemblio-web`).
    base_url : str
        Origin including scheme; no trailing slash.
    routes : tuple[str, ...]
        Path-only routes to crawl. Each begins with `/`.
    expect_status : int
        Status code the surface itself MUST return.
    """

    name: str
    base_url: str
    routes: tuple[str, ...]
    expect_status: int = 200


@dataclass(frozen=True)
class ExpectedRedirect:
    """A documented `from -> to` 301 that should pass the gate.

    Plumbing for when the redirects middleware ships (e.g. WP-import legacy
    paths). v1 ships with an empty registry; the data shape is here so adding
    a redirect is a single-line edit not a parser overhaul.
    """

    from_url: str
    to_url: str


@dataclass(frozen=True)
class LinkResult:
    """One row in the per-link report."""

    source_url: str
    link_url: str
    status: int  # -1 means fetch crashed entirely (network error)
    passed: bool
    error: str | None = None


@dataclass(frozen=True)
class CrawlReport:
    """Top-level report dumped to JSON.

    `schema_version` is the contract for downstream consumers (currently the
    deploy.yml step that prints failures; tomorrow possibly a dashboard).
    """

    schema_version: str
    generated_at: str  # ISO8601 UTC
    total_links_found: int
    total_internal_links: int
    total_passed: int
    total_failed: int
    surfaces_crawled: int
    results: tuple[LinkResult, ...]
    exit_code: Literal[0, 1]


# --------------------------------------------------------------------------- #
# HTML parsing (pure, unit-tested)                                            #
# --------------------------------------------------------------------------- #


class _LinkExtractor(html.parser.HTMLParser):
    """Collects href/src/action values from the configured tag set.

    Subclasses stdlib `html.parser.HTMLParser` rather than pulling in
    BeautifulSoup or lxml. Resemblio's rendered HTML is Next.js output;
    `html.parser` handles it correctly and avoids adding a runtime dep for
    a smoke gate that runs in CI.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        wanted_attr = LINK_TAG_ATTRS.get(tag.lower())
        if wanted_attr is None:
            return
        for attr_name, attr_value in attrs:
            if attr_name.lower() == wanted_attr and attr_value:
                self.links.append(attr_value)


def extract_links(html_body: str) -> list[str]:
    """Parse the given HTML and return every raw link value (href/src/action).

    Edge cases handled:
    - Malformed tags: html.parser is permissive; partial tags are skipped.
    - Empty hrefs / hrefs that are `#`: returned as-is; classification step
      drops them.
    - Duplicate links across the same document: preserved; the crawler will
      dedupe at fetch time.

    Returns links in document order so the report is stable across runs
    against the same input.
    """
    parser = _LinkExtractor()
    parser.feed(html_body)
    parser.close()
    return parser.links


def classify_link(
    raw_link: str,
    source_url: str,
    internal_hosts: frozenset[str],
) -> str | None:
    """Resolve a raw link against its source URL and return the absolute URL
    if-and-only-if it is internal and crawlable.

    Returns None for:
    - Empty or fragment-only links (`#`, `#section`).
    - `mailto:`, `tel:`, `javascript:`, `data:` and other non-http(s) schemes.
    - External hosts not in the registered internal-hosts set.

    Edge cases:
    - Protocol-relative links (`//cdn.example.com/...`) are resolved against
      the source URL's scheme; classified internal only if the host matches.
    - Path-relative links (`./foo`, `foo`) resolve against the source URL.
    - Anchor-bearing links (`/about#team`) are normalized by stripping the
      fragment; the page is what matters, not the anchor.
    """
    if not raw_link or raw_link.startswith("#"):
        return None
    scheme_marker = raw_link.split(":", 1)[0].lower() if ":" in raw_link else ""
    if scheme_marker in {"mailto", "tel", "javascript", "data"}:
        return None
    absolute = urllib.parse.urljoin(source_url, raw_link)
    parsed = urllib.parse.urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.hostname is None or parsed.hostname.lower() not in internal_hosts:
        return None
    # Strip fragment; page identity is what we crawl.
    normalized = parsed._replace(fragment="")
    return urllib.parse.urlunparse(normalized)


# --------------------------------------------------------------------------- #
# Surfaces loader                                                             #
# --------------------------------------------------------------------------- #


def load_surfaces_yaml(yaml_text: str) -> tuple[Surface, ...]:
    """Parse a surfaces.yml document into a tuple of Surface objects.

    Uses PyYAML if available (already a runtime dep); raises ImportError if
    the caller forgot to install. Validation is structural: missing required
    keys raise ValueError with the offending surface name in the message.
    """
    import yaml  # delayed import keeps the test surface clean

    parsed = yaml.safe_load(yaml_text)
    if not isinstance(parsed, dict) or "surfaces" not in parsed:
        raise ValueError("surfaces.yml missing top-level `surfaces` key")
    surfaces: list[Surface] = []
    for entry in parsed["surfaces"]:
        try:
            name = entry["name"]
            base_url = entry["base_url"].rstrip("/")
            routes = tuple(entry.get("routes", ()))
            expect_status = int(entry.get("expect_status", 200))
        except (KeyError, TypeError) as exc:
            raise ValueError(f"surfaces.yml entry malformed: {entry!r}") from exc
        surfaces.append(
            Surface(
                name=name,
                base_url=base_url,
                routes=routes,
                expect_status=expect_status,
            )
        )
    return tuple(surfaces)


def internal_hosts_from_surfaces(surfaces: Iterable[Surface]) -> frozenset[str]:
    """Collect the hostname set treated as internal for classification."""
    hosts: set[str] = set()
    for surface in surfaces:
        host = urllib.parse.urlparse(surface.base_url).hostname
        if host:
            hosts.add(host.lower())
    return frozenset(hosts)


# --------------------------------------------------------------------------- #
# HTTP fetcher with retry                                                     #
# --------------------------------------------------------------------------- #


# Fetcher callable shape: (url) -> (status_code, body_text)
# status_code -1 signals fetch crashed entirely (DNS, connect, timeout).
Fetcher = Callable[[str], tuple[int, str]]


def default_fetcher(url: str) -> tuple[int, str]:
    """Stdlib-only HTTP fetcher with retry + exponential-ish backoff.

    Why stdlib instead of httpx: this script runs in CI on every deploy. The
    GitHub Actions runner has python stdlib guaranteed; adding httpx as a
    runtime dep just for the smoke gate would inflate the install surface.
    The unit tests inject a synthetic fetcher and never touch this function.

    Retry policy: three attempts using FETCH_RETRY_DELAYS_SEC. A 5xx or a
    network crash triggers retry; a 4xx returns immediately (the route is
    not flaky, it is broken). 3xx returns the redirect status without
    following; the caller decides whether the target is in the expected-301
    registry.
    """
    last_status = -1
    last_error: str | None = None
    for attempt, delay in enumerate(FETCH_RETRY_DELAYS_SEC, start=1):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT},
                method="GET",
            )
            with urllib.request.urlopen(  # noqa: S310 -- crawler over our own surfaces
                request,
                timeout=FETCH_TIMEOUT_SEC,
            ) as response:
                body_bytes = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                try:
                    body = body_bytes.decode(charset, errors="replace")
                except LookupError:
                    body = body_bytes.decode("utf-8", errors="replace")
                return response.status, body
        except urllib.error.HTTPError as http_err:
            # 4xx: not flaky, just broken; return immediately.
            if 400 <= http_err.code < 500:
                try:
                    body = http_err.read().decode("utf-8", errors="replace")
                except Exception:
                    body = ""
                return http_err.code, body
            last_status = http_err.code
            last_error = f"HTTP {http_err.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as net_err:
            last_status = -1
            last_error = f"{type(net_err).__name__}: {net_err}"
        if attempt < len(FETCH_RETRY_DELAYS_SEC):
            time.sleep(delay)
    logger.warning("fetch failed for %s after retries: %s", url, last_error)
    return last_status, ""


# --------------------------------------------------------------------------- #
# Crawl orchestration                                                         #
# --------------------------------------------------------------------------- #


def evaluate_link_status(
    link_url: str,
    status: int,
    expected_redirects: dict[str, str],
) -> tuple[bool, str | None]:
    """Decide whether a link's observed status counts as a pass.

    A pass is one of:
    - status in PASS_STATUSES
    - status is a redirect (301/302/307/308) AND the link is in the
      expected-redirects registry (target match is not enforced at this
      layer; the registry presence is the contract)

    Returns (passed, error_message). error_message is None on pass.
    """
    if status in PASS_STATUSES:
        return True, None
    if status in {301, 302, 307, 308} and link_url in expected_redirects:
        return True, None
    if status == -1:
        return False, "fetch crashed (network error / DNS / timeout)"
    return False, f"unexpected status {status}"


def crawl_surfaces(
    surfaces: tuple[Surface, ...],
    fetcher: Fetcher = default_fetcher,
    expected_redirects: tuple[ExpectedRedirect, ...] = (),
) -> CrawlReport:
    """Crawl every registered surface and return a CrawlReport.

    Per-surface flow:
    1. Fetch the surface URL itself; if it does not return expect_status,
       record a failure for that surface (the surface IS a link).
    2. Extract links from the rendered HTML.
    3. Classify each link as internal or external.
    4. Fetch each unique internal link once (deduped across the entire
       crawl, not just per surface; same URL referenced from /home and
       /pricing is one fetch).
    5. Evaluate each fetch against the pass criteria.

    Returns a frozen CrawlReport. Exit code is 0 iff every link passed.
    """
    internal_hosts = internal_hosts_from_surfaces(surfaces)
    redirect_map = {r.from_url: r.to_url for r in expected_redirects}

    all_results: list[LinkResult] = []
    seen_links: set[str] = set()
    total_links_found = 0
    total_internal_links = 0

    for surface in surfaces:
        for route in surface.routes:
            source_url = f"{surface.base_url}{route}"

            # Step 1: fetch the surface itself; it IS a link the gate must check.
            if source_url not in seen_links:
                seen_links.add(source_url)
                status, body = fetcher(source_url)
                passed, err = evaluate_link_status(source_url, status, redirect_map)
                if status != surface.expect_status and status in PASS_STATUSES:
                    # The registered expect_status is the source of truth; a
                    # surface that registers expect_status=204 but returns 200
                    # is a contract drift the gate should surface.
                    passed = status == surface.expect_status
                    err = (
                        f"surface returned {status}; expected {surface.expect_status}"
                        if not passed
                        else None
                    )
                all_results.append(
                    LinkResult(
                        source_url=source_url,
                        link_url=source_url,
                        status=status,
                        passed=passed,
                        error=err,
                    )
                )
                if not passed:
                    # If the surface itself failed, don't try to parse a body
                    # that may be a 5xx error page; skip link extraction.
                    continue
            else:
                # Surface already crawled (shouldn't happen given how the
                # registry is shaped, but stays defensive).
                status, body = fetcher(source_url)

            # Step 2 + 3: extract + classify.
            raw_links = extract_links(body)
            total_links_found += len(raw_links)
            classified: list[str] = []
            for raw in raw_links:
                resolved = classify_link(raw, source_url, internal_hosts)
                if resolved is not None:
                    classified.append(resolved)
            total_internal_links += len(classified)

            # Step 4 + 5: fetch + evaluate each unique internal link.
            for link_url in classified:
                if link_url in seen_links:
                    continue
                seen_links.add(link_url)
                link_status, _ = fetcher(link_url)
                passed, err = evaluate_link_status(link_url, link_status, redirect_map)
                all_results.append(
                    LinkResult(
                        source_url=source_url,
                        link_url=link_url,
                        status=link_status,
                        passed=passed,
                        error=err,
                    )
                )

    total_passed = sum(1 for r in all_results if r.passed)
    total_failed = sum(1 for r in all_results if not r.passed)
    exit_code: Literal[0, 1] = 0 if total_failed == 0 else 1

    return CrawlReport(
        schema_version=REPORT_SCHEMA_VERSION,
        generated_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
        total_links_found=total_links_found,
        total_internal_links=total_internal_links,
        total_passed=total_passed,
        total_failed=total_failed,
        surfaces_crawled=sum(len(s.routes) for s in surfaces),
        results=tuple(all_results),
        exit_code=exit_code,
    )


def report_to_dict(report: CrawlReport) -> dict:
    """Convert the report to a JSON-serializable dict.

    `dataclasses.asdict` walks nested dataclasses; tuples become lists in
    JSON. Done here (rather than at the call site) so the JSON shape is
    captured in one place under the schema_version contract.
    """
    return dataclasses.asdict(report)

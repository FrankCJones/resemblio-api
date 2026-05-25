"""Deterministic recon for a candidate source.

Replaces the ~5K-token LLM recon agent that crawls 8-15 pages and synthesizes
a recon doc. Python does the crawl + classification + reachability matrix
deterministically; the LLM call (when needed) only adds the design_principles
synthesis at the very end.

## What it does

1. Fetches `/sitemap.xml` (and `/sitemap_index.xml` fallback). Pulls every
   URL declared by the source.
2. If no sitemap, falls back to homepage parse: fetches `/`, extracts every
   `<a href>` in the head + nav, builds a candidate URL list.
3. Classifies URLs by regex matching against canonical page-type patterns
   (`/pricing`, `/blog/`, `/customers/`, `/docs/`, `/about/`, `/news/`,
   etc.).
4. For each canonical page-type, picks ONE representative URL and runs a
   reachability probe (HTTP HEAD via `urllib`, content-type check, stub-
   markdown detection).
5. Emits `_INBOX/recon_<slug>_<date>.json` conforming to `ReconRecord`.

## What it does NOT do

- Does not extract design tokens. That's the extraction agent's job
  (Phase B contract).
- Does not write asset files. That's compose.py.
- Does not synthesize design_principles. The LLM is still better at that
  (about 3K tokens); recon.py emits an empty principles list that the
  orchestrator fills in afterwards.

## Retry + backoff

Network calls use `urllib.request` with explicit timeouts + retry on
transient failures (connection reset, 5xx). Three attempts max with
exponential backoff (1s, 2s, 4s).

## Run command

    python -m _scripts.recon <slug> --url https://x.com
    python -m _scripts.recon <slug> --url https://x.com --dry  # don't write JSON
    python -m _scripts.test_recon                              # unit tests

Throwaway: no. Quality floor applies.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import TypedDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INBOX_ROOT = PROJECT_ROOT / "_INBOX"

SCHEMA_VERSION = 1
"""Bump on incompatible ReconRecord shape changes."""

DEFAULT_TIMEOUT = 12.0
"""Seconds before a single HTTP call gives up."""

MAX_RETRIES = 3
"""Per-URL retry budget for transient failures."""

USER_AGENT = "Mozilla/5.0 (compatible; DesignReferenceLibrary-recon/1.0)"
"""Sent on every request. Polite identification."""

STUB_MARKDOWN_MARKERS: tuple[str, ...] = (
    "auto-generated markdown",
    "ai-optimized",
    "navigation to this domain is not allowed",
    "<!-- llm-optimized -->",
    "sit tight",  # Patagonia SPA-failover marker
    "service unavailable",
)
"""Substrings whose presence in a body marks the response as a stub, not real
content. Lowercased before matching."""

# Canonical page-type detector. Each entry: (page_type, list of regex patterns).
# Order matters: first match wins. More specific patterns earlier.
PAGE_TYPE_PATTERNS: list[tuple[str, list[re.Pattern]]] = [
    ("pricing", [re.compile(r"/pricing(?:/|$|\?)", re.I),
                 re.compile(r"/plans(?:/|$|\?)", re.I)]),
    ("docs", [re.compile(r"/docs?/", re.I),
              re.compile(r"^https?://(developers?|docs|support)\.", re.I),
              re.compile(r"/help/?", re.I),
              re.compile(r"/university/", re.I)]),
    ("article", [re.compile(r"/blog/[a-zA-Z0-9_-]+/?$", re.I),
                 re.compile(r"^https?://blog\.", re.I),
                 re.compile(r"/posts?/[a-zA-Z0-9_-]+", re.I),
                 re.compile(r"/news/[a-zA-Z0-9_-]+", re.I)]),
    ("customer-story", [re.compile(r"/customers?/[a-zA-Z0-9_-]+", re.I),
                        re.compile(r"/case-studies?/[a-zA-Z0-9_-]+", re.I),
                        re.compile(r"/stories/[a-zA-Z0-9_-]+", re.I)]),
    ("about", [re.compile(r"/about/?", re.I),
               re.compile(r"/company/?", re.I)]),
    ("research", [re.compile(r"/research/?", re.I),
                  re.compile(r"/publications?/?", re.I)]),
    ("marketing", [re.compile(r"^https?://[^/]+/?$", re.I)]),
]
"""Page-type detection. Matched in order; first match assigns the URL."""


# ----------------------------------------------------------------------
# Type contracts
# ----------------------------------------------------------------------


class ReachabilityProbe(TypedDict):
    """Result of one HEAD/GET probe."""
    url: str
    status_code: int           # 0 if network error
    content_type: str
    is_stub: bool              # body looks like an LLM stub or failover page
    error: str | None          # error message if probe failed


class PageTypeCandidate(TypedDict):
    """One URL classified to a canonical page-type."""
    page_type: str             # marketing, about, pricing, etc.
    url: str
    reachability: ReachabilityProbe


class ReconRecord(TypedDict):
    """The structured output of a recon pass."""
    schema_version: int
    system_slug: str
    source_url: str
    captured: str              # ISO date
    sitemap_found: bool
    total_urls_discovered: int
    candidates: list[PageTypeCandidate]   # one per applicable page-type
    not_applicable: list[str]             # page-types not found on the source
    homepage_reachability: ReachabilityProbe
    design_principles: list[str]          # empty; orchestrator fills in
    notes: list[str]                      # observations from the crawl


# ----------------------------------------------------------------------
# Network helpers (retry-aware)
# ----------------------------------------------------------------------


def _fetch(url: str, *, method: str = "GET",
           timeout: float = DEFAULT_TIMEOUT) -> tuple[int, str, bytes]:
    """Fetch a URL with retry + backoff. Returns (status, content_type, body).

    Returns (0, "", b"") on terminal failure (after retries). Raises only
    for programming errors (bad URL).
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read() if method == "GET" else b""
                ctype = resp.headers.get("Content-Type", "")
                return (resp.status, ctype, body)
        except urllib.error.HTTPError as e:
            # 4xx is not retriable.
            if 400 <= e.code < 500:
                return (e.code, e.headers.get("Content-Type", ""), b"")
            last_error = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_error = e
        if attempt < MAX_RETRIES - 1:
            time.sleep(2 ** attempt)
    return (0, "", b"")


def probe(url: str) -> ReachabilityProbe:
    """Probe a URL with HEAD first, fall back to GET if HEAD returns
    method-not-allowed or empty body. Detects stub-markdown responses.
    """
    status, ctype, _body = _fetch(url, method="HEAD")
    # HEAD returns no body; for stub detection we need GET. If status is
    # 200/3xx and ctype is text/html, do a GET to inspect.
    body_str = ""
    if status in (0, 405) or (status == 200 and "html" not in ctype.lower()):
        status, ctype, body = _fetch(url, method="GET")
        body_str = body.decode("utf-8", errors="replace").lower()[:8000]
    elif status == 200:
        # Pull a small GET sample for stub detection.
        _status_g, _ctype_g, body = _fetch(url, method="GET")
        body_str = body.decode("utf-8", errors="replace").lower()[:8000]

    is_stub = any(marker in body_str for marker in STUB_MARKDOWN_MARKERS)
    return ReachabilityProbe(
        url=url, status_code=status, content_type=ctype,
        is_stub=is_stub,
        error=None if status >= 200 and status < 400 else f"status {status}",
    )


# ----------------------------------------------------------------------
# Sitemap + URL classification
# ----------------------------------------------------------------------


def fetch_sitemap_urls(base_url: str) -> list[str]:
    """Read /sitemap.xml (and /sitemap_index.xml as fallback). Returns list
    of URLs declared by the source. Empty list if no sitemap.
    """
    candidates = [
        urllib.parse.urljoin(base_url, "/sitemap.xml"),
        urllib.parse.urljoin(base_url, "/sitemap_index.xml"),
        urllib.parse.urljoin(base_url, "/sitemap-index.xml"),
    ]
    urls: list[str] = []
    for sm_url in candidates:
        status, ctype, body = _fetch(sm_url, method="GET")
        if status != 200 or not body:
            continue
        text = body.decode("utf-8", errors="replace")
        # Sitemap-index: nested sitemaps. Recurse one level.
        if "<sitemapindex" in text:
            inner = re.findall(r"<loc>([^<]+)</loc>", text)
            for inner_url in inner[:5]:  # cap recursion breadth
                _status_i, _ctype_i, body_i = _fetch(inner_url, method="GET")
                if body_i:
                    inner_text = body_i.decode("utf-8", errors="replace")
                    urls.extend(re.findall(r"<loc>([^<]+)</loc>", inner_text))
            return _dedupe(urls)
        urls.extend(re.findall(r"<loc>([^<]+)</loc>", text))
        if urls:
            return _dedupe(urls)
    return []


def _dedupe(urls: list[str]) -> list[str]:
    """Stable dedupe; preserves order."""
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def classify_url(url: str) -> str | None:
    """Match a URL against PAGE_TYPE_PATTERNS, return page_type or None."""
    for page_type, patterns in PAGE_TYPE_PATTERNS:
        for pat in patterns:
            if pat.search(url):
                return page_type
    return None


def pick_representatives(urls: list[str]) -> dict[str, str]:
    """For each canonical page-type, pick the first matching URL.

    Returns dict[page_type, url]. Page-types not present are simply missing
    from the dict.
    """
    chosen: dict[str, str] = {}
    for url in urls:
        page_type = classify_url(url)
        if page_type and page_type not in chosen:
            chosen[page_type] = url
    return chosen


# ----------------------------------------------------------------------
# Homepage-link fallback
# ----------------------------------------------------------------------


def extract_homepage_links(homepage_url: str) -> list[str]:
    """Pull every <a href> from the homepage HTML. Resolves relative URLs."""
    status, _ctype, body = _fetch(homepage_url, method="GET")
    if status != 200 or not body:
        return []
    text = body.decode("utf-8", errors="replace")
    raw = re.findall(r'<a\b[^>]*\bhref=["\']([^"\']+)["\']', text, flags=re.I)
    urls: list[str] = []
    for href in raw:
        if href.startswith(("javascript:", "mailto:", "#")):
            continue
        absolute = urllib.parse.urljoin(homepage_url, href)
        if absolute.startswith("http"):
            urls.append(absolute)
    return _dedupe(urls)


# ----------------------------------------------------------------------
# Top-level recon
# ----------------------------------------------------------------------


def recon(slug: str, url: str) -> ReconRecord:
    """Run the full recon pipeline for one source.

    Returns a ReconRecord ready to write to `_INBOX/recon_<slug>_<date>.json`.
    """
    home_probe = probe(url)
    sitemap_urls = fetch_sitemap_urls(url)
    sitemap_found = bool(sitemap_urls)
    notes: list[str] = []

    if not sitemap_found:
        notes.append("no sitemap found; falling back to homepage link scrape")
        sitemap_urls = extract_homepage_links(url)

    representatives = pick_representatives(sitemap_urls)

    candidates: list[PageTypeCandidate] = []
    canonical_types = ["marketing", "about", "pricing", "customer-story",
                       "article", "docs", "research"]
    for page_type in canonical_types:
        if page_type == "marketing":
            # Always probe the homepage itself for the marketing slot.
            candidates.append(PageTypeCandidate(
                page_type="marketing", url=url, reachability=home_probe,
            ))
            continue
        candidate_url = representatives.get(page_type)
        if not candidate_url:
            continue
        candidates.append(PageTypeCandidate(
            page_type=page_type,
            url=candidate_url,
            reachability=probe(candidate_url),
        ))

    not_applicable = [pt for pt in canonical_types
                      if pt != "marketing" and pt not in representatives]

    # If the homepage is a stub or unreachable, surface that loudly.
    if home_probe["is_stub"]:
        notes.append("homepage returned a stub/failover response — site "
                     "may be in outage or LLM-stub mode")
    if home_probe["status_code"] >= 400 or home_probe["status_code"] == 0:
        notes.append(f"homepage unreachable: {home_probe.get('error')}")

    return ReconRecord(
        schema_version=SCHEMA_VERSION,
        system_slug=slug,
        source_url=url,
        captured=dt.date.today().isoformat(),
        sitemap_found=sitemap_found,
        total_urls_discovered=len(sitemap_urls),
        candidates=candidates,
        not_applicable=not_applicable,
        homepage_reachability=home_probe,
        design_principles=[],
        notes=notes,
    )


def write_recon(record: ReconRecord) -> Path:
    """Persist a ReconRecord to `_INBOX/recon_<slug>_<date>.json`."""
    INBOX_ROOT.mkdir(parents=True, exist_ok=True)
    out_path = (INBOX_ROOT
                / f"recon_{record['system_slug']}_{record['captured']}.json")
    out_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return out_path


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI entry. Returns shell exit code."""
    ap = argparse.ArgumentParser(
        description="Deterministic recon: sitemap + URL classify + reachability.",
    )
    ap.add_argument("slug", help="System slug.")
    ap.add_argument("--url", required=True, help="Source homepage URL.")
    ap.add_argument("--dry", action="store_true",
                    help="Print JSON to stdout; do not write to disk.")
    args = ap.parse_args(argv)

    record = recon(args.slug, args.url)
    if args.dry:
        print(json.dumps(record, indent=2))
        return 0

    out = write_recon(record)
    rel = out.relative_to(PROJECT_ROOT)
    print(f"Wrote {rel}")
    print(f"  sitemap_found: {record['sitemap_found']}")
    print(f"  candidates: {len(record['candidates'])} page-types")
    print(f"  not_applicable: {record['not_applicable']}")
    if record["notes"]:
        print("  notes:")
        for n in record["notes"]:
            print(f"    - {n}")
    return 0 if record["homepage_reachability"]["status_code"] < 400 else 1


if __name__ == "__main__":
    sys.exit(main())

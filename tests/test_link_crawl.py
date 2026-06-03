"""Unit tests for app.monitoring.link_crawl (Resemblio link-crawl smoke gate).

Covers the behaviors the production gate MUST get right:

1. DOM parse extracts links from a/img/script/link/iframe/form tags (no regex)
2. Classification correctly separates internal vs external vs non-http schemes
3. Anchor-only and mailto/tel/javascript/data links are dropped
4. Surfaces.yml loader validates structure + raises on malformed entries
5. Pass evaluation: 200 passes; 4xx/5xx fail; 301 passes only if in registry
6. Crawl orchestration: end-to-end against a synthetic fetcher with happy
   path, broken-link path, and surface-itself-fails path
7. Report schema_version is present + JSON-serializable

All network is fed through a synthetic fetcher callable; no live IO.
"""
from __future__ import annotations

import json

import pytest

from app.monitoring.link_crawl import (
    FETCH_RETRY_DELAYS_SEC,
    PASS_STATUSES,
    REPORT_SCHEMA_VERSION,
    CrawlReport,
    ExpectedRedirect,
    LinkResult,
    Surface,
    classify_link,
    crawl_surfaces,
    evaluate_link_status,
    extract_links,
    internal_hosts_from_surfaces,
    load_surfaces_yaml,
    report_to_dict,
)


# --------------------------------------------------------------------------- #
# extract_links                                                               #
# --------------------------------------------------------------------------- #


def test_extract_links_finds_anchor_href() -> None:
    html = '<html><body><a href="/foo">Foo</a><a href="/bar">Bar</a></body></html>'
    assert extract_links(html) == ["/foo", "/bar"]


def test_extract_links_handles_all_configured_tag_attrs() -> None:
    html = (
        '<html><head>'
        '<link rel="stylesheet" href="/styles.css">'
        '<script src="/app.js"></script>'
        '</head><body>'
        '<a href="/about">About</a>'
        '<img src="/logo.png">'
        '<iframe src="/embed"></iframe>'
        '<form action="/submit"></form>'
        '</body></html>'
    )
    found = extract_links(html)
    assert "/styles.css" in found
    assert "/app.js" in found
    assert "/about" in found
    assert "/logo.png" in found
    assert "/embed" in found
    assert "/submit" in found


def test_extract_links_ignores_unconfigured_tags() -> None:
    html = '<div data-href="/should-not-appear">x</div><span src="/nope">y</span>'
    assert extract_links(html) == []


def test_extract_links_skips_empty_attributes() -> None:
    html = '<a href="">empty</a><a href="/ok">ok</a>'
    assert extract_links(html) == ["/ok"]


def test_extract_links_preserves_order_for_stable_reports() -> None:
    html = '<a href="/z">z</a><a href="/a">a</a><a href="/m">m</a>'
    assert extract_links(html) == ["/z", "/a", "/m"]


def test_extract_links_tolerates_malformed_html() -> None:
    # html.parser is permissive; unterminated tags do not crash.
    html = '<a href="/ok">ok<unterminated <a href="/also-ok">also</a>'
    found = extract_links(html)
    assert "/ok" in found


# --------------------------------------------------------------------------- #
# classify_link                                                               #
# --------------------------------------------------------------------------- #


INTERNAL = frozenset({"resemblio.com", "api.resemblio.com"})


def test_classify_link_drops_fragment_only() -> None:
    assert classify_link("#section", "https://resemblio.com/", INTERNAL) is None


def test_classify_link_drops_empty() -> None:
    assert classify_link("", "https://resemblio.com/", INTERNAL) is None


@pytest.mark.parametrize(
    "scheme_link",
    [
        "mailto:hi@resemblio.com",
        "tel:+19195551234",
        "javascript:void(0)",
        "data:text/plain,abc",
    ],
)
def test_classify_link_drops_non_http_schemes(scheme_link: str) -> None:
    assert classify_link(scheme_link, "https://resemblio.com/", INTERNAL) is None


def test_classify_link_resolves_relative() -> None:
    out = classify_link("about", "https://resemblio.com/blog/", INTERNAL)
    assert out == "https://resemblio.com/blog/about"


def test_classify_link_resolves_absolute_path() -> None:
    out = classify_link("/library", "https://resemblio.com/", INTERNAL)
    assert out == "https://resemblio.com/library"


def test_classify_link_keeps_absolute_internal_url() -> None:
    out = classify_link(
        "https://api.resemblio.com/v1/healthz",
        "https://resemblio.com/",
        INTERNAL,
    )
    assert out == "https://api.resemblio.com/v1/healthz"


def test_classify_link_drops_external_host() -> None:
    out = classify_link("https://google.com/", "https://resemblio.com/", INTERNAL)
    assert out is None


def test_classify_link_strips_fragment_for_dedupe() -> None:
    out = classify_link("/about#team", "https://resemblio.com/", INTERNAL)
    assert out == "https://resemblio.com/about"


def test_classify_link_protocol_relative_resolves_against_source_scheme() -> None:
    out = classify_link(
        "//resemblio.com/foo",
        "https://resemblio.com/source",
        INTERNAL,
    )
    assert out == "https://resemblio.com/foo"


# --------------------------------------------------------------------------- #
# load_surfaces_yaml + internal_hosts_from_surfaces                           #
# --------------------------------------------------------------------------- #


CANONICAL_YAML = """
schema_version: 1
project: resemblio
surfaces:
  - name: resemblio-web
    base_url: https://resemblio.com
    routes:
      - /
      - /app
    expect_status: 200
  - name: resemblio-api
    base_url: https://api.resemblio.com
    routes:
      - /v1/healthz
    expect_status: 200
"""


def test_load_surfaces_yaml_parses_canonical_shape() -> None:
    surfaces = load_surfaces_yaml(CANONICAL_YAML)
    assert len(surfaces) == 2
    assert surfaces[0].name == "resemblio-web"
    assert surfaces[0].base_url == "https://resemblio.com"
    assert surfaces[0].routes == ("/", "/app")
    assert surfaces[1].base_url == "https://api.resemblio.com"


def test_load_surfaces_yaml_strips_trailing_slash_on_base_url() -> None:
    yaml_text = """
surfaces:
  - name: web
    base_url: https://resemblio.com/
    routes: [/]
"""
    surfaces = load_surfaces_yaml(yaml_text)
    assert surfaces[0].base_url == "https://resemblio.com"


def test_load_surfaces_yaml_raises_on_missing_top_level() -> None:
    with pytest.raises(ValueError, match="missing top-level"):
        load_surfaces_yaml("not: a surface registry\n")


def test_load_surfaces_yaml_raises_on_malformed_entry() -> None:
    with pytest.raises(ValueError, match="malformed"):
        load_surfaces_yaml(
            """
surfaces:
  - name: web
    # base_url missing
    routes: [/]
"""
        )


def test_internal_hosts_extracts_both_origins() -> None:
    surfaces = load_surfaces_yaml(CANONICAL_YAML)
    hosts = internal_hosts_from_surfaces(surfaces)
    assert hosts == frozenset({"resemblio.com", "api.resemblio.com"})


# --------------------------------------------------------------------------- #
# evaluate_link_status                                                        #
# --------------------------------------------------------------------------- #


def test_evaluate_link_status_passes_200() -> None:
    passed, err = evaluate_link_status("https://resemblio.com/", 200, {})
    assert passed is True
    assert err is None


def test_evaluate_link_status_passes_204() -> None:
    passed, _ = evaluate_link_status("https://resemblio.com/x", 204, {})
    assert passed is True


def test_evaluate_link_status_fails_404() -> None:
    passed, err = evaluate_link_status("https://resemblio.com/missing", 404, {})
    assert passed is False
    assert "404" in (err or "")


def test_evaluate_link_status_fails_500() -> None:
    passed, err = evaluate_link_status("https://resemblio.com/boom", 500, {})
    assert passed is False
    assert "500" in (err or "")


def test_evaluate_link_status_passes_301_when_registered() -> None:
    passed, err = evaluate_link_status(
        "https://resemblio.com/old",
        301,
        {"https://resemblio.com/old": "https://resemblio.com/new"},
    )
    assert passed is True
    assert err is None


def test_evaluate_link_status_fails_301_when_not_registered() -> None:
    passed, err = evaluate_link_status("https://resemblio.com/sneaky", 301, {})
    assert passed is False


def test_evaluate_link_status_fails_on_fetch_crash() -> None:
    passed, err = evaluate_link_status("https://resemblio.com/x", -1, {})
    assert passed is False
    assert "crashed" in (err or "")


# --------------------------------------------------------------------------- #
# crawl_surfaces orchestration (end-to-end with synthetic fetcher)            #
# --------------------------------------------------------------------------- #


def _make_fetcher(routes: dict[str, tuple[int, str]]):
    """Build a synthetic fetcher that returns (status, body) per URL.

    URLs not in the map return (404, '') so missing-link cases are easy.
    """

    def fetch(url: str) -> tuple[int, str]:
        return routes.get(url, (404, ""))

    return fetch


def test_crawl_happy_path_all_links_pass() -> None:
    surfaces = (
        Surface(
            name="web",
            base_url="https://resemblio.com",
            routes=("/",),
            expect_status=200,
        ),
    )
    home_html = (
        '<html><body>'
        '<a href="/about">About</a>'
        '<a href="/pricing">Pricing</a>'
        '<a href="https://external.com/x">External (ignored)</a>'
        '</body></html>'
    )
    fetcher = _make_fetcher(
        {
            "https://resemblio.com/": (200, home_html),
            "https://resemblio.com/about": (200, "<html></html>"),
            "https://resemblio.com/pricing": (200, "<html></html>"),
        }
    )

    report = crawl_surfaces(surfaces, fetcher=fetcher)

    assert report.exit_code == 0
    assert report.total_failed == 0
    assert report.surfaces_crawled == 1
    # 3 raw links in HTML (2 internal + 1 external)
    assert report.total_links_found == 3
    # 2 internal after classification
    assert report.total_internal_links == 2
    # Surface itself + 2 internal links = 3 results
    assert report.total_passed == 3


def test_crawl_flags_failing_internal_link() -> None:
    """The 2026-06-02 failure shape: surface is 200 but a link 500s."""
    surfaces = (
        Surface(
            name="web",
            base_url="https://resemblio.com",
            routes=("/",),
        ),
    )
    home_html = (
        '<a href="/library/aeon/buttons/">Aeon Buttons</a>'
        '<a href="/library/aeon/colors/">Aeon Colors</a>'
    )
    fetcher = _make_fetcher(
        {
            "https://resemblio.com/": (200, home_html),
            "https://resemblio.com/library/aeon/buttons/": (500, ""),
            "https://resemblio.com/library/aeon/colors/": (200, ""),
        }
    )

    report = crawl_surfaces(surfaces, fetcher=fetcher)

    assert report.exit_code == 1
    assert report.total_failed == 1
    failing = [r for r in report.results if not r.passed]
    assert len(failing) == 1
    assert failing[0].link_url == "https://resemblio.com/library/aeon/buttons/"
    assert failing[0].status == 500


def test_crawl_flags_404_link_susann_nav_shape() -> None:
    """WP-nav 404 shape locked 2026-06-02 on Susann staging."""
    surfaces = (
        Surface(
            name="web",
            base_url="https://resemblio.com",
            routes=("/",),
        ),
    )
    home_html = '<a href="/about-us/">About</a>'
    fetcher = _make_fetcher(
        {
            "https://resemblio.com/": (200, home_html),
            "https://resemblio.com/about-us/": (404, ""),
        }
    )

    report = crawl_surfaces(surfaces, fetcher=fetcher)

    assert report.exit_code == 1
    assert any(r.status == 404 and not r.passed for r in report.results)


def test_crawl_fails_when_surface_itself_returns_500() -> None:
    surfaces = (
        Surface(
            name="web",
            base_url="https://resemblio.com",
            routes=("/",),
        ),
    )
    fetcher = _make_fetcher({"https://resemblio.com/": (500, "")})
    report = crawl_surfaces(surfaces, fetcher=fetcher)
    assert report.exit_code == 1
    assert report.total_failed == 1


def test_crawl_skips_link_extraction_when_surface_failed() -> None:
    """Don't parse links out of a 5xx error page body."""
    surfaces = (
        Surface(
            name="web",
            base_url="https://resemblio.com",
            routes=("/",),
        ),
    )
    error_body = '<html><a href="/nonsense-from-error-page">x</a></html>'
    fetcher = _make_fetcher({"https://resemblio.com/": (500, error_body)})
    report = crawl_surfaces(surfaces, fetcher=fetcher)
    # Only the surface itself appears in results; no /nonsense-from-error-page.
    assert all(
        r.link_url != "https://resemblio.com/nonsense-from-error-page"
        for r in report.results
    )


def test_crawl_dedupes_same_link_across_surfaces() -> None:
    surfaces = (
        Surface(
            name="web",
            base_url="https://resemblio.com",
            routes=("/", "/about"),
        ),
    )
    body_with_shared_link = '<a href="/contact">Contact</a>'
    fetcher = _make_fetcher(
        {
            "https://resemblio.com/": (200, body_with_shared_link),
            "https://resemblio.com/about": (200, body_with_shared_link),
            "https://resemblio.com/contact": (200, ""),
        }
    )
    report = crawl_surfaces(surfaces, fetcher=fetcher)
    contact_results = [
        r for r in report.results if r.link_url == "https://resemblio.com/contact"
    ]
    assert len(contact_results) == 1  # deduped, not fetched twice


def test_crawl_accepts_documented_301() -> None:
    surfaces = (
        Surface(
            name="web",
            base_url="https://resemblio.com",
            routes=("/",),
        ),
    )
    home_html = '<a href="/blog/old-post/">Old post</a>'
    fetcher = _make_fetcher(
        {
            "https://resemblio.com/": (200, home_html),
            "https://resemblio.com/blog/old-post/": (301, ""),
        }
    )
    redirects = (
        ExpectedRedirect(
            from_url="https://resemblio.com/blog/old-post/",
            to_url="https://resemblio.com/posts/old-post/",
        ),
    )
    report = crawl_surfaces(surfaces, fetcher=fetcher, expected_redirects=redirects)
    assert report.exit_code == 0


# --------------------------------------------------------------------------- #
# Report shape + schema_version                                               #
# --------------------------------------------------------------------------- #


def test_report_carries_schema_version() -> None:
    surfaces = (
        Surface(name="web", base_url="https://resemblio.com", routes=("/",)),
    )
    fetcher = _make_fetcher({"https://resemblio.com/": (200, "<html></html>")})
    report = crawl_surfaces(surfaces, fetcher=fetcher)
    assert report.schema_version == REPORT_SCHEMA_VERSION


def test_report_serializes_to_json() -> None:
    surfaces = (
        Surface(name="web", base_url="https://resemblio.com", routes=("/",)),
    )
    fetcher = _make_fetcher({"https://resemblio.com/": (200, "<html></html>")})
    report = crawl_surfaces(surfaces, fetcher=fetcher)
    as_dict = report_to_dict(report)
    serialized = json.dumps(as_dict)
    parsed = json.loads(serialized)
    assert parsed["schema_version"] == REPORT_SCHEMA_VERSION
    assert parsed["exit_code"] == 0
    assert "results" in parsed
    assert isinstance(parsed["results"], list)


def test_retry_delays_constant_has_three_attempts() -> None:
    """Documents the retry policy contract (matches synthetic_probe)."""
    assert len(FETCH_RETRY_DELAYS_SEC) == 3


def test_pass_statuses_contains_200() -> None:
    assert 200 in PASS_STATUSES

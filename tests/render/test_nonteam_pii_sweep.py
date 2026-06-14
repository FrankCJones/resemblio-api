"""Tests for the non-about-team PII sweep (Phase 17).

Covers testimonials, article-layout, and news-list categories. These categories
can carry real-person photos (customer headshots in testimonials, author photos
in article layouts, contributor photos in news feeds).

The sweep is HTML-based (no Playwright). It scans img src attributes for
suspicious person-photo URL patterns and classifies each (brand, category)
pair as CLEAN, NA, or UNVERIFIED. UNVERIFIED (not LEAK) means suspicious
pattern found and needing Playwright/human follow-up before reporting LEAK.

This is intentionally different from Phase 16 (about-team), which used
Playwright for DOM-level precision. For non-team categories the risk is more
diffuse and an HTML scan with a conservative UNVERIFIED verdict is sufficient.

Test file: Phase 17.1-17.4 (separate RED/GREEN commits per sub-phase).
All tests use synthetic fixtures; no network calls, no Playwright.

Schema: nonteam_pii_sweep_v1
"""
from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Phase 17.1 RED - extract_img_srcs + classify_img_srcs
# ---------------------------------------------------------------------------

from tests.render.nonteam_pii_sweep import (
    classify_img_srcs,
    extract_img_srcs,
)


class TestExtractImgSrcs:
    """extract_img_srcs: pull every img src from raw HTML."""

    def test_single_double_quoted_src(self):
        html = '<img src="/logo/brand.svg" alt="logo">'
        assert extract_img_srcs(html) == ["/logo/brand.svg"]

    def test_single_single_quoted_src(self):
        html = "<img src='/logo/brand.svg'>"
        assert extract_img_srcs(html) == ["/logo/brand.svg"]

    def test_multiple_imgs(self):
        html = (
            '<img src="/logo/a.png"><img src="/avatar/user.jpg" class="foo">'
        )
        result = extract_img_srcs(html)
        assert "/logo/a.png" in result
        assert "/avatar/user.jpg" in result
        assert len(result) == 2

    def test_self_closing_tag(self):
        html = '<img src="/hero/banner.jpg" />'
        assert extract_img_srcs(html) == ["/hero/banner.jpg"]

    def test_with_other_attrs_before_src(self):
        html = '<img class="headshot" loading="lazy" src="/profile/ceo.jpg" alt="CEO">'
        assert extract_img_srcs(html) == ["/profile/ceo.jpg"]

    def test_no_img_tags_returns_empty(self):
        html = "<p>No images here</p>"
        assert extract_img_srcs(html) == []

    def test_empty_html(self):
        assert extract_img_srcs("") == []

    def test_img_without_src_not_included(self):
        html = '<img alt="decorative">'
        assert extract_img_srcs(html) == []


class TestClassifyImgSrcs:
    """classify_img_srcs: classify a list of img srcs for PII risk."""

    def test_empty_list(self):
        has_suspicious, paths = classify_img_srcs([])
        assert has_suspicious is False
        assert paths == []

    def test_all_known_clean(self):
        srcs = ["/logo/brand.svg", "/wordmark/company.png", "/icon/arrow.svg"]
        has_suspicious, paths = classify_img_srcs(srcs)
        assert has_suspicious is False
        assert paths == []

    def test_anti_vacuity_pin_avatar_fires(self):
        """ANTI-VACUITY: /avatar/... MUST return (True, [...])."""
        has_suspicious, paths = classify_img_srcs(["/avatar/headshot.jpg"])
        assert has_suspicious is True
        assert "/avatar/headshot.jpg" in paths

    def test_anti_false_positive_pin_logo_clean(self):
        """ANTI-FALSE-POSITIVE: /logo/brand.svg MUST return (False, [])."""
        has_suspicious, paths = classify_img_srcs(["/logo/brand.svg"])
        assert has_suspicious is False
        assert paths == []

    def test_suspicious_author_pattern(self):
        has_suspicious, paths = classify_img_srcs(["/author/jane-doe.jpg"])
        assert has_suspicious is True
        assert "/author/jane-doe.jpg" in paths

    def test_suspicious_profile_pattern(self):
        has_suspicious, paths = classify_img_srcs(["/profile/team-member.png"])
        assert has_suspicious is True
        assert "/profile/team-member.png" in paths

    def test_suspicious_gravatar_cdn(self):
        has_suspicious, paths = classify_img_srcs(
            ["https://gravatar.com/avatar/abc123"]
        )
        assert has_suspicious is True

    def test_suspicious_github_users(self):
        has_suspicious, paths = classify_img_srcs(
            ["https://github.com/users/jsmith/avatar"]
        )
        assert has_suspicious is True

    def test_suspicious_twitter_profile(self):
        has_suspicious, paths = classify_img_srcs(
            ["https://pbs.twimg.com/profile_images/1234/photo.jpg"]
        )
        assert has_suspicious is True

    def test_mixed_clean_and_suspicious(self):
        srcs = ["/logo/brand.svg", "/user/photo.jpg"]
        has_suspicious, paths = classify_img_srcs(srcs)
        assert has_suspicious is True
        assert "/user/photo.jpg" in paths
        assert "/logo/brand.svg" not in paths

    def test_unknown_path_not_matching_either_list(self):
        """A path matching neither list passes through as clean (not suspicious)."""
        srcs = ["/assets/diagram.png"]
        has_suspicious, paths = classify_img_srcs(srcs)
        assert has_suspicious is False


# ---------------------------------------------------------------------------
# Phase 17.2 RED - assess_brand_category_html verdict mapping
# ---------------------------------------------------------------------------

from tests.render.nonteam_pii_sweep import assess_brand_category_html, NonTeamPIIVerdict


class TestAssessBrandCategoryHtml:
    """assess_brand_category_html: map HTML + HTTP status to a NonTeamPIIFinding."""

    def test_http_404_yields_na(self):
        finding = assess_brand_category_html("stripe", "testimonials", "", 404)
        assert finding.verdict == NonTeamPIIVerdict.NA.value
        assert finding.suspicious_paths == []
        assert finding.live_status == 404

    def test_200_no_imgs_yields_na(self):
        html = "<section><p>Great product!</p></section>"
        finding = assess_brand_category_html("stripe", "testimonials", html, 200)
        assert finding.verdict == NonTeamPIIVerdict.NA.value

    def test_200_all_clean_imgs_yields_clean(self):
        html = '<img src="/logo/brand.svg"><img src="/product/screenshot.png">'
        finding = assess_brand_category_html("stripe", "testimonials", html, 200)
        assert finding.verdict == NonTeamPIIVerdict.CLEAN.value
        assert finding.suspicious_paths == []

    def test_200_suspicious_img_yields_unverified(self):
        html = (
            '<img src="/logo/brand.svg">'
            '<img src="/avatar/customer.jpg">'
        )
        finding = assess_brand_category_html("stripe", "testimonials", html, 200)
        assert finding.verdict == NonTeamPIIVerdict.UNVERIFIED.value
        assert "/avatar/customer.jpg" in finding.suspicious_paths

    def test_200_mix_clean_and_suspicious_yields_unverified(self):
        html = (
            '<img src="/icon/arrow.svg">'
            '<img src="/author/blog-author.jpg">'
        )
        finding = assess_brand_category_html("apple", "article-layout", html, 200)
        assert finding.verdict == NonTeamPIIVerdict.UNVERIFIED.value
        assert "/author/blog-author.jpg" in finding.suspicious_paths

    def test_fetch_error_yields_unverified(self):
        finding = assess_brand_category_html(
            "stripe", "news-list", None, None, error="connection timeout"
        )
        assert finding.verdict == NonTeamPIIVerdict.UNVERIFIED.value
        assert finding.error == "connection timeout"

    def test_finding_carries_brand_and_category_slug(self):
        html = '<img src="/logo/brand.svg">'
        finding = assess_brand_category_html("vercel", "article-layout", html, 200)
        assert finding.brand_slug == "vercel"
        assert finding.category_slug == "article-layout"

    def test_unverified_finding_lists_all_suspicious_paths(self):
        html = (
            '<img src="/avatar/user1.jpg">'
            '<img src="/headshot/exec.png">'
        )
        finding = assess_brand_category_html("linear", "testimonials", html, 200)
        assert finding.verdict == NonTeamPIIVerdict.UNVERIFIED.value
        assert len(finding.suspicious_paths) == 2


# ---------------------------------------------------------------------------
# Phase 17.3 RED - run_nonteam_pii_sweep aggregate + go
# ---------------------------------------------------------------------------

from tests.render.nonteam_pii_sweep import run_nonteam_pii_sweep


def _make_clean_fetcher(html: str = '<img src="/logo/brand.svg">'):
    """Return a fetch_html that always returns clean HTML with HTTP 200."""
    def fetch(url: str):
        return html, 200, None
    return fetch


def _make_na_fetcher():
    """Return a fetch_html that always returns HTTP 404."""
    def fetch(url: str):
        return "", 404, None
    return fetch


def _make_error_fetcher(error: str = "connection timeout"):
    """Return a fetch_html that always returns a fetch error."""
    def fetch(url: str):
        return None, None, error
    return fetch


def _make_suspicious_fetcher():
    """Return a fetch_html that always returns HTML with a suspicious img."""
    def fetch(url: str):
        return '<img src="/avatar/person.jpg">', 200, None
    return fetch


class TestRunNonteamPiiSweep:
    """run_nonteam_pii_sweep: orchestrator aggregates findings into NonTeamPIIReport."""

    def test_all_clean_corpus_go_true(self):
        report = run_nonteam_pii_sweep(
            prod_slugs=["stripe", "apple"],
            categories=["testimonials"],
            resemblio_base="https://resemblio.com",
            fetch_html=_make_clean_fetcher(),
        )
        assert report.go is True
        assert report.unverified_count == 0
        assert report.total_pairs == 2

    def test_all_na_corpus_go_true(self):
        report = run_nonteam_pii_sweep(
            prod_slugs=["stripe"],
            categories=["testimonials", "article-layout"],
            resemblio_base="https://resemblio.com",
            fetch_html=_make_na_fetcher(),
        )
        assert report.go is True
        assert report.na_count == 2
        assert report.unverified_count == 0

    def test_one_unverified_pair_go_false(self):
        report = run_nonteam_pii_sweep(
            prod_slugs=["stripe"],
            categories=["testimonials"],
            resemblio_base="https://resemblio.com",
            fetch_html=_make_suspicious_fetcher(),
        )
        assert report.go is False
        assert report.unverified_count == 1

    def test_pairs_swept_equals_slugs_times_categories(self):
        report = run_nonteam_pii_sweep(
            prod_slugs=["stripe", "apple", "vercel"],
            categories=["testimonials", "article-layout"],
            resemblio_base="https://resemblio.com",
            fetch_html=_make_clean_fetcher(),
        )
        assert report.pairs_swept == 6  # 3 brands * 2 categories
        assert report.total_pairs == 6

    def test_counts_sum_to_pairs_swept(self):
        def mixed_fetcher(url: str):
            if "testimonials" in url:
                return '<img src="/avatar/person.jpg">', 200, None
            return '<img src="/logo/brand.svg">', 200, None

        report = run_nonteam_pii_sweep(
            prod_slugs=["stripe", "apple"],
            categories=["testimonials", "article-layout"],
            resemblio_base="https://resemblio.com",
            fetch_html=mixed_fetcher,
        )
        total = report.unverified_count + report.na_count + report.clean_count
        assert total == report.pairs_swept

    def test_error_fetcher_yields_unverified_and_no_go(self):
        report = run_nonteam_pii_sweep(
            prod_slugs=["stripe"],
            categories=["testimonials"],
            resemblio_base="https://resemblio.com",
            fetch_html=_make_error_fetcher(),
        )
        assert report.go is False
        assert report.unverified_count == 1

    def test_report_has_schema_version(self):
        report = run_nonteam_pii_sweep(
            prod_slugs=["stripe"],
            categories=["testimonials"],
            resemblio_base="https://resemblio.com",
            fetch_html=_make_clean_fetcher(),
        )
        assert report.schema_version == "nonteam_pii_sweep_v1"

    def test_categories_swept_field(self):
        categories = ["testimonials", "article-layout"]
        report = run_nonteam_pii_sweep(
            prod_slugs=["stripe"],
            categories=categories,
            resemblio_base="https://resemblio.com",
            fetch_html=_make_clean_fetcher(),
        )
        assert report.categories_swept == categories


# ---------------------------------------------------------------------------
# Phase 17.4 RED - render_nonteam_pii_markdown
# ---------------------------------------------------------------------------

from tests.render.nonteam_pii_sweep import render_nonteam_pii_markdown


def _build_go_report():
    return run_nonteam_pii_sweep(
        prod_slugs=["stripe", "apple"],
        categories=["testimonials", "article-layout"],
        resemblio_base="https://resemblio.com",
        fetch_html=_make_clean_fetcher(),
    )


def _build_no_go_report():
    def fetcher(url: str):
        if "stripe" in url and "testimonials" in url:
            return '<img src="/avatar/customer.jpg">', 200, None
        return '<img src="/logo/brand.svg">', 200, None

    return run_nonteam_pii_sweep(
        prod_slugs=["stripe", "apple"],
        categories=["testimonials", "article-layout"],
        resemblio_base="https://resemblio.com",
        fetch_html=fetcher,
    )


class TestRenderNonteamPiiMarkdown:
    """render_nonteam_pii_markdown: produces GO/NO-GO Markdown report."""

    def test_go_report_has_go_headline(self):
        md = render_nonteam_pii_markdown(_build_go_report())
        assert "GO" in md
        assert "NO-GO" not in md

    def test_no_go_report_has_no_go_headline(self):
        md = render_nonteam_pii_markdown(_build_no_go_report())
        assert "NO-GO" in md

    def test_go_report_has_clean_count(self):
        report = _build_go_report()
        md = render_nonteam_pii_markdown(report)
        assert str(report.clean_count) in md

    def test_go_report_has_na_count(self):
        report = run_nonteam_pii_sweep(
            prod_slugs=["stripe"],
            categories=["testimonials"],
            resemblio_base="https://resemblio.com",
            fetch_html=_make_na_fetcher(),
        )
        md = render_nonteam_pii_markdown(report)
        assert "1" in md  # na_count=1

    def test_no_go_report_lists_unverified_pairs(self):
        report = _build_no_go_report()
        md = render_nonteam_pii_markdown(report)
        assert "stripe" in md
        assert "testimonials" in md
        assert "/avatar/customer.jpg" in md

    def test_scope_note_lists_swept_categories(self):
        md = render_nonteam_pii_markdown(_build_go_report())
        assert "testimonials" in md
        assert "article-layout" in md

    def test_scope_note_names_unswept_categories(self):
        md = render_nonteam_pii_markdown(_build_go_report())
        # The report must explicitly state which categories were NOT swept.
        assert "buttons" in md or "hero" in md or "footer" in md

    def test_playwright_follow_up_note_present(self):
        md = render_nonteam_pii_markdown(_build_go_report())
        # Must note that UNVERIFIED requires Playwright/human confirmation.
        assert "Playwright" in md or "human" in md or "follow-up" in md

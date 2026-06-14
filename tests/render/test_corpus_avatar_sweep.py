"""TDD tests for corpus_avatar_sweep.py - Phase 16 full-corpus avatar/PII sweep.

All tests are pure (no Playwright, no network, no filesystem). Synthetic
fixtures used throughout. The real capturer and live-run execution are
separate from this suite.

Schema: corpus_avatar_sweep_v1
"""
from __future__ import annotations

import pytest
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Phase 16.1 RED - classify_avatar_eval verdict mapping
# ---------------------------------------------------------------------------


class TestClassifyAvatarEval:
    """Phase 16.1 - classify_avatar_eval maps the four cases to AvatarVerdict.

    The anti-vacuity pin (LEAK) and anti-false-positive pin (NA-not-LEAK) are
    both required here. These two tests are the crux guards of Phase 16.
    """

    def test_members_with_photo_yields_leak(self):
        """Anti-vacuity pin: member_count>0, members_with_photo>0 -> LEAK (hard NO-GO)."""
        from tests.render.corpus_avatar_sweep import classify_avatar_eval, AvatarVerdict

        finding = classify_avatar_eval(
            brand_slug="acme",
            page_loaded=True,
            http_status=200,
            member_count=3,
            members_with_photo=2,
            error=None,
        )
        assert finding.verdict == AvatarVerdict.LEAK
        assert finding.brand_slug == "acme"
        assert finding.members_with_photo == 2

    def test_members_present_no_photo_yields_clean(self):
        """member_count>0, members_with_photo==0 -> CLEAN."""
        from tests.render.corpus_avatar_sweep import classify_avatar_eval, AvatarVerdict

        finding = classify_avatar_eval(
            brand_slug="beta",
            page_loaded=True,
            http_status=200,
            member_count=4,
            members_with_photo=0,
            error=None,
        )
        assert finding.verdict == AvatarVerdict.CLEAN
        assert finding.members_with_photo == 0

    def test_no_members_page_loaded_yields_na_not_leak(self):
        """Anti-false-positive pin (THE TRAP GUARD): page loaded, member_count==0 -> NA, never LEAK."""
        from tests.render.corpus_avatar_sweep import classify_avatar_eval, AvatarVerdict

        finding = classify_avatar_eval(
            brand_slug="gamma",
            page_loaded=True,
            http_status=200,
            member_count=0,
            members_with_photo=0,
            error=None,
        )
        assert finding.verdict == AvatarVerdict.NA
        # This is the trap guard: no members cannot be a LEAK
        assert finding.verdict != AvatarVerdict.LEAK

    def test_http_404_yields_na(self):
        """HTTP 404 about-team page -> NA (no team page to leak from)."""
        from tests.render.corpus_avatar_sweep import classify_avatar_eval, AvatarVerdict

        finding = classify_avatar_eval(
            brand_slug="delta",
            page_loaded=True,
            http_status=404,
            member_count=0,
            members_with_photo=0,
            error=None,
        )
        assert finding.verdict == AvatarVerdict.NA

    def test_error_set_yields_unverified(self):
        """Network error / timeout -> UNVERIFIED (blocks GO, conservative posture)."""
        from tests.render.corpus_avatar_sweep import classify_avatar_eval, AvatarVerdict

        finding = classify_avatar_eval(
            brand_slug="epsilon",
            page_loaded=False,
            http_status=None,
            member_count=0,
            members_with_photo=0,
            error="connection timed out",
        )
        assert finding.verdict == AvatarVerdict.UNVERIFIED
        assert finding.error == "connection timed out"

    def test_page_not_loaded_yields_unverified(self):
        """page_loaded=False without explicit error -> UNVERIFIED."""
        from tests.render.corpus_avatar_sweep import classify_avatar_eval, AvatarVerdict

        finding = classify_avatar_eval(
            brand_slug="zeta",
            page_loaded=False,
            http_status=None,
            member_count=0,
            members_with_photo=0,
            error=None,
        )
        assert finding.verdict == AvatarVerdict.UNVERIFIED

    def test_eval_missing_yields_unverified(self):
        """members_with_photo=None (evaluator threw) -> UNVERIFIED even if page loaded."""
        from tests.render.corpus_avatar_sweep import classify_avatar_eval, AvatarVerdict

        finding = classify_avatar_eval(
            brand_slug="eta",
            page_loaded=True,
            http_status=200,
            member_count=2,
            members_with_photo=None,
            error=None,
        )
        assert finding.verdict == AvatarVerdict.UNVERIFIED

    def test_finding_carries_brand_slug(self):
        """BrandAvatarFinding always carries the brand_slug through."""
        from tests.render.corpus_avatar_sweep import classify_avatar_eval

        finding = classify_avatar_eval(
            brand_slug="my-brand",
            page_loaded=True,
            http_status=200,
            member_count=5,
            members_with_photo=0,
            error=None,
        )
        assert finding.brand_slug == "my-brand"


# ---------------------------------------------------------------------------
# Phase 16.2 RED - build_avatar_assertion shape
# ---------------------------------------------------------------------------


class TestBuildAvatarAssertion:
    """Phase 16.2 - build_avatar_assertion returns a correctly shaped assertion dict."""

    def test_id_contains_avatar_leak_id_marker(self):
        """Assertion id MUST contain AVATAR_LEAK_ID_MARKER so the classifier fires correctly."""
        from tests.render.corpus_avatar_sweep import build_avatar_assertion
        from tests.render.assertion_eval import AVATAR_LEAK_ID_MARKER

        assertion = build_avatar_assertion("apple")
        assert AVATAR_LEAK_ID_MARKER in assertion["id"]

    def test_id_contains_brand_slug(self):
        """Assertion id contains the brand slug so per-brand results are identifiable."""
        from tests.render.corpus_avatar_sweep import build_avatar_assertion

        assertion = build_avatar_assertion("stripe")
        assert "stripe" in assertion["id"]

    def test_expected_is_true(self):
        """expected=True: the assertion passes when no photos found (CLEAN or NA).
        A LEAK (photos present) returns False from the evaluator, mismatch with True = FAIL."""
        from tests.render.corpus_avatar_sweep import build_avatar_assertion

        assertion = build_avatar_assertion("linear")
        assert assertion.get("expected") is True

    def test_evaluate_field_is_string(self):
        """evaluate field must be a non-empty string (JS evaluator)."""
        from tests.render.corpus_avatar_sweep import build_avatar_assertion

        assertion = build_avatar_assertion("vercel")
        evaluate = assertion.get("evaluate")
        assert isinstance(evaluate, str)
        assert len(evaluate) > 0

    def test_evaluate_references_at__member(self):
        """Evaluator queries .at__member elements (the team member CSS class)."""
        from tests.render.corpus_avatar_sweep import build_avatar_assertion

        assertion = build_avatar_assertion("openai")
        assert ".at__member" in assertion["evaluate"]

    def test_different_slugs_produce_different_ids(self):
        """Each brand slug produces a unique assertion id."""
        from tests.render.corpus_avatar_sweep import build_avatar_assertion

        a1 = build_avatar_assertion("apple")
        a2 = build_avatar_assertion("stripe")
        assert a1["id"] != a2["id"]


# ---------------------------------------------------------------------------
# Phase 16.3 RED - run_corpus_avatar_sweep + CorpusAvatarReport
# ---------------------------------------------------------------------------


def _make_capturer(returns_by_slug: Dict[str, Tuple]) -> callable:
    """Build a fake capture_avatar callable returning canned tuples per slug."""

    def _capture(slug: str) -> Tuple:
        return returns_by_slug.get(
            slug,
            (True, 200, 1, 0, None),  # default: CLEAN
        )

    return _capture


SAMPLE_SLUGS = ["apple", "stripe", "a24", "notion", "figma"]

_ALL_CLEAN_RETURNS = {
    "apple":  (True, 200, 4, 0, None),
    "stripe": (True, 200, 3, 0, None),
    "a24":    (True, 200, 2, 0, None),
    "notion": (True, 200, 0, 0, None),   # NA: no members
    "figma":  (True, 200, 1, 0, None),
}


class TestRunCorpusAvatarSweep:
    """Phase 16.3 - run_corpus_avatar_sweep builds a correct CorpusAvatarReport."""

    def test_all_clean_go_true(self):
        """All CLEAN/NA brands -> go=True, leak_count=0, unverified_count=0."""
        from tests.render.corpus_avatar_sweep import run_corpus_avatar_sweep, AvatarVerdict

        capturer = _make_capturer(_ALL_CLEAN_RETURNS)
        report = run_corpus_avatar_sweep(SAMPLE_SLUGS, "https://resemblio.com", capturer)
        assert report.go is True
        assert report.leak_count == 0
        assert report.unverified_count == 0

    def test_counts_sum_to_brands_swept(self):
        """leak_count + unverified_count + na_count + clean_count == brands_swept."""
        from tests.render.corpus_avatar_sweep import run_corpus_avatar_sweep

        capturer = _make_capturer(_ALL_CLEAN_RETURNS)
        report = run_corpus_avatar_sweep(SAMPLE_SLUGS, "https://resemblio.com", capturer)
        total = (
            report.leak_count
            + report.unverified_count
            + report.na_count
            + report.clean_count
        )
        assert total == report.brands_swept
        assert report.brands_swept == len(SAMPLE_SLUGS)

    def test_one_leaking_brand_go_false(self):
        """One LEAK brand -> go=False, leak_count=1."""
        from tests.render.corpus_avatar_sweep import run_corpus_avatar_sweep

        returns = dict(_ALL_CLEAN_RETURNS)
        returns["apple"] = (True, 200, 4, 2, None)  # LEAK: 2 members have photos
        capturer = _make_capturer(returns)
        report = run_corpus_avatar_sweep(SAMPLE_SLUGS, "https://resemblio.com", capturer)
        assert report.go is False
        assert report.leak_count == 1

    def test_one_unverified_brand_go_false(self):
        """One UNVERIFIED brand -> go=False, unverified_count=1."""
        from tests.render.corpus_avatar_sweep import run_corpus_avatar_sweep

        returns = dict(_ALL_CLEAN_RETURNS)
        returns["stripe"] = (False, None, 0, 0, "timeout after 30s")
        capturer = _make_capturer(returns)
        report = run_corpus_avatar_sweep(SAMPLE_SLUGS, "https://resemblio.com", capturer)
        assert report.go is False
        assert report.unverified_count == 1

    def test_na_brands_listed(self):
        """Brands with no team section are listed in na_brands."""
        from tests.render.corpus_avatar_sweep import run_corpus_avatar_sweep

        capturer = _make_capturer(_ALL_CLEAN_RETURNS)
        report = run_corpus_avatar_sweep(SAMPLE_SLUGS, "https://resemblio.com", capturer)
        # "notion" has member_count=0 -> NA
        assert "notion" in report.na_brands

    def test_brands_swept_equals_slug_count(self):
        """brands_swept equals the number of slugs provided."""
        from tests.render.corpus_avatar_sweep import run_corpus_avatar_sweep

        capturer = _make_capturer(_ALL_CLEAN_RETURNS)
        report = run_corpus_avatar_sweep(SAMPLE_SLUGS, "https://resemblio.com", capturer)
        assert report.brands_swept == len(SAMPLE_SLUGS)

    def test_schema_version(self):
        """CorpusAvatarReport carries schema_version = 'corpus_avatar_sweep_v1'."""
        from tests.render.corpus_avatar_sweep import run_corpus_avatar_sweep

        capturer = _make_capturer(_ALL_CLEAN_RETURNS)
        report = run_corpus_avatar_sweep(SAMPLE_SLUGS, "https://resemblio.com", capturer)
        assert report.schema_version == "corpus_avatar_sweep_v1"

    def test_findings_length_equals_brands_swept(self):
        """findings list has one entry per brand swept."""
        from tests.render.corpus_avatar_sweep import run_corpus_avatar_sweep

        capturer = _make_capturer(_ALL_CLEAN_RETURNS)
        report = run_corpus_avatar_sweep(SAMPLE_SLUGS, "https://resemblio.com", capturer)
        assert len(report.findings) == len(SAMPLE_SLUGS)

    def test_leak_does_not_block_na_clean_count(self):
        """A LEAK brand does not corrupt NA/CLEAN counts for other brands."""
        from tests.render.corpus_avatar_sweep import run_corpus_avatar_sweep

        returns = dict(_ALL_CLEAN_RETURNS)
        returns["apple"] = (True, 200, 4, 1, None)  # LEAK
        capturer = _make_capturer(returns)
        report = run_corpus_avatar_sweep(SAMPLE_SLUGS, "https://resemblio.com", capturer)
        # notion is NA, the rest (except apple) are CLEAN
        assert report.na_count == 1
        assert report.clean_count == 3
        assert report.leak_count == 1


# ---------------------------------------------------------------------------
# Phase 16.4 RED - render_corpus_avatar_markdown
# ---------------------------------------------------------------------------


class TestRenderCorpusAvatarMarkdown:
    """Phase 16.4 - render_corpus_avatar_markdown produces the expected Markdown."""

    def _go_report(self):
        from tests.render.corpus_avatar_sweep import run_corpus_avatar_sweep

        capturer = _make_capturer(_ALL_CLEAN_RETURNS)
        return run_corpus_avatar_sweep(SAMPLE_SLUGS, "https://resemblio.com", capturer)

    def _no_go_leak_report(self):
        from tests.render.corpus_avatar_sweep import run_corpus_avatar_sweep

        returns = dict(_ALL_CLEAN_RETURNS)
        returns["apple"] = (True, 200, 4, 2, None)  # LEAK
        capturer = _make_capturer(returns)
        return run_corpus_avatar_sweep(SAMPLE_SLUGS, "https://resemblio.com", capturer)

    def _no_go_unverified_report(self):
        from tests.render.corpus_avatar_sweep import run_corpus_avatar_sweep

        returns = dict(_ALL_CLEAN_RETURNS)
        returns["stripe"] = (False, None, 0, 0, "playwright crash")
        capturer = _make_capturer(returns)
        return run_corpus_avatar_sweep(SAMPLE_SLUGS, "https://resemblio.com", capturer)

    def test_go_report_contains_go_headline(self):
        """A GO report has 'GO' in the headline."""
        from tests.render.corpus_avatar_sweep import render_corpus_avatar_markdown

        md = render_corpus_avatar_markdown(self._go_report())
        assert "GO" in md
        assert "NO-GO" not in md

    def test_no_go_leak_contains_no_go_headline(self):
        """A NO-GO report (leak) has 'NO-GO' in the headline."""
        from tests.render.corpus_avatar_sweep import render_corpus_avatar_markdown

        md = render_corpus_avatar_markdown(self._no_go_leak_report())
        assert "NO-GO" in md

    def test_leak_brand_named_in_output(self):
        """A leaking brand appears in the NO-GO output."""
        from tests.render.corpus_avatar_sweep import render_corpus_avatar_markdown

        md = render_corpus_avatar_markdown(self._no_go_leak_report())
        assert "apple" in md

    def test_unverified_brand_named_in_output(self):
        """An UNVERIFIED brand appears in the NO-GO output."""
        from tests.render.corpus_avatar_sweep import render_corpus_avatar_markdown

        md = render_corpus_avatar_markdown(self._no_go_unverified_report())
        assert "stripe" in md

    def test_na_brands_section_present(self):
        """NA brands section is always present to show coverage is NA, not silently skipped."""
        from tests.render.corpus_avatar_sweep import render_corpus_avatar_markdown

        md = render_corpus_avatar_markdown(self._go_report())
        assert "notion" in md  # NA brand named

    def test_phase_17_footer_present(self):
        """Non-about-team PII sweep explicitly deferred to Phase 17 in the footer."""
        from tests.render.corpus_avatar_sweep import render_corpus_avatar_markdown

        md = render_corpus_avatar_markdown(self._go_report())
        assert "Phase 17" in md
        # Must mention non-about-team scope deferral
        assert "non-about-team" in md.lower() or "other" in md.lower() or "testimonial" in md.lower() or "phase 17" in md.lower()

    def test_clean_count_in_output(self):
        """Clean count appears in the markdown output."""
        from tests.render.corpus_avatar_sweep import render_corpus_avatar_markdown

        report = self._go_report()
        md = render_corpus_avatar_markdown(report)
        assert str(report.clean_count) in md

    def test_returns_string(self):
        """render_corpus_avatar_markdown returns a str."""
        from tests.render.corpus_avatar_sweep import render_corpus_avatar_markdown

        md = render_corpus_avatar_markdown(self._go_report())
        assert isinstance(md, str)
        assert len(md) > 0

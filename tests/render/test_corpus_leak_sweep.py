"""TDD tests for corpus_leak_sweep.py - Phase 15 full-corpus trademark leak sweep.

All tests are pure (no network, no filesystem). Synthetic targets and HTML fixtures
used throughout. The real fetcher and live-run execution are separate from this suite.

Schema: corpus_leak_sweep_v1
"""
from __future__ import annotations

import pytest
from typing import Dict, List, Optional, Tuple

from tests.render.assertion_eval import NO_LEAK_ID_MARKER


# ---------------------------------------------------------------------------
# Synthetic fixtures shared across sub-phases
# ---------------------------------------------------------------------------

UNIVERSAL_TOKENS = ["/logo.svg", "/wordmark.svg", "class=\"brand-logo\""]

SAMPLE_TARGETS = {
    "schema_version": "trademark_strip_targets_v1",
    "universal_forbidden_substrings": UNIVERSAL_TOKENS,
    "brands": [
        {
            "slug": "apple",
            "pretty": "Apple",
            "forbidden_image_substrings": ["apple.com/logo", "apple-logo"],
        },
        {
            "slug": "stripe",
            "pretty": "Stripe",
            "forbidden_image_substrings": ["stripe-logo", "stripe-wordmark"],
        },
    ],
}

PROD_SLUGS = ["apple", "stripe", "a24", "notion", "figma"]


# ---------------------------------------------------------------------------
# Phase 15.1 RED: forbidden_for_brand + build_no_leak_assertion
# ---------------------------------------------------------------------------


class TestForbiddenForBrand:
    """Phase 15.1 - forbidden_for_brand returns correct token list + coverage flag."""

    def test_covered_brand_merges_universal_and_per_brand(self):
        from tests.render.corpus_leak_sweep import forbidden_for_brand

        tokens, had_per_brand = forbidden_for_brand("apple", SAMPLE_TARGETS)
        assert had_per_brand is True
        # Must include universal tokens
        for u in UNIVERSAL_TOKENS:
            assert u in tokens, f"missing universal token: {u}"
        # Must include per-brand tokens
        assert "apple.com/logo" in tokens
        assert "apple-logo" in tokens

    def test_uncovered_brand_returns_only_universal(self):
        from tests.render.corpus_leak_sweep import forbidden_for_brand

        tokens, had_per_brand = forbidden_for_brand("notion", SAMPLE_TARGETS)
        assert had_per_brand is False
        assert tokens == UNIVERSAL_TOKENS

    def test_total_token_count_for_covered_brand(self):
        from tests.render.corpus_leak_sweep import forbidden_for_brand

        tokens, _ = forbidden_for_brand("apple", SAMPLE_TARGETS)
        assert len(tokens) == len(UNIVERSAL_TOKENS) + 2  # 3 universal + 2 per-brand

    def test_second_covered_brand(self):
        from tests.render.corpus_leak_sweep import forbidden_for_brand

        tokens, had_per_brand = forbidden_for_brand("stripe", SAMPLE_TARGETS)
        assert had_per_brand is True
        assert "stripe-logo" in tokens
        assert "stripe-wordmark" in tokens


class TestBuildNoLeakAssertion:
    """Phase 15.1 - build_no_leak_assertion produces the exact evaluator shape."""

    def test_id_contains_no_leak_id_marker(self):
        from tests.render.corpus_leak_sweep import build_no_leak_assertion

        assertion = build_no_leak_assertion("apple", ["/logo.svg", "apple-logo"])
        assert NO_LEAK_ID_MARKER in assertion["id"]

    def test_id_contains_brand_slug(self):
        from tests.render.corpus_leak_sweep import build_no_leak_assertion

        assertion = build_no_leak_assertion("apple", ["/logo.svg"])
        assert "apple" in assertion["id"]

    def test_expected_is_true(self):
        from tests.render.corpus_leak_sweep import build_no_leak_assertion

        assertion = build_no_leak_assertion("apple", ["/logo.svg"])
        assert assertion.get("expected") is True

    def test_evaluator_contains_forbidden_every(self):
        from tests.render.corpus_leak_sweep import build_no_leak_assertion

        assertion = build_no_leak_assertion("stripe", ["stripe-logo", "stripe-wordmark"])
        evaluate = assertion.get("evaluate", "")
        assert "forbidden.every" in evaluate

    def test_evaluator_contains_all_tokens(self):
        from tests.render.corpus_leak_sweep import build_no_leak_assertion
        from tests.render.assertion_eval import forbidden_tokens_from_evaluator

        tokens_in = ["stripe-logo", "stripe-wordmark"]
        assertion = build_no_leak_assertion("stripe", tokens_in)
        parsed = forbidden_tokens_from_evaluator(assertion["evaluate"])
        assert set(parsed) == set(tokens_in)

    def test_evaluator_fires_correctly_on_clean_html(self):
        from tests.render.corpus_leak_sweep import build_no_leak_assertion
        from tests.render.assertion_eval import evaluate_all_assertions_against_live_html

        assertion = build_no_leak_assertion("apple", ["apple-logo"])
        result = evaluate_all_assertions_against_live_html(
            [assertion], "<div>Apple brand stripped</div>"
        )
        assert result.wordmark_leak is False

    def test_evaluator_fires_correctly_on_leaking_html(self):
        from tests.render.corpus_leak_sweep import build_no_leak_assertion
        from tests.render.assertion_eval import evaluate_all_assertions_against_live_html

        assertion = build_no_leak_assertion("apple", ["apple-logo"])
        result = evaluate_all_assertions_against_live_html(
            [assertion], '<img src="/assets/apple-logo.svg">'
        )
        assert result.wordmark_leak is True


# ---------------------------------------------------------------------------
# Phase 15.2 RED: assess_brand_html (pure core + anti-vacuity)
# ---------------------------------------------------------------------------


class TestAssessBrandHtml:
    """Phase 15.2 - assess_brand_html maps HTML against trademark targets."""

    def test_clean_html_returns_not_leaked(self):
        from tests.render.corpus_leak_sweep import assess_brand_html

        finding = assess_brand_html("apple", "<div>Apple brand page</div>", SAMPLE_TARGETS)
        assert finding.leaked is False
        assert finding.leaked_tokens == []

    def test_anti_vacuity_known_token_in_html_returns_leaked_true(self):
        """Anti-vacuity pin: a brand with a known forbidden token MUST return leaked=True.

        This is the regression guard for the entire phase. If a misshaped assertion
        silently never fires, this test catches it.
        """
        from tests.render.corpus_leak_sweep import assess_brand_html

        finding = assess_brand_html(
            "apple",
            '<img src="https://cdn.apple.com/apple-logo.svg">',
            SAMPLE_TARGETS,
        )
        assert finding.leaked is True

    def test_leaked_tokens_names_the_matching_substring(self):
        from tests.render.corpus_leak_sweep import assess_brand_html

        finding = assess_brand_html(
            "apple",
            "<div>visit apple.com/logo for more</div>",
            SAMPLE_TARGETS,
        )
        assert finding.leaked is True
        assert "apple.com/logo" in finding.leaked_tokens

    def test_uncovered_brand_with_universal_token_leaks(self):
        from tests.render.corpus_leak_sweep import assess_brand_html

        finding = assess_brand_html(
            "notion",
            '<img src="/logo.svg">',
            SAMPLE_TARGETS,
        )
        assert finding.leaked is True

    def test_uncovered_brand_clean_returns_not_leaked(self):
        from tests.render.corpus_leak_sweep import assess_brand_html

        finding = assess_brand_html(
            "notion",
            "<div>Notion brand page - typography and color only</div>",
            SAMPLE_TARGETS,
        )
        assert finding.leaked is False

    def test_had_per_brand_rules_true_for_covered_brand(self):
        from tests.render.corpus_leak_sweep import assess_brand_html

        finding = assess_brand_html("apple", "<div>clean</div>", SAMPLE_TARGETS)
        assert finding.had_per_brand_rules is True

    def test_had_per_brand_rules_false_for_uncovered_brand(self):
        from tests.render.corpus_leak_sweep import assess_brand_html

        finding = assess_brand_html("notion", "<div>clean</div>", SAMPLE_TARGETS)
        assert finding.had_per_brand_rules is False

    def test_brand_slug_preserved(self):
        from tests.render.corpus_leak_sweep import assess_brand_html

        finding = assess_brand_html("apple", "<div>clean</div>", SAMPLE_TARGETS)
        assert finding.brand_slug == "apple"

    def test_live_status_and_error_none_when_called_directly(self):
        from tests.render.corpus_leak_sweep import assess_brand_html

        finding = assess_brand_html("apple", "<div>clean</div>", SAMPLE_TARGETS)
        assert finding.live_status is None
        assert finding.error is None

    def test_case_insensitive_check(self):
        from tests.render.corpus_leak_sweep import assess_brand_html

        finding = assess_brand_html(
            "apple",
            "<div>APPLE-LOGO embedded here</div>",
            SAMPLE_TARGETS,
        )
        assert finding.leaked is True


# ---------------------------------------------------------------------------
# Phase 15.3 RED: audit_coverage
# ---------------------------------------------------------------------------


class TestAuditCoverage:
    """Phase 15.3 - audit_coverage returns slugs lacking per-brand entries."""

    def test_returns_uncovered_slugs(self):
        from tests.render.corpus_leak_sweep import audit_coverage

        uncovered = audit_coverage(PROD_SLUGS, SAMPLE_TARGETS)
        assert set(uncovered) == {"a24", "notion", "figma"}

    def test_empty_when_all_covered(self):
        from tests.render.corpus_leak_sweep import audit_coverage

        uncovered = audit_coverage(["apple", "stripe"], SAMPLE_TARGETS)
        assert uncovered == []

    def test_all_uncovered_when_no_per_brand(self):
        from tests.render.corpus_leak_sweep import audit_coverage

        targets_no_brands = {
            "schema_version": "trademark_strip_targets_v1",
            "universal_forbidden_substrings": ["/logo.svg"],
            "brands": [],
        }
        uncovered = audit_coverage(["x", "y"], targets_no_brands)
        assert set(uncovered) == {"x", "y"}

    def test_returns_list(self):
        from tests.render.corpus_leak_sweep import audit_coverage

        result = audit_coverage(PROD_SLUGS, SAMPLE_TARGETS)
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Phase 15.4 RED: run_corpus_leak_sweep + CorpusLeakReport
# ---------------------------------------------------------------------------


def _make_fake_fetcher(html_map: Dict[str, str]) -> object:
    """Return a fake fetch_html callable that returns canned HTML per slug.

    Slugs not in html_map return a fetch error (simulating a network failure).
    """
    def fake_fetch(slug: str) -> Tuple[Optional[str], Optional[int], Optional[str]]:
        if slug in html_map:
            return html_map[slug], 200, None
        return None, None, f"fetch failed for {slug}"
    return fake_fetch


class TestRunCorpusLeakSweep:
    """Phase 15.4 - run_corpus_leak_sweep orchestrator + CorpusLeakReport aggregation."""

    def test_all_clean_corpus_go_true(self):
        from tests.render.corpus_leak_sweep import run_corpus_leak_sweep

        slugs = ["apple", "notion"]
        html_map = {
            "apple": "<div>Apple brand page - clean</div>",
            "notion": "<div>Notion brand page - clean</div>",
        }
        report = run_corpus_leak_sweep(
            slugs, SAMPLE_TARGETS, "https://resemblio.com", _make_fake_fetcher(html_map)
        )
        assert report.go is True
        assert report.leak_count == 0
        assert report.error_count == 0
        assert report.brands_swept == 2

    def test_leaking_brand_go_false(self):
        from tests.render.corpus_leak_sweep import run_corpus_leak_sweep

        slugs = ["apple", "notion"]
        html_map = {
            "apple": '<img src="/assets/apple-logo.svg">',
            "notion": "<div>Notion brand page - clean</div>",
        }
        report = run_corpus_leak_sweep(
            slugs, SAMPLE_TARGETS, "https://resemblio.com", _make_fake_fetcher(html_map)
        )
        assert report.go is False
        assert report.leak_count == 1
        assert report.error_count == 0
        leaking = [f for f in report.findings if f.leaked]
        assert len(leaking) == 1
        assert leaking[0].brand_slug == "apple"

    def test_fetch_error_go_false(self):
        from tests.render.corpus_leak_sweep import run_corpus_leak_sweep

        slugs = ["apple", "notion"]
        html_map = {"apple": "<div>clean</div>"}  # notion not in map -> error
        report = run_corpus_leak_sweep(
            slugs, SAMPLE_TARGETS, "https://resemblio.com", _make_fake_fetcher(html_map)
        )
        assert report.go is False
        assert report.error_count == 1
        errored = [f for f in report.findings if f.error is not None]
        assert errored[0].brand_slug == "notion"

    def test_brands_swept_equals_slugs_count(self):
        from tests.render.corpus_leak_sweep import run_corpus_leak_sweep

        html_map = {s: "<div>clean</div>" for s in PROD_SLUGS}
        report = run_corpus_leak_sweep(
            PROD_SLUGS, SAMPLE_TARGETS, "https://resemblio.com",
            _make_fake_fetcher(html_map)
        )
        assert report.brands_swept == len(PROD_SLUGS)

    def test_coverage_only_universal_lists_uncovered_brands(self):
        from tests.render.corpus_leak_sweep import run_corpus_leak_sweep

        html_map = {s: "<div>clean</div>" for s in PROD_SLUGS}
        report = run_corpus_leak_sweep(
            PROD_SLUGS, SAMPLE_TARGETS, "https://resemblio.com",
            _make_fake_fetcher(html_map)
        )
        assert set(report.coverage_only_universal) == {"a24", "notion", "figma"}

    def test_schema_version_is_corpus_leak_sweep_v1(self):
        from tests.render.corpus_leak_sweep import run_corpus_leak_sweep

        html_map = {s: "<div>clean</div>" for s in PROD_SLUGS}
        report = run_corpus_leak_sweep(
            PROD_SLUGS, SAMPLE_TARGETS, "https://resemblio.com",
            _make_fake_fetcher(html_map)
        )
        assert report.schema_version == "corpus_leak_sweep_v1"

    def test_total_brands_field(self):
        from tests.render.corpus_leak_sweep import run_corpus_leak_sweep

        html_map = {s: "<div>clean</div>" for s in PROD_SLUGS}
        report = run_corpus_leak_sweep(
            PROD_SLUGS, SAMPLE_TARGETS, "https://resemblio.com",
            _make_fake_fetcher(html_map)
        )
        assert report.total_brands == len(PROD_SLUGS)

    def test_go_false_when_both_leak_and_error(self):
        from tests.render.corpus_leak_sweep import run_corpus_leak_sweep

        slugs = ["apple", "stripe", "a24"]
        html_map = {
            "apple": '<img src="/assets/apple-logo.svg">',  # leaks
            "stripe": "<div>clean</div>",
            # a24 not in map -> error
        }
        report = run_corpus_leak_sweep(
            slugs, SAMPLE_TARGETS, "https://resemblio.com", _make_fake_fetcher(html_map)
        )
        assert report.go is False
        assert report.leak_count == 1
        assert report.error_count == 1


# ---------------------------------------------------------------------------
# Phase 15.5 RED: render_corpus_leak_markdown
# ---------------------------------------------------------------------------


class TestRenderCorpusLeakMarkdown:
    """Phase 15.5 - render_corpus_leak_markdown produces a readable GO/NO-GO report."""

    def _make_go_report(self):
        from tests.render.corpus_leak_sweep import (
            run_corpus_leak_sweep,
            BrandLeakFinding,
            CorpusLeakReport,
        )
        from datetime import datetime, timezone

        return CorpusLeakReport(
            schema_version="corpus_leak_sweep_v1",
            generated_at_utc=datetime.now(tz=timezone.utc).isoformat(),
            resemblio_base="https://resemblio.com",
            total_brands=5,
            brands_swept=5,
            leak_count=0,
            error_count=0,
            coverage_only_universal=["a24", "notion", "figma"],
            go=True,
            findings=[
                BrandLeakFinding(
                    brand_slug="apple", leaked=False, leaked_tokens=[],
                    had_per_brand_rules=True, live_status=200, error=None
                ),
                BrandLeakFinding(
                    brand_slug="a24", leaked=False, leaked_tokens=[],
                    had_per_brand_rules=False, live_status=200, error=None
                ),
            ],
        )

    def _make_nogo_report(self):
        from tests.render.corpus_leak_sweep import BrandLeakFinding, CorpusLeakReport
        from datetime import datetime, timezone

        return CorpusLeakReport(
            schema_version="corpus_leak_sweep_v1",
            generated_at_utc=datetime.now(tz=timezone.utc).isoformat(),
            resemblio_base="https://resemblio.com",
            total_brands=3,
            brands_swept=3,
            leak_count=1,
            error_count=0,
            coverage_only_universal=["a24"],
            go=False,
            findings=[
                BrandLeakFinding(
                    brand_slug="apple", leaked=True, leaked_tokens=["apple-logo"],
                    had_per_brand_rules=True, live_status=200, error=None
                ),
            ],
        )

    def test_go_report_contains_go_headline(self):
        from tests.render.corpus_leak_sweep import render_corpus_leak_markdown

        md = render_corpus_leak_markdown(self._make_go_report())
        assert "GO" in md
        assert "NO-GO" not in md

    def test_nogo_report_contains_nogo_headline(self):
        from tests.render.corpus_leak_sweep import render_corpus_leak_markdown

        md = render_corpus_leak_markdown(self._make_nogo_report())
        assert "NO-GO" in md

    def test_go_report_states_zero_leaks(self):
        from tests.render.corpus_leak_sweep import render_corpus_leak_markdown

        md = render_corpus_leak_markdown(self._make_go_report())
        assert "0 leak" in md.lower()

    def test_nogo_report_names_leaking_brand(self):
        from tests.render.corpus_leak_sweep import render_corpus_leak_markdown

        md = render_corpus_leak_markdown(self._make_nogo_report())
        assert "apple" in md

    def test_universal_only_section_present(self):
        from tests.render.corpus_leak_sweep import render_corpus_leak_markdown

        md = render_corpus_leak_markdown(self._make_go_report())
        assert "universal" in md.lower()
        assert "a24" in md

    def test_phase_16_pii_footer_present(self):
        from tests.render.corpus_leak_sweep import render_corpus_leak_markdown

        md = render_corpus_leak_markdown(self._make_go_report())
        lower = md.lower()
        assert "phase 16" in lower or "pii" in lower or "avatar" in lower

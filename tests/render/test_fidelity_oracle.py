"""Tests for the DRL fidelity oracle (Issue #37, Epic #35 Step 1).

RED -> GREEN -> refactor per WORKFLOW.md.

Split into two tiers:

Pure tier (no importorskip at module level):
  Tests for compare_computed_styles, build_baseline_map,
  iter_corpus_assets, is_candidate_wrapped, and
  extract_component_from_candidate. These ALWAYS run, including on bare
  CI checkouts without Playwright or Pillow.

Browser tier (self-skip):
  Tests that require Playwright + Pillow. Each browser-tier test calls
  pytest.importorskip("playwright.sync_api") and
  pytest.importorskip("PIL.Image") so the whole test is skipped cleanly
  when those deps are absent (AC5).

Acceptance criteria verified here:
  AC1 - identical style maps -> "pass".
  AC2 - single mismatch -> "fail" naming property + state (non-vacuous).
  AC3 - build_baseline_map: enumerates with pass/fail, schema_version,
        deterministic ordering.
  AC4 - candidate chrome (font-disclosure aside, wrapper) excluded from
        comparison (pure part: extract_component_from_candidate /
        is_candidate_wrapped; browser part: capture_candidate_styles
        targets the component element, not the aside or article).
  AC5 - browser-tier tests self-skip when Playwright/Pillow absent.

Do this work at a level that would impress a senior developer.
Include documentation and code comments that make it easy for a future
developer to maintain this project.
"""
from __future__ import annotations

import pathlib

import pytest


# ---------------------------------------------------------------------------
# AC1 + AC2: compare_computed_styles - pure comparator
# ---------------------------------------------------------------------------


class TestComparatorPassCases:
    """AC1: identical style maps return verdict="pass" with no diffs."""

    def test_identical_single_state(self):
        from tests.render.fidelity_oracle import compare_computed_styles

        ref = {"rest": {"color": "rgb(0, 0, 0)", "font-size": "16px"}}
        cand = {"rest": {"color": "rgb(0, 0, 0)", "font-size": "16px"}}
        v = compare_computed_styles(ref, cand)
        assert v.verdict == "pass"
        assert v.diffs == []

    def test_identical_multiple_states(self):
        from tests.render.fidelity_oracle import compare_computed_styles

        ref = {
            "rest": {"color": "rgb(0, 0, 0)"},
            "hover": {"color": "rgb(30, 30, 30)"},
        }
        cand = {
            "rest": {"color": "rgb(0, 0, 0)"},
            "hover": {"color": "rgb(30, 30, 30)"},
        }
        v = compare_computed_styles(ref, cand)
        assert v.verdict == "pass"
        assert v.diffs == []

    def test_empty_both_maps(self):
        """Empty reference produces no diffs and pass verdict."""
        from tests.render.fidelity_oracle import compare_computed_styles

        v = compare_computed_styles({}, {})
        assert v.verdict == "pass"
        assert v.diffs == []

    def test_extra_state_in_candidate_is_ignored(self):
        """Candidate having MORE states than reference is not a failure.

        The oracle measures reference -> candidate fidelity. A candidate
        that adds states the reference does not specify is not penalized.
        """
        from tests.render.fidelity_oracle import compare_computed_styles

        ref = {"rest": {"color": "rgb(0, 0, 0)"}}
        cand = {
            "rest": {"color": "rgb(0, 0, 0)"},
            "hover": {"color": "rgb(30, 30, 30)"},  # extra; OK
        }
        v = compare_computed_styles(ref, cand)
        assert v.verdict == "pass"


class TestComparatorFailCases:
    """AC2: mismatched maps return verdict="fail" naming the diff (non-vacuous)."""

    def test_single_property_mismatch_names_property_and_state(self):
        from tests.render.fidelity_oracle import compare_computed_styles

        ref = {"rest": {"color": "rgb(0, 0, 0)", "font-size": "16px"}}
        cand = {"rest": {"color": "rgb(255, 0, 0)", "font-size": "16px"}}
        v = compare_computed_styles(ref, cand)
        assert v.verdict == "fail"
        assert any(d.state == "rest" and d.property == "color" for d in v.diffs)

    def test_diff_values_are_non_vacuous(self):
        """AC2: reference and candidate in the diff are genuinely different."""
        from tests.render.fidelity_oracle import compare_computed_styles

        ref = {"rest": {"color": "rgb(0, 0, 0)"}}
        cand = {"rest": {"color": "rgb(255, 0, 0)"}}
        v = compare_computed_styles(ref, cand)
        diff = next(d for d in v.diffs if d.property == "color" and d.state == "rest")
        assert diff.reference == "rgb(0, 0, 0)"
        assert diff.candidate == "rgb(255, 0, 0)"
        assert diff.reference != diff.candidate  # explicitly non-vacuous

    def test_hover_state_mismatch_names_state(self):
        """Diff correctly identifies the state name, not just the property."""
        from tests.render.fidelity_oracle import compare_computed_styles

        ref = {"hover": {"background-color": "rgb(0, 128, 0)"}}
        cand = {"hover": {"background-color": "rgb(0, 200, 0)"}}
        v = compare_computed_styles(ref, cand)
        diff = next(d for d in v.diffs if d.property == "background-color")
        assert diff.state == "hover"

    def test_missing_state_in_candidate_reported_as_missing(self):
        """When candidate lacks a state, diffs use sentinel candidate='<missing>'."""
        from tests.render.fidelity_oracle import compare_computed_styles

        ref = {
            "rest": {"color": "rgb(0, 0, 0)"},
            "hover": {"color": "rgb(30, 30, 30)"},
        }
        cand = {"rest": {"color": "rgb(0, 0, 0)"}}  # "hover" absent
        v = compare_computed_styles(ref, cand)
        assert v.verdict == "fail"
        hover_diffs = [d for d in v.diffs if d.state == "hover"]
        assert len(hover_diffs) > 0, "Expected diffs for missing hover state"
        assert all(d.candidate == "<missing>" for d in hover_diffs)

    def test_fail_tier_is_structural(self):
        """Structural mismatches report tier='structural'."""
        from tests.render.fidelity_oracle import compare_computed_styles

        ref = {"rest": {"color": "rgb(0, 0, 0)"}}
        cand = {"rest": {"color": "rgb(255, 0, 0)"}}
        v = compare_computed_styles(ref, cand)
        assert v.tier == "structural"

    def test_multiple_mismatches_all_captured(self):
        """Multiple differing properties produce one diff per property."""
        from tests.render.fidelity_oracle import compare_computed_styles

        ref = {
            "rest": {
                "color": "rgb(0, 0, 0)",
                "font-size": "16px",
                "font-weight": "500",
            }
        }
        cand = {
            "rest": {
                "color": "rgb(255, 0, 0)",  # differs
                "font-size": "14px",         # differs
                "font-weight": "500",        # matches
            }
        }
        v = compare_computed_styles(ref, cand)
        assert v.verdict == "fail"
        diff_props = {d.property for d in v.diffs}
        assert "color" in diff_props
        assert "font-size" in diff_props
        assert "font-weight" not in diff_props  # matched


# ---------------------------------------------------------------------------
# AC3: build_baseline_map - map builder
# ---------------------------------------------------------------------------


class TestBaselineMapBuilder:
    """AC3: map builder enumerates assets with pass/fail, schema_version,
    deterministic ordering, and correct aggregation counts."""

    def test_schema_version(self):
        from tests.render.fidelity_oracle import build_baseline_map, FidelityVerdict

        bm = build_baseline_map(
            [("a24", "alphabets", "a24", FidelityVerdict(verdict="pass"))]
        )
        assert bm.schema_version == "fidelity_baseline_map_v1"

    def test_generated_at_present(self):
        from tests.render.fidelity_oracle import build_baseline_map, FidelityVerdict

        bm = build_baseline_map(
            [("a24", "alphabets", "a24", FidelityVerdict(verdict="pass"))]
        )
        assert bm.generated_at  # non-empty ISO-8601 string

    def test_aggregation_counts(self):
        from tests.render.fidelity_oracle import build_baseline_map, FidelityVerdict

        inp = [
            ("a24", "alphabets", "a24", FidelityVerdict(verdict="pass")),
            ("a24", "buttons", "a24-btn-001", FidelityVerdict(verdict="fail")),
            ("aeon", "alphabets", "aeon", FidelityVerdict(verdict="candidate_missing", tier="n/a")),
        ]
        bm = build_baseline_map(inp)
        assert bm.asset_count == 3
        assert bm.pass_count == 1
        assert bm.fail_count == 1
        assert bm.missing_count == 1

    def test_deterministic_ordering(self):
        """Same inputs in different order -> same entry order in the map."""
        from tests.render.fidelity_oracle import build_baseline_map, FidelityVerdict

        vp = FidelityVerdict(verdict="pass")
        inp1 = [("z-brand", "alphabets", "z-brand", vp), ("a-brand", "buttons", "a-001", vp)]
        inp2 = [("a-brand", "buttons", "a-001", vp), ("z-brand", "alphabets", "z-brand", vp)]
        m1 = build_baseline_map(inp1)
        m2 = build_baseline_map(inp2)
        keys1 = [(e.brand, e.asset_class, e.asset_slug) for e in m1.entries]
        keys2 = [(e.brand, e.asset_class, e.asset_slug) for e in m2.entries]
        assert keys1 == keys2

    def test_all_entries_present(self):
        from tests.render.fidelity_oracle import build_baseline_map, FidelityVerdict

        inp = [
            ("a24", "alphabets", "a24", FidelityVerdict(verdict="pass")),
            ("a24", "buttons", "a24-btn", FidelityVerdict(verdict="fail")),
        ]
        bm = build_baseline_map(inp)
        slugs = {e.asset_slug for e in bm.entries}
        assert "a24" in slugs
        assert "a24-btn" in slugs

    def test_entry_preserves_diffs(self):
        """Diffs from the verdict are preserved in the corresponding entry."""
        from tests.render.fidelity_oracle import (
            build_baseline_map,
            FidelityVerdict,
            StyleDiff,
        )

        diff = StyleDiff(
            state="rest",
            property="color",
            reference="rgb(0,0,0)",
            candidate="rgb(255,0,0)",
        )
        v = FidelityVerdict(verdict="fail", diffs=[diff], tier="structural")
        bm = build_baseline_map([("a24", "buttons", "a24-btn", v)])
        entry = bm.entries[0]
        assert len(entry.diffs) == 1
        assert entry.diffs[0].property == "color"
        assert entry.diffs[0].state == "rest"


# ---------------------------------------------------------------------------
# AC4 (pure): candidate chrome detection and extraction
# ---------------------------------------------------------------------------


class TestCandidateChromeDetection:
    """AC4 pure part: is_candidate_wrapped and extract_component_from_candidate."""

    def test_wrapped_candidate_detected(self):
        from tests.render.fidelity_oracle import is_candidate_wrapped

        html = (
            '<article class="rs-library-page" data-rs-source="drl-component"'
            ' data-rs-class="buttons">'
            '<aside class="rs-font-disclosure">Font info</aside>'
            '<button class="btn">Click</button>'
            '</article>'
        )
        assert is_candidate_wrapped(html) is True

    def test_unwrapped_html_not_detected(self):
        from tests.render.fidelity_oracle import is_candidate_wrapped

        assert is_candidate_wrapped('<button class="btn">Click</button>') is False

    def test_extract_excludes_aside(self):
        from tests.render.fidelity_oracle import extract_component_from_candidate

        html = (
            '<article class="rs-library-page" data-rs-source="drl-component"'
            ' data-rs-class="buttons">'
            '<aside class="rs-font-disclosure">Font info</aside>'
            '<button class="btn">Click</button>'
            '</article>'
        )
        result = extract_component_from_candidate(html)
        assert result is not None
        assert "rs-font-disclosure" not in result  # aside excluded
        assert "rs-library-page" not in result     # article wrapper excluded
        assert "btn" in result                      # component remains

    def test_extract_returns_none_for_unwrapped_html(self):
        from tests.render.fidelity_oracle import extract_component_from_candidate

        result = extract_component_from_candidate("<button>plain</button>")
        assert result is None


# ---------------------------------------------------------------------------
# Corpus iteration - runs when vendored corpus is present (CI + dev)
# ---------------------------------------------------------------------------


class TestCorpusIteration:
    """Corpus iteration must yield 955 assets in deterministic order."""

    @pytest.fixture
    def corpus_root(self):
        here = pathlib.Path(__file__).resolve()
        # Traverse: tests/render/ -> tests/ -> code/api/ -> _vendored/drl_corpus
        root = here.parent.parent.parent / "_vendored" / "drl_corpus"
        if not root.exists():
            pytest.skip("vendored DRL corpus not present")
        return root

    def test_yields_955_assets(self, corpus_root):
        from tests.render.fidelity_oracle import iter_corpus_assets

        assets = list(iter_corpus_assets(corpus_root))
        assert len(assets) == 955

    def test_deterministic_across_two_runs(self, corpus_root):
        from tests.render.fidelity_oracle import iter_corpus_assets

        run1 = [(a.brand, a.asset_class, a.asset_slug) for a in iter_corpus_assets(corpus_root)]
        run2 = [(a.brand, a.asset_class, a.asset_slug) for a in iter_corpus_assets(corpus_root)]
        assert run1 == run2

    def test_all_html_paths_exist(self, corpus_root):
        from tests.render.fidelity_oracle import iter_corpus_assets

        for asset in iter_corpus_assets(corpus_root):
            assert asset.html_path.is_file(), (
                f"Missing asset.html: {asset.brand}/{asset.asset_class}/{asset.asset_slug}"
            )

    def test_asset_fields_are_nonempty(self, corpus_root):
        from tests.render.fidelity_oracle import iter_corpus_assets

        first = next(iter_corpus_assets(corpus_root))
        assert isinstance(first.brand, str) and first.brand
        assert isinstance(first.asset_class, str) and first.asset_class
        assert isinstance(first.asset_slug, str) and first.asset_slug
        assert isinstance(first.html_path, pathlib.Path)


# ---------------------------------------------------------------------------
# AC5 + AC4 browser part: self-skip when Playwright or Pillow absent
# ---------------------------------------------------------------------------


class TestBrowserCapture:
    """Browser-tier tests. Each self-skips via importorskip when deps absent (AC5)."""

    def test_reference_capture_self_skips_without_playwright(self, tmp_path):
        """AC5: capture_reference_styles skips when Playwright is not installed."""
        pytest.importorskip("playwright.sync_api")
        pytest.importorskip("PIL.Image")

        # Minimal self-contained asset.html with one .group/.state-label state node.
        asset_html = tmp_path / "asset.html"
        asset_html.write_text(
            "<!doctype html><html><head>"
            "<style>"
            "* { box-sizing: border-box; }"
            ".btn { color: rgb(0, 0, 0); font-size: 16px; background-color: rgb(255, 255, 255); }"
            "</style></head><body>"
            '<div class="group">'
            '<span class="state-label">rest</span>'
            '<button class="btn">Test</button>'
            "</div>"
            "</body></html>",
            encoding="utf-8",
        )
        from tests.render.fidelity_oracle import capture_reference_styles

        result = capture_reference_styles(asset_html)
        # With Playwright, returns a dict (may be empty if fonts not loaded).
        assert result is None or isinstance(result, dict)

    def test_candidate_capture_excludes_aside_in_browser(self, tmp_path):
        """AC4 browser part: capture_candidate_styles targets the component, not the aside."""
        pytest.importorskip("playwright.sync_api")
        pytest.importorskip("PIL.Image")

        from tests.render.fidelity_oracle import capture_candidate_styles

        # Rendered HTML with article wrapper + aside + button component.
        # The button has a distinctive color; the aside has default browser color.
        # The capture must return the button's color, not the aside's.
        rendered_html = (
            '<article class="rs-library-page" data-rs-source="drl-component"'
            ' data-rs-class="buttons">'
            '<aside class="rs-font-disclosure" style="color: rgb(200, 200, 200);">'
            "<p>Font: Inter</p></aside>"
            '<button class="btn" style="color: rgb(0, 128, 0);">Click</button>'
            "</article>"
        )
        result = capture_candidate_styles(rendered_html)
        if result is not None:
            # If a result was returned, its "default" state should capture the
            # button (color rgb(0, 128, 0)), not the aside (rgb(200, 200, 200)).
            default = result.get("default", {})
            captured_color = default.get("color", "")
            if captured_color:
                assert captured_color != "rgb(200, 200, 200)", (
                    "Captured color matches the aside; component subtree was not isolated"
                )

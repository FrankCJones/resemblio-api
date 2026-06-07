"""Tests for app.library_render_policy - render-real-or-hide decision (Phase 2).

TDD: tests written BEFORE implementation. These pin the D2 invariant:
  - An uncaptured component-showcase category must NOT emit the component body.
  - A captured component-showcase category MUST emit the component body.
  - Page-pattern categories render regardless of component-group capture status.

The critical test is the uncaptured-button assertion: a DRL seed brand (no button
snapshot, no button geometry) must produce NO '.b-btn' chiclet in its 'buttons'
category render. Today (before Phase 2) it DOES produce one. These tests watch
that failure before the policy is applied.

The D2 line (plan Section 2, Phase 2 step 3):
  - Token-level var() fallback for cascade safety: ALLOWED. The :root block still
    emits default values for uncaptured slots so var(--ds-button-padding-y)
    doesn't cascade to 'unset'. The CSS variable EXISTS; the COMPONENT does not render.
  - Rendering a whole component body painted with default geometry: FORBIDDEN.
    Hiding the component entirely is the correct response when it isn't captured.
"""
from __future__ import annotations

import pytest

from app.brand_capture_manifest import build_capture_manifest
from app.library_render_policy import (
    CATEGORY_CAPTURE_REQUIREMENTS,
    CategoryRenderDecision,
    evaluate_category_render,
    filter_captured_categories,
)


# ---------------------------------------------------------------------------
# Fixtures re-used from the manifest test module
# ---------------------------------------------------------------------------

# DRL-seed brand: color/typography/spacing/radius/layout/section captured;
# button/card/badge/input NOT captured.
DRL_SEED_TOKENS: dict[str, str] = {
    "ds-bg": "#ffffff",
    "ds-surface": "#f6f9fc",
    "ds-text": "#0a2540",
    "ds-accent": "#635bff",
    "ds-font-display": "Sohne, system-ui",
    "ds-font-body": "Sohne, system-ui",
    "ds-font-weight-display": "600",
    "ds-radius-sm": "6px",
    "ds-radius-md": "8px",
    "ds-space-4": "16px",
    "ds-page-pad-x": "32px",
    "ds-page-max-default": "880px",
    "ds-section-padding-x": "32px",
    "ds-section-padding-y": "96px",
}

# Fully captured brand: adds button + card + badge + input geometry.
FULL_CAPTURE_TOKENS: dict[str, str] = {
    **DRL_SEED_TOKENS,
    "ds-button-padding-y": "12px",
    "ds-button-padding-x": "24px",
    "ds-button-border-width": "0px",
    "ds-button-radius": "9999px",
    "ds-card-border-width": "1px",
    "ds-card-padding": "24px",
    "ds-badge-padding-y": "3px",
    "ds-badge-padding-x": "10px",
    "ds-input-padding-y": "10px",
    "ds-input-border-width": "1px",
}


# ---------------------------------------------------------------------------
# CATEGORY_CAPTURE_REQUIREMENTS shape
# ---------------------------------------------------------------------------

class TestCaptureRequirementsMap:
    """The requirements map covers exactly the component-showcase categories."""

    def test_buttons_requires_button_group(self) -> None:
        assert "button" in CATEGORY_CAPTURE_REQUIREMENTS["buttons"]

    def test_cards_requires_card_group(self) -> None:
        assert "card" in CATEGORY_CAPTURE_REQUIREMENTS["cards"]

    def test_badges_requires_badge_group(self) -> None:
        assert "badge" in CATEGORY_CAPTURE_REQUIREMENTS["badges"]

    def test_form_fields_requires_input_group(self) -> None:
        assert "input" in CATEGORY_CAPTURE_REQUIREMENTS["form-fields"]

    def test_inputs_requires_input_group(self) -> None:
        assert "input" in CATEGORY_CAPTURE_REQUIREMENTS["inputs"]

    def test_library_requires_multiple_groups(self) -> None:
        reqs = CATEGORY_CAPTURE_REQUIREMENTS["library"]
        assert "button" in reqs
        assert "card" in reqs

    def test_page_pattern_categories_not_in_map(self) -> None:
        # Page-pattern categories have no component-group gate; they render always.
        page_patterns = [
            "hero", "navigation", "footer", "cta-block", "feature-grid",
            "testimonials", "pricing-table", "process-steps", "news-list",
            "article-layout", "about-team", "alphabet",
        ]
        for cat in page_patterns:
            assert cat not in CATEGORY_CAPTURE_REQUIREMENTS, (
                f"{cat} should NOT have a capture requirement (it is a page pattern)"
            )


# ---------------------------------------------------------------------------
# evaluate_category_render - the core decision function
# ---------------------------------------------------------------------------

class TestEvaluateCategoryRender:
    """evaluate_category_render returns the correct render decision."""

    def test_buttons_not_rendered_for_drl_seed(self) -> None:
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        decision = evaluate_category_render("buttons", manifest)
        assert isinstance(decision, CategoryRenderDecision)
        assert not decision.should_render
        assert decision.missing_groups == ("button",)

    def test_cards_not_rendered_for_drl_seed(self) -> None:
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        decision = evaluate_category_render("cards", manifest)
        assert not decision.should_render
        assert decision.missing_groups == ("card",)

    def test_badges_not_rendered_for_drl_seed(self) -> None:
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        decision = evaluate_category_render("badges", manifest)
        assert not decision.should_render

    def test_form_fields_not_rendered_for_drl_seed(self) -> None:
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        decision = evaluate_category_render("form-fields", manifest)
        assert not decision.should_render

    def test_inputs_not_rendered_for_drl_seed(self) -> None:
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        decision = evaluate_category_render("inputs", manifest)
        assert not decision.should_render

    def test_buttons_rendered_for_full_capture(self) -> None:
        manifest = build_capture_manifest(FULL_CAPTURE_TOKENS)
        decision = evaluate_category_render("buttons", manifest)
        assert decision.should_render
        assert decision.missing_groups == ()

    def test_cards_rendered_for_full_capture(self) -> None:
        manifest = build_capture_manifest(FULL_CAPTURE_TOKENS)
        decision = evaluate_category_render("cards", manifest)
        assert decision.should_render

    def test_hero_always_renders_regardless_of_capture(self) -> None:
        # Page patterns have no capture requirement; they render even for
        # a brand with an empty token bag.
        manifest = build_capture_manifest({})
        decision = evaluate_category_render("hero", manifest)
        assert decision.should_render
        assert decision.missing_groups == ()

    def test_alphabet_always_renders(self) -> None:
        manifest = build_capture_manifest({})
        decision = evaluate_category_render("alphabet", manifest)
        assert decision.should_render

    def test_navigation_always_renders(self) -> None:
        manifest = build_capture_manifest({})
        decision = evaluate_category_render("navigation", manifest)
        assert decision.should_render

    def test_unknown_category_renders(self) -> None:
        # A future DRL category not in the map should render by default.
        manifest = build_capture_manifest({})
        decision = evaluate_category_render("brand-new-category", manifest)
        assert decision.should_render


# ---------------------------------------------------------------------------
# filter_captured_categories - batch decision
# ---------------------------------------------------------------------------

class TestFilterCapturedCategories:
    """filter_captured_categories returns a correctly partitioned result."""

    def test_drl_seed_partitioning(self) -> None:
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        all_cats = list(CATEGORY_CAPTURE_REQUIREMENTS.keys()) + ["hero", "alphabet"]
        decisions = filter_captured_categories(all_cats, manifest)
        # Component showcases should be hidden
        for cat in ["buttons", "cards", "badges", "form-fields", "inputs"]:
            assert cat in decisions
            assert not decisions[cat].should_render, f"{cat} should be hidden"
        # Page patterns should render
        for cat in ["hero", "alphabet"]:
            assert decisions[cat].should_render, f"{cat} should render"

    def test_full_capture_all_render(self) -> None:
        manifest = build_capture_manifest(FULL_CAPTURE_TOKENS)
        all_cats = list(CATEGORY_CAPTURE_REQUIREMENTS.keys()) + ["hero", "alphabet"]
        decisions = filter_captured_categories(all_cats, manifest)
        for cat, decision in decisions.items():
            assert decision.should_render, f"{cat} should render with full capture"

    def test_deterministic_output(self) -> None:
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        cats = ["buttons", "hero"]
        r1 = filter_captured_categories(cats, manifest)
        r2 = filter_captured_categories(cats, manifest)
        assert r1 == r2


# ---------------------------------------------------------------------------
# D2 invariant: the line between cascade-safety fallback and component fabrication
# ---------------------------------------------------------------------------

class TestD2Invariant:
    """Document and test the D2 conceptual line.

    cascade-safety fallback: the :root block emitting var(--ds-button-padding-y)
    at contract default is ALLOWED - the CSS variable exists so var() doesn't
    cascade to 'unset' and break other uses.

    component fabrication: rendering the actual button HTML (the .b-btn block
    with all its padding/border/radius CSS) when the brand has no real button
    data - FORBIDDEN.

    This test is conceptual and pure-data (no HTML render). Phase 2's indexer
    integration test (in test_library_indexer_render_policy.py) pins the HTML
    output. Here we assert that evaluate_category_render correctly gates the
    HTML emission decision.
    """

    def test_uncaptured_button_decision_is_no_render(self) -> None:
        # This is the D2 line: 'buttons' for a DRL seed brand must return
        # should_render=False so the indexer omits the HTML body.
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        decision = evaluate_category_render("buttons", manifest)
        assert not decision.should_render, (
            "D2 VIOLATED: 'buttons' category must not render for a brand "
            "without button capture. The :root CSS var block may still emit "
            "default button slot values (cascade safety), but the button "
            "HTML body must be omitted."
        )

    def test_captured_button_decision_is_render(self) -> None:
        from extractor.button_tokens import ButtonTokens
        bt: ButtonTokens = {
            "--ds-button-radius": "9999px",
            "--ds-button-padding-block": "12px",
            "--ds-button-padding-inline": "24px",
            "--ds-button-font-size": "14px",
            "--ds-button-font-weight": "600",
            "--ds-button-font-family": "system-ui",
            "--ds-button-border-width": "0px",
        }
        manifest = build_capture_manifest(DRL_SEED_TOKENS, button_tokens=bt)
        decision = evaluate_category_render("buttons", manifest)
        assert decision.should_render, (
            "A brand WITH a ButtonTokens snapshot must render the 'buttons' category."
        )

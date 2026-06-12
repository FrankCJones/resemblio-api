"""Phase 0.B RED test: capture plan builder.

Tests that build_capture_plan produces a deterministic, exhaustive
CaptureTarget list covering every (brand x surface x viewport) tuple,
with correct URLs, filenames, and counts.

These tests are pure-data: no network, no browser. The module under test
(capture_plan.py in this same package) is absent when this test is first
committed; tests go RED for the right reason (ImportError / missing symbol).
"""
from __future__ import annotations

import re

import pytest


# ---------------------------------------------------------------------------
# The first three imports are RED when capture_plan.py does not yet exist.
# ---------------------------------------------------------------------------
from tests.render.capture_plan import (  # noqa: E402 - intentional RED import
    DESKTOP_VIEWPORT,
    MOBILE_VIEWPORT,
    Surface,
    Viewport,
    CaptureTarget,
    build_capture_plan,
)


# ---------------------------------------------------------------------------
# Constants and helpers
# ---------------------------------------------------------------------------

_SYNTHETIC_BRANDS = ["apple", "stripe", "gwern"]
_BASE_URL = "https://resemblio.com"

# Slug-safe filename pattern: only alphanumeric, hyphens, underscores, dots.
_SAFE_FILENAME_RE = re.compile(r"^[a-zA-Z0-9_\-\.]+$")


# ---------------------------------------------------------------------------
# Viewport constants
# ---------------------------------------------------------------------------


def test_desktop_viewport_dimensions() -> None:
    """DESKTOP_VIEWPORT is 1440x900 per D16 harness spec."""
    assert DESKTOP_VIEWPORT.width == 1440
    assert DESKTOP_VIEWPORT.height == 900
    assert DESKTOP_VIEWPORT.label == "desktop"


def test_mobile_viewport_dimensions() -> None:
    """MOBILE_VIEWPORT is 390x844 per D16 harness spec."""
    assert MOBILE_VIEWPORT.width == 390
    assert MOBILE_VIEWPORT.height == 844
    assert MOBILE_VIEWPORT.label == "mobile"


# ---------------------------------------------------------------------------
# Surface constants
# ---------------------------------------------------------------------------


def test_surface_landing_url_path() -> None:
    """Surface.LANDING maps to the /library/<slug> hub page."""
    assert Surface.LANDING.url_path_template == "/library/{slug}"


def test_surface_specimen_url_path() -> None:
    """Surface.SPECIMEN maps to the /library/<slug>/alphabet specimen page."""
    assert Surface.SPECIMEN.url_path_template == "/library/{slug}/alphabet"


def test_surface_labels_are_strings() -> None:
    """Surface labels are non-empty strings used in filenames."""
    assert isinstance(Surface.LANDING.label, str) and Surface.LANDING.label
    assert isinstance(Surface.SPECIMEN.label, str) and Surface.SPECIMEN.label


# ---------------------------------------------------------------------------
# build_capture_plan: count
# ---------------------------------------------------------------------------


def test_capture_plan_count_matches_formula() -> None:
    """Plan has exactly len(brands) * 2 surfaces * 2 viewports targets."""
    brands = _SYNTHETIC_BRANDS
    plan = build_capture_plan(brands, base_url=_BASE_URL)
    expected = len(brands) * 2 * 2
    assert len(plan) == expected, (
        f"Expected {expected} targets for {len(brands)} brands x 2 surfaces "
        f"x 2 viewports; got {len(plan)}"
    )


def test_capture_plan_empty_brands_returns_empty() -> None:
    """Empty brand list produces an empty plan (edge case per docstring)."""
    plan = build_capture_plan([], base_url=_BASE_URL)
    assert plan == []


# ---------------------------------------------------------------------------
# build_capture_plan: exhaustiveness
# ---------------------------------------------------------------------------


def test_capture_plan_every_brand_appears() -> None:
    """Every brand slug in the input appears in the output."""
    brands = _SYNTHETIC_BRANDS
    plan = build_capture_plan(brands, base_url=_BASE_URL)
    plan_slugs = {t.brand_slug for t in plan}
    for slug in brands:
        assert slug in plan_slugs, f"Brand '{slug}' missing from capture plan"


def test_capture_plan_no_extra_brands() -> None:
    """No brand appears in the plan that was not in the input."""
    brands = _SYNTHETIC_BRANDS
    plan = build_capture_plan(brands, base_url=_BASE_URL)
    plan_slugs = {t.brand_slug for t in plan}
    assert plan_slugs == set(brands)


def test_capture_plan_both_surfaces_present_per_brand() -> None:
    """Each brand has both landing and specimen surface targets."""
    brands = _SYNTHETIC_BRANDS
    plan = build_capture_plan(brands, base_url=_BASE_URL)
    for slug in brands:
        surfaces_for_slug = {
            t.surface for t in plan if t.brand_slug == slug
        }
        assert Surface.LANDING in surfaces_for_slug, (
            f"Brand '{slug}' missing LANDING surface"
        )
        assert Surface.SPECIMEN in surfaces_for_slug, (
            f"Brand '{slug}' missing SPECIMEN surface"
        )


def test_capture_plan_both_viewports_present_per_brand_surface() -> None:
    """Each (brand, surface) combination has both desktop and mobile targets."""
    brands = _SYNTHETIC_BRANDS
    plan = build_capture_plan(brands, base_url=_BASE_URL)
    for slug in brands:
        for surface in (Surface.LANDING, Surface.SPECIMEN):
            viewports_for = {
                t.viewport_label
                for t in plan
                if t.brand_slug == slug and t.surface == surface
            }
            assert "desktop" in viewports_for, (
                f"({slug}, {surface.label}) missing desktop viewport"
            )
            assert "mobile" in viewports_for, (
                f"({slug}, {surface.label}) missing mobile viewport"
            )


# ---------------------------------------------------------------------------
# build_capture_plan: URL correctness
# ---------------------------------------------------------------------------


def test_capture_plan_landing_url_format() -> None:
    """Landing targets have URL matching /library/<slug>."""
    plan = build_capture_plan(_SYNTHETIC_BRANDS, base_url=_BASE_URL)
    for target in plan:
        if target.surface == Surface.LANDING:
            expected = f"{_BASE_URL}/library/{target.brand_slug}"
            assert target.url == expected, (
                f"LANDING URL mismatch for '{target.brand_slug}': "
                f"got {target.url!r}, want {expected!r}"
            )


def test_capture_plan_specimen_url_format() -> None:
    """Specimen targets have URL matching /library/<slug>/alphabet."""
    plan = build_capture_plan(_SYNTHETIC_BRANDS, base_url=_BASE_URL)
    for target in plan:
        if target.surface == Surface.SPECIMEN:
            expected = (
                f"{_BASE_URL}/library/{target.brand_slug}/alphabet"
            )
            assert target.url == expected, (
                f"SPECIMEN URL mismatch for '{target.brand_slug}': "
                f"got {target.url!r}, want {expected!r}"
            )


def test_capture_plan_base_url_override() -> None:
    """base_url prefix is honored, no trailing slash injected."""
    plan = build_capture_plan(["stripe"], base_url="https://staging.example.com")
    for target in plan:
        assert target.url.startswith("https://staging.example.com"), (
            f"URL does not start with base_url: {target.url!r}"
        )
        assert "//library" not in target.url, (
            "Double slash in URL from base_url trailing slash handling"
        )


# ---------------------------------------------------------------------------
# build_capture_plan: filename determinism and slug-safety
# ---------------------------------------------------------------------------


def test_capture_plan_filenames_are_deterministic() -> None:
    """Same inputs produce the same filenames on repeated calls."""
    plan_a = build_capture_plan(_SYNTHETIC_BRANDS, base_url=_BASE_URL)
    plan_b = build_capture_plan(_SYNTHETIC_BRANDS, base_url=_BASE_URL)
    for a, b in zip(plan_a, plan_b):
        assert a.output_filename == b.output_filename, (
            "Filenames differ across calls with the same inputs"
        )


def test_capture_plan_filenames_are_slug_safe() -> None:
    """Output filenames contain only alphanumeric, hyphen, underscore, dot."""
    tricky_brands = ["are-na", "frank-chimero", "the-pudding", "read-cv"]
    plan = build_capture_plan(tricky_brands, base_url=_BASE_URL)
    for target in plan:
        assert _SAFE_FILENAME_RE.match(target.output_filename), (
            f"Filename '{target.output_filename}' contains unsafe characters"
        )


def test_capture_plan_filenames_are_unique() -> None:
    """No two targets in the same plan share an output filename."""
    plan = build_capture_plan(_SYNTHETIC_BRANDS, base_url=_BASE_URL)
    filenames = [t.output_filename for t in plan]
    assert len(filenames) == len(set(filenames)), (
        "Duplicate filenames detected in capture plan"
    )


def test_capture_plan_filenames_end_with_png() -> None:
    """All output filenames have a .png extension."""
    plan = build_capture_plan(_SYNTHETIC_BRANDS, base_url=_BASE_URL)
    for target in plan:
        assert target.output_filename.endswith(".png"), (
            f"Expected .png extension on '{target.output_filename}'"
        )


# ---------------------------------------------------------------------------
# CaptureTarget shape
# ---------------------------------------------------------------------------


def test_capture_target_has_required_fields() -> None:
    """CaptureTarget exposes all required attributes."""
    plan = build_capture_plan(["stripe"], base_url=_BASE_URL)
    assert len(plan) == 4  # 1 brand x 2 surfaces x 2 viewports
    for target in plan:
        assert hasattr(target, "brand_slug")
        assert hasattr(target, "surface")
        assert hasattr(target, "viewport_label")
        assert hasattr(target, "width")
        assert hasattr(target, "height")
        assert hasattr(target, "url")
        assert hasattr(target, "output_filename")


def test_capture_target_viewport_dimensions_match_constants() -> None:
    """CaptureTarget width/height mirror the viewport constant values."""
    plan = build_capture_plan(["stripe"], base_url=_BASE_URL)
    for target in plan:
        if target.viewport_label == "desktop":
            assert target.width == 1440
            assert target.height == 900
        else:
            assert target.viewport_label == "mobile"
            assert target.width == 390
            assert target.height == 844

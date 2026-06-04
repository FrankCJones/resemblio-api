"""Unit tests for extractor.screenshot_palette.

The screenshot palette helper is the deterministic pre-LLM signal that
closes the "extraction misses actually-rendered brand colors" diagnostic
from the 2026-06-04 ENC Explorer redesign bug report. These tests
exercise the pure-data report shape, the JS template, the dedup against
declared colors, and the prompt rendering. Live-browser execution is
opt-in via RESEMBLIO_RUN_REAL_BROWSER=1 to keep CI hermetic.

Source bug report:
    projects/Resemblio/_handoff/inbox/claude/2026-06-04-extraction-misses-rendered-colors-BUG.md
"""
from __future__ import annotations

import pytest

from extractor.screenshot_palette import (
    COLOR_SIMILARITY_THRESHOLD,
    DEFAULT_VIEWPORT_HEIGHT,
    DEFAULT_VIEWPORT_WIDTH,
    DOMINANT_PIXEL_FRACTION,
    MAX_DOMINANT_COLORS,
    QUANTIZATION_STEP,
    SCHEMA_VERSION,
    DominantColor,
    ScreenshotPaletteReport,
    _coerce_payload,
    build_capture_script,
    capture_screenshot_palette,
    empty_report,
    filter_against_declared,
    hex_to_rgb,
    render_for_prompt,
    rgb_distance,
)


# ---------------------------------------------------------------------------
# Report-shape and helper invariants
# ---------------------------------------------------------------------------


def test_empty_report_has_all_required_fields() -> None:
    """Helper produces a well-formed empty report for any status."""
    report = empty_report("skipped", "test reason")
    assert report["status"] == "skipped"
    assert report["colors"] == []
    assert report["viewport"] == (0, 0)
    assert report["total_pixels"] == 0
    assert report["error"] == "test reason"
    assert report["schema_version"] == SCHEMA_VERSION


def test_capture_with_neither_input_returns_error() -> None:
    """Caller must supply html OR url; neither is a programming error."""
    report = capture_screenshot_palette()
    assert report["status"] == "error"
    assert report["error"] is not None
    assert "neither html nor url" in report["error"]


def test_capture_with_both_inputs_returns_error() -> None:
    """Caller must supply html OR url, not both."""
    report = capture_screenshot_palette(html="<html></html>", url="https://x")
    assert report["status"] == "error"
    assert report["error"] is not None


def test_build_capture_script_embeds_thresholds() -> None:
    """The generated JS embeds the tuning constants verbatim."""
    script = build_capture_script()
    assert str(QUANTIZATION_STEP) in script
    assert str(MAX_DOMINANT_COLORS) in script
    # Floating threshold appears as float literal in the script.
    assert f"{DOMINANT_PIXEL_FRACTION}" in script or "0.005" in script
    # Sanity: script is a single arrow function body.
    assert script.strip().startswith("() =>")


# ---------------------------------------------------------------------------
# hex_to_rgb / rgb_distance / hex parsing edge cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hex_input,expected",
    [
        ("#ffffff", (255, 255, 255)),
        ("#000000", (0, 0, 0)),
        ("#f8485e", (248, 72, 94)),
        ("#592a8a", (89, 42, 138)),
        ("#FFF", (255, 255, 255)),
        ("f8485e", (248, 72, 94)),  # leading '#' optional
        ("  #f8485e  ", (248, 72, 94)),  # whitespace tolerated
    ],
)
def test_hex_to_rgb_accepts_valid_forms(
    hex_input: str, expected: tuple[int, int, int]
) -> None:
    """The parser accepts standard hex inputs in shorthand and longhand."""
    assert hex_to_rgb(hex_input) == expected


@pytest.mark.parametrize(
    "bad_input",
    ["", None, "#", "#abc12", "#zzzzzz", "not a color", "#1234567"],
)
def test_hex_to_rgb_rejects_malformed(bad_input: object) -> None:
    """Malformed input returns None rather than raising."""
    assert hex_to_rgb(bad_input) is None  # type: ignore[arg-type]


def test_rgb_distance_is_symmetric_and_zero_for_identical() -> None:
    """Distance is symmetric, zero between identical colors, positive otherwise."""
    a = (89, 42, 138)
    b = (248, 72, 94)
    assert rgb_distance(a, a) == 0.0
    assert rgb_distance(a, b) == rgb_distance(b, a)
    assert rgb_distance(a, b) > 0


# ---------------------------------------------------------------------------
# Payload coercion
# ---------------------------------------------------------------------------


def test_coerce_payload_happy_path() -> None:
    """A well-shaped JS payload produces status=ok with shaped colors."""
    raw = {
        "viewport": [1280, 800],
        "totalPixels": 1024000,
        "buckets": [
            {"r": 248, "g": 72, "b": 94, "count": 50000, "fraction": 0.0488},
            {"r": 89, "g": 42, "b": 138, "count": 30000, "fraction": 0.0293},
        ],
    }
    report = _coerce_payload(raw)
    assert report["status"] == "ok"
    assert report["viewport"] == (1280, 800)
    assert report["total_pixels"] == 1024000
    assert len(report["colors"]) == 2
    assert report["colors"][0]["hex"] == "#f8485e"
    assert report["colors"][0]["rgb"] == (248, 72, 94)
    assert report["colors"][1]["hex"] == "#592a8a"


def test_coerce_payload_js_error_surfaces_as_error_status() -> None:
    """A JS-side {error: ...} payload becomes status=error with the message."""
    raw = {"error": "tainted canvas"}
    report = _coerce_payload(raw)
    assert report["status"] == "error"
    assert "tainted canvas" in (report["error"] or "")


def test_coerce_payload_malformed_buckets_skip_individual_entries() -> None:
    """One bad bucket does not poison the whole report."""
    raw = {
        "viewport": [1280, 800],
        "totalPixels": 1024000,
        "buckets": [
            {"r": 248, "g": 72, "b": 94, "count": 50000, "fraction": 0.0488},
            "not a dict",
            {"r": "bad", "g": 1, "b": 2, "count": 100, "fraction": 0.0001},
            {"r": 89, "g": 42, "b": 138, "count": 30000, "fraction": 0.0293},
        ],
    }
    report = _coerce_payload(raw)
    assert report["status"] == "ok"
    assert len(report["colors"]) == 2


def test_coerce_payload_missing_viewport_is_error() -> None:
    """A payload missing viewport is a programming/protocol error."""
    raw = {"totalPixels": 1024000, "buckets": []}
    report = _coerce_payload(raw)
    assert report["status"] == "error"


def test_coerce_payload_rejects_non_dict() -> None:
    """A non-dict payload is an error, not a silent empty result."""
    report = _coerce_payload([1, 2, 3])
    assert report["status"] == "error"


# ---------------------------------------------------------------------------
# Declared-color dedup
# ---------------------------------------------------------------------------


def _make_report(colors: list[DominantColor]) -> ScreenshotPaletteReport:
    """Build a status=ok report with a custom color list for filter tests."""
    return ScreenshotPaletteReport(
        status="ok",
        colors=colors,
        viewport=(1280, 800),
        total_pixels=1024000,
        error=None,
        schema_version=SCHEMA_VERSION,
    )


def test_filter_returns_full_list_when_no_declared() -> None:
    """No declared baseline means every dominant color survives."""
    colors: list[DominantColor] = [
        DominantColor(hex="#f8485e", rgb=(248, 72, 94), pixel_count=50000, pixel_fraction=0.05),
    ]
    survivors = filter_against_declared(_make_report(colors), [])
    assert survivors == colors


def test_filter_drops_near_declared_color() -> None:
    """Dominant colors close to a declared color collapse to the declared one."""
    colors: list[DominantColor] = [
        DominantColor(hex="#f8485e", rgb=(248, 72, 94), pixel_count=50000, pixel_fraction=0.05),
        DominantColor(hex="#592a8a", rgb=(89, 42, 138), pixel_count=30000, pixel_fraction=0.03),
    ]
    # #f8485e is itself in the declared list -> drop.
    survivors = filter_against_declared(_make_report(colors), ["#f8485e"])
    assert len(survivors) == 1
    assert survivors[0]["hex"] == "#592a8a"


def test_filter_drops_within_threshold_distance() -> None:
    """A near-neighbour of a declared color is collapsed even without exact match."""
    colors: list[DominantColor] = [
        DominantColor(hex="#f8475d", rgb=(248, 71, 93), pixel_count=50000, pixel_fraction=0.05),
    ]
    survivors = filter_against_declared(_make_report(colors), ["#f8485e"])
    assert survivors == []


def test_filter_ignores_malformed_declared_entries() -> None:
    """A bad hex in the declared list is silently skipped, not raised."""
    colors: list[DominantColor] = [
        DominantColor(hex="#f8485e", rgb=(248, 72, 94), pixel_count=50000, pixel_fraction=0.05),
    ]
    survivors = filter_against_declared(_make_report(colors), ["not a color", "#zzz", "#f8485e"])
    assert survivors == []


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def test_render_for_prompt_empty_returns_empty_string() -> None:
    """No surviving colors means no Markdown block; caller omits the section."""
    report = empty_report("ok")
    assert render_for_prompt(report) == ""


def test_render_for_prompt_includes_each_color_with_percent() -> None:
    """Each color line carries hex + percent + weighted-unit count."""
    colors: list[DominantColor] = [
        DominantColor(hex="#f8485e", rgb=(248, 72, 94), pixel_count=50000, pixel_fraction=0.0488),
        DominantColor(hex="#592a8a", rgb=(89, 42, 138), pixel_count=30000, pixel_fraction=0.0293),
    ]
    rendered = render_for_prompt(_make_report(colors))
    assert "#f8485e" in rendered
    assert "#592a8a" in rendered
    assert "4.88%" in rendered
    assert "2.93%" in rendered


def test_render_for_prompt_with_declared_surfaces_only_gap_colors() -> None:
    """The core cross-check: the prompt names only the MISSING colors."""
    colors: list[DominantColor] = [
        DominantColor(hex="#f8485e", rgb=(248, 72, 94), pixel_count=50000, pixel_fraction=0.05),
        DominantColor(hex="#592a8a", rgb=(89, 42, 138), pixel_count=30000, pixel_fraction=0.03),
        DominantColor(hex="#ffffff", rgb=(255, 255, 255), pixel_count=900000, pixel_fraction=0.9),
    ]
    rendered = render_for_prompt(
        _make_report(colors),
        declared_hex_colors=["#ffffff", "#007cba"],
    )
    # White is declared (matches body bg) so it must be filtered out.
    assert "#ffffff" not in rendered
    # The two missing brand colors must be present.
    assert "#f8485e" in rendered
    assert "#592a8a" in rendered
    # The header must frame the block as "not represented in declared tokens."
    assert "NOT represented" in rendered


# ---------------------------------------------------------------------------
# ENC Explorer regression fixture (the bug report's canonical case)
# ---------------------------------------------------------------------------


def test_enc_explorer_fixture_surfaces_coral_and_purple_through_cross_check() -> None:
    """The 2026-06-04 ENC bug case: WP declares #007cba; render shows coral + purple.

    Cross-check must surface coral (#f8485e) and purple (#592a8a) as
    NEW colors the declared pipeline missed. This is the test that
    locks the fix against regression.
    """
    declared = [
        "#ffffff", "#f5f5f5", "#eeeeee", "#313131", "#000000",
        "#abb8c3", "#dddddd", "#007cba", "#006ba1",
    ]
    rendered_dominant: list[DominantColor] = [
        DominantColor(hex="#ffffff", rgb=(255, 255, 255), pixel_count=21144, pixel_fraction=0.70),
        DominantColor(hex="#f8485e", rgb=(248, 72, 94), pixel_count=768, pixel_fraction=0.025),
        DominantColor(hex="#592a8a", rgb=(89, 42, 138), pixel_count=734, pixel_fraction=0.024),
        DominantColor(hex="#1e73be", rgb=(30, 115, 190), pixel_count=118, pixel_fraction=0.004),
    ]
    report = _make_report(rendered_dominant)
    survivors = filter_against_declared(report, declared)
    survivor_hexes = {c["hex"] for c in survivors}
    # The actual brand colors must surface as gaps.
    assert "#f8485e" in survivor_hexes
    assert "#592a8a" in survivor_hexes
    # The declared white must be filtered.
    assert "#ffffff" not in survivor_hexes
    # The fix should NOT spuriously surface the declared accent.
    assert "#007cba" not in survivor_hexes


def test_enc_explorer_fixture_prompt_block_carries_coral_and_purple() -> None:
    """End-to-end: the LLM-facing prompt block names the missing brand colors."""
    declared = [
        "#ffffff", "#f5f5f5", "#eeeeee", "#313131", "#000000",
        "#abb8c3", "#dddddd", "#007cba", "#006ba1",
    ]
    rendered_dominant: list[DominantColor] = [
        DominantColor(hex="#ffffff", rgb=(255, 255, 255), pixel_count=21144, pixel_fraction=0.70),
        DominantColor(hex="#f8485e", rgb=(248, 72, 94), pixel_count=768, pixel_fraction=0.025),
        DominantColor(hex="#592a8a", rgb=(89, 42, 138), pixel_count=734, pixel_fraction=0.024),
    ]
    rendered = render_for_prompt(_make_report(rendered_dominant), declared_hex_colors=declared)
    assert "#f8485e" in rendered
    assert "#592a8a" in rendered
    assert "#ffffff" not in rendered
    assert "NOT represented" in rendered


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------


def test_default_viewport_is_reasonable() -> None:
    """Defaults match a common desktop ICP viewport."""
    assert DEFAULT_VIEWPORT_WIDTH >= 1024
    assert DEFAULT_VIEWPORT_HEIGHT >= 600


def test_dominance_threshold_is_a_fraction() -> None:
    """The threshold is a fraction in (0, 1)."""
    assert 0.0 < DOMINANT_PIXEL_FRACTION < 1.0


def test_color_similarity_threshold_is_positive() -> None:
    """Distance threshold is a positive RGB delta."""
    assert COLOR_SIMILARITY_THRESHOLD > 0

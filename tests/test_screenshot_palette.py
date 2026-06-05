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

import os

import pytest

from extractor.screenshot_palette import (
    COLOR_SIMILARITY_THRESHOLD,
    DEFAULT_TIMEOUT_MS,
    DEFAULT_VIEWPORT_HEIGHT,
    DEFAULT_VIEWPORT_WIDTH,
    DOMINANT_PIXEL_FRACTION,
    MAX_DOMINANT_COLORS,
    QUANTIZATION_STEP,
    SCHEMA_VERSION,
    DominantColor,
    ScreenshotPaletteReport,
    _coerce_payload,
    _decode_png_and_count,
    build_capture_script,
    capture_screenshot_palette,
    empty_report,
    filter_against_declared,
    hex_to_rgb,
    palette_completeness_warning,
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


def test_default_timeout_lifted_for_a1_1_navigate_path() -> None:
    """The A1.1 raster path needs networkidle headroom; 15s replaces 8s."""
    # The screenshot pass now waits for networkidle then takes a real PNG
    # screenshot and decodes via Pillow. The previous 8000ms budget was
    # tuned for set_content+DOM-walk; the navigate path needs more room.
    assert DEFAULT_TIMEOUT_MS >= 15_000


# ---------------------------------------------------------------------------
# A1.1 Part 1: Pillow-decoded raster path
# ---------------------------------------------------------------------------


def _make_solid_png(width: int, height: int, rgba: tuple[int, int, int, int]) -> bytes:
    """Build a flat-color RGBA PNG for raster-path tests.

    Pillow is imported lazily so the test module still loads on runtimes
    without the [browser] extra installed; the test itself is skipped in
    that case via the pytest.importorskip call below.
    """
    pil_image = pytest.importorskip("PIL.Image")
    from io import BytesIO

    image = pil_image.new("RGBA", (width, height), rgba)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_decode_png_and_count_buckets_solid_brand_color() -> None:
    """A flat coral PNG bucketed by Pillow surfaces coral as the dominant color.

    This is the unit-test seam for the A1.1 raster path: the helper
    receives PNG bytes (what `page.screenshot()` returns), decodes via
    Pillow, quantizes to QUANTIZATION_STEP, and produces a report whose
    top color matches the input within the quantization step.
    """
    pil_image = pytest.importorskip("PIL.Image")
    # Coral #f8485e is the ENC brand accent the A1 bug surfaces. Quantized
    # to step=16 this collapses to (240, 64, 80) = #f04050; the test asserts
    # the rounded bucket, not the original input.
    png_bytes = _make_solid_png(64, 64, (248, 72, 94, 255))
    report = _decode_png_and_count(png_bytes, pil_image)
    assert report["status"] == "ok"
    assert report["viewport"] == (64, 64)
    assert report["total_pixels"] == 64 * 64
    assert len(report["colors"]) >= 1
    top = report["colors"][0]
    # The bucket key floors each channel to a multiple of QUANTIZATION_STEP.
    expected_r = (248 // QUANTIZATION_STEP) * QUANTIZATION_STEP
    expected_g = (72 // QUANTIZATION_STEP) * QUANTIZATION_STEP
    expected_b = (94 // QUANTIZATION_STEP) * QUANTIZATION_STEP
    assert top["rgb"] == (expected_r, expected_g, expected_b)
    # The fraction must be 1.0 for a flat-color image (all pixels one bucket).
    assert top["pixel_fraction"] == pytest.approx(1.0)
    assert top["pixel_count"] == 64 * 64


def test_decode_png_skips_fully_transparent_pixels() -> None:
    """Alpha < 13 (≈5%) is treated as overlay noise and excluded from counts.

    Mirrors the legacy JS path's alpha-cutoff so transparent layers do
    not skew the palette toward the background underneath.
    """
    pil_image = pytest.importorskip("PIL.Image")
    png_bytes = _make_solid_png(32, 32, (248, 72, 94, 0))  # fully transparent
    report = _decode_png_and_count(png_bytes, pil_image)
    assert report["status"] == "ok"
    # All pixels skipped: no color exceeds the dominance floor.
    assert report["colors"] == []


def test_decode_png_handles_bad_bytes_gracefully() -> None:
    """A corrupt PNG payload surfaces as status=error without raising."""
    pil_image = pytest.importorskip("PIL.Image")
    report = _decode_png_and_count(b"\x89PNG\r\n\x1a\nnot really a png", pil_image)
    assert report["status"] == "error"
    assert "png decode failure" in (report["error"] or "")


# ---------------------------------------------------------------------------
# A1.1 Part 2: palette_completeness_warning helper
# ---------------------------------------------------------------------------


def test_palette_completeness_warning_none_when_report_unavailable() -> None:
    """An unavailable / errored report carries no warning signal."""
    assert palette_completeness_warning(empty_report("unavailable"), []) is None
    assert palette_completeness_warning(empty_report("error", "boom"), []) is None
    assert palette_completeness_warning(empty_report("skipped"), []) is None


def test_palette_completeness_warning_none_when_palette_complete() -> None:
    """All rendered-dominant colors covered by declared: no warning."""
    colors: list[DominantColor] = [
        DominantColor(hex="#ffffff", rgb=(255, 255, 255), pixel_count=900, pixel_fraction=0.9),
        DominantColor(hex="#f8485e", rgb=(248, 72, 94), pixel_count=100, pixel_fraction=0.1),
    ]
    report = _make_report(colors)
    declared = ["#ffffff", "#f8485e"]
    assert palette_completeness_warning(report, declared) is None


def test_palette_completeness_warning_lists_only_missed_colors() -> None:
    """The warning carries the EXACT hex strings the declared pipeline missed.

    This is the field the ENC fixture's
    ``must_emit_palette_completeness_warning`` clause asserts against in
    the live-extraction harness.
    """
    colors: list[DominantColor] = [
        DominantColor(hex="#ffffff", rgb=(255, 255, 255), pixel_count=900, pixel_fraction=0.9),
        DominantColor(hex="#f8485e", rgb=(248, 72, 94), pixel_count=50, pixel_fraction=0.05),
        DominantColor(hex="#592a8a", rgb=(89, 42, 138), pixel_count=30, pixel_fraction=0.03),
    ]
    declared = ["#ffffff", "#007cba"]  # stock Gutenberg default
    warning = palette_completeness_warning(_make_report(colors), declared)
    assert warning is not None
    assert "#f8485e" in warning
    assert "#592a8a" in warning
    assert "#ffffff" not in warning
    # Order is part of the contract: pixel_count desc => coral before purple.
    assert warning.index("#f8485e") < warning.index("#592a8a")


def test_palette_completeness_warning_treats_none_declared_as_empty() -> None:
    """A None declared list is treated as empty (no false negatives)."""
    colors: list[DominantColor] = [
        DominantColor(hex="#f8485e", rgb=(248, 72, 94), pixel_count=50, pixel_fraction=0.05),
    ]
    warning = palette_completeness_warning(_make_report(colors), None)
    assert warning == ["#f8485e"]


# ---------------------------------------------------------------------------
# A1.1 LIVE integration: the test that would have caught the regression
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("RESEMBLIO_RUN_REAL_BROWSER") != "1",
    reason="Live-browser test; opt in with RESEMBLIO_RUN_REAL_BROWSER=1",
)
def test_live_encexplorer_surfaces_brand_colors_via_real_navigate() -> None:
    """The canonical ENC regression: real navigate + raster MUST surface brand colors.

    Without the A1.1 fix this assertion fails because ``set_content``
    loaded the HTML with about:blank base URL, every linked asset
    failed, and the DOM-walk pass collapsed to UA defaults. With the
    fix, page.goto + networkidle + PNG raster + Pillow decode produces
    the actual rendered palette where #f8485e or #592a8a appear as
    dominant buckets.
    """
    pytest.importorskip("playwright.sync_api")
    pytest.importorskip("PIL.Image")
    report = capture_screenshot_palette(url="https://encexplorer.com")
    # If Chromium isn't installed the helper returns unavailable; treat
    # that as a skip rather than a fail so the test stays useful in
    # environments without the binary.
    if report["status"] == "unavailable":
        pytest.skip(f"playwright/chromium unavailable: {report['error']}")
    assert report["status"] == "ok", report["error"]
    hex_set = {c["hex"] for c in report["colors"]}
    # The bucket center for coral (248, 72, 94) at step=16 is #f04050;
    # for purple (89, 42, 138) is #582888. Accept either the bucket
    # center or anything within COLOR_SIMILARITY_THRESHOLD of the brand.
    coral_bucket_neighbors = {"#f04050", "#f8485e", "#f04060"}
    purple_bucket_neighbors = {"#582888", "#582a8a", "#502888", "#592a8a"}
    assert hex_set & (coral_bucket_neighbors | purple_bucket_neighbors), (
        f"Neither coral nor purple bucket surfaced in screenshot palette; "
        f"got {hex_set}. This is the A1 regression: real navigate failed "
        f"to load brand assets."
    )

"""Meta-tests for tests/ground_truth_harness.py.

The harness loads, validates, and asserts against real-URL ground-truth
fixtures. The harness ITSELF must be testable — otherwise a bug in the
harness produces silent false-PASS on every fixture (the failure mode
the 2026-05-31 visual-fidelity-check v16 lock was authored to prevent
on UI work; the same principle applies to extraction work).

This module exercises:

- ``hex_to_rgb`` and ``rgb_distance`` numerics
- ``color_present_in_palette`` tolerance behavior
- ``font_matches`` fuzzy vs. exact modes
- ``load_fixture`` shape validation (rejects malformed YAML)
- ``run_assertions`` against synthetic fixtures (one known-good +
  one known-bad) so harness behavior under both verdicts is asserted

The synthetic fixtures live at
``tests/fixtures/ground_truth/_meta/`` and are excluded from the
production sweep by virtue of being under a sub-directory.

Source dispatch: Jim Builder dispatch 2026-06-04 (R3-downstream cycle #1).
Throwaway: NO. Quality floor applies.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.ground_truth_harness import (
    DEFAULT_COLOR_DISTANCE_MAX,
    FIXTURE_SCHEMA_VERSION,
    FixtureShapeError,
    SkipFixture,
    color_present_in_palette,
    discover_fixtures,
    font_matches,
    hex_to_rgb,
    load_fixture,
    resolve_payload_for_snapshot_mode,
    rgb_distance,
    run_assertions,
)

META_DIR = Path(__file__).parent / "fixtures" / "ground_truth" / "_meta"
GOOD_FIXTURE_PATH = META_DIR / "good_fixture_for_meta_test.yaml"
BAD_FIXTURE_PATH = META_DIR / "bad_fixture_for_meta_test.yaml"


# ---------------------------------------------------------------------------
# Numeric primitives
# ---------------------------------------------------------------------------


def test_hex_to_rgb_handles_short_form() -> None:
    """#f06 expands to (#ff0066) RGB (255, 0, 102)."""
    assert hex_to_rgb("#f06") == (0xFF, 0x00, 0x66)


def test_hex_to_rgb_handles_long_form() -> None:
    """#ff0066 parses to (255, 0, 102)."""
    assert hex_to_rgb("#ff0066") == (0xFF, 0x00, 0x66)


def test_hex_to_rgb_strips_alpha() -> None:
    """#ff006699 with alpha drops alpha and keeps RGB."""
    assert hex_to_rgb("#ff006699") == (0xFF, 0x00, 0x66)


def test_rgb_distance_zero_for_identical_colors() -> None:
    """Identity case: distance(x, x) == 0."""
    assert rgb_distance((0, 0, 0), (0, 0, 0)) == 0.0


def test_rgb_distance_known_pair() -> None:
    """White vs. black distance is sqrt(3 * 255^2) ~= 441.67."""
    distance = rgb_distance((255, 255, 255), (0, 0, 0))
    assert 441.0 < distance < 442.0


# ---------------------------------------------------------------------------
# color_present_in_palette
# ---------------------------------------------------------------------------


def test_color_present_within_tolerance() -> None:
    """Within tolerance returns the matched palette entry."""
    match = color_present_in_palette(
        "#ff0066", ["#ffffff", "#ff0064"], tolerance=8.0
    )
    assert match == "#ff0064"


def test_color_absent_outside_tolerance() -> None:
    """Outside tolerance returns None."""
    match = color_present_in_palette(
        "#ff0066", ["#ffffff", "#222222"], tolerance=8.0
    )
    assert match is None


def test_color_present_ignores_invalid_palette_entries() -> None:
    """Garbage palette entries don't crash; they're skipped."""
    match = color_present_in_palette(
        "#ff0066", ["not-a-hex", "#ff0066"], tolerance=8.0
    )
    assert match == "#ff0066"


# ---------------------------------------------------------------------------
# font_matches
# ---------------------------------------------------------------------------


def test_font_fuzzy_match_first_cascade_segment() -> None:
    """Fuzzy mode accepts head-of-cascade containing the expected name."""
    assert font_matches("Inter, -apple-system, sans-serif", "Inter", mode="fuzzy")


def test_font_fuzzy_match_case_insensitive() -> None:
    """Fuzzy mode is case-insensitive."""
    assert font_matches("inter, sans-serif", "INTER", mode="fuzzy")


def test_font_fuzzy_match_rejects_unrelated_family() -> None:
    """Helvetica is not Inter; fuzzy rejects."""
    assert not font_matches("Helvetica, Arial, sans-serif", "Inter", mode="fuzzy")


def test_font_exact_match_requires_whole_string() -> None:
    """Exact mode rejects fallback fragments after the head."""
    assert not font_matches("Inter, sans-serif", "Inter", mode="exact")
    assert font_matches("Inter", "Inter", mode="exact")


# ---------------------------------------------------------------------------
# load_fixture shape validation
# ---------------------------------------------------------------------------


def test_load_fixture_accepts_good_meta_fixture() -> None:
    """The good meta fixture parses cleanly."""
    fx = load_fixture(GOOD_FIXTURE_PATH)
    assert fx["brand_slug"] == "meta_good"
    assert fx["schema_version"] == FIXTURE_SCHEMA_VERSION


def test_load_fixture_rejects_wrong_schema_version(tmp_path: Path) -> None:
    """Wrong schema_version raises FixtureShapeError with the slug + actual."""
    bad = tmp_path / "bad_schema.yaml"
    bad.write_text(
        "schema_version: not_the_right_one\n"
        "brand_slug: shape_test\n"
        "source_url: https://example.com/\n"
        "fixture_authored_at: '2026-06-04'\n"
        "fixture_author: meta-test\n"
        "ground_truth:\n"
        "  color:\n"
        "    bg: '#ffffff'\n",
        encoding="utf-8",
    )
    with pytest.raises(FixtureShapeError, match="schema_version"):
        load_fixture(bad)


def test_load_fixture_rejects_invalid_hex(tmp_path: Path) -> None:
    """Bad hex in ground_truth.color raises FixtureShapeError."""
    bad = tmp_path / "bad_hex.yaml"
    bad.write_text(
        "schema_version: resemblio_ground_truth_v1\n"
        "brand_slug: shape_test\n"
        "source_url: https://example.com/\n"
        "fixture_authored_at: '2026-06-04'\n"
        "fixture_author: meta-test\n"
        "ground_truth:\n"
        "  color:\n"
        "    bg: notahex\n",
        encoding="utf-8",
    )
    with pytest.raises(FixtureShapeError, match="not a valid hex"):
        load_fixture(bad)


def test_load_fixture_rejects_must_include_referencing_undeclared_slot(
    tmp_path: Path,
) -> None:
    """must_include_colors slot that isn't in ground_truth.color is rejected."""
    bad = tmp_path / "bad_slot_ref.yaml"
    bad.write_text(
        "schema_version: resemblio_ground_truth_v1\n"
        "brand_slug: shape_test\n"
        "source_url: https://example.com/\n"
        "fixture_authored_at: '2026-06-04'\n"
        "fixture_author: meta-test\n"
        "ground_truth:\n"
        "  color:\n"
        "    bg: '#ffffff'\n"
        "expected_extraction_behavior:\n"
        "  must_include_colors:\n"
        "    - accent\n",
        encoding="utf-8",
    )
    with pytest.raises(FixtureShapeError, match="not declared"):
        load_fixture(bad)


def test_load_fixture_rejects_bad_font_mode(tmp_path: Path) -> None:
    """tolerance.font_family_match outside the allowed set is rejected."""
    bad = tmp_path / "bad_font_mode.yaml"
    bad.write_text(
        "schema_version: resemblio_ground_truth_v1\n"
        "brand_slug: shape_test\n"
        "source_url: https://example.com/\n"
        "fixture_authored_at: '2026-06-04'\n"
        "fixture_author: meta-test\n"
        "ground_truth:\n"
        "  color:\n"
        "    bg: '#ffffff'\n"
        "tolerance:\n"
        "  font_family_match: vibes\n",
        encoding="utf-8",
    )
    with pytest.raises(FixtureShapeError, match="font_family_match"):
        load_fixture(bad)


# ---------------------------------------------------------------------------
# discover_fixtures excludes _meta
# ---------------------------------------------------------------------------


def test_discover_fixtures_excludes_meta_dir() -> None:
    """discover_fixtures returns top-level YAMLs only; _meta is hidden."""
    root = GOOD_FIXTURE_PATH.parent.parent  # the ground_truth/ dir
    paths = discover_fixtures(root)
    names = {p.name for p in paths}
    assert "good_fixture_for_meta_test.yaml" not in names
    assert "bad_fixture_for_meta_test.yaml" not in names


# ---------------------------------------------------------------------------
# resolve_payload_for_snapshot_mode
# ---------------------------------------------------------------------------


def test_resolve_payload_returns_snapshot_when_present() -> None:
    """Good meta fixture carries a snapshot; resolver returns it."""
    fx = load_fixture(GOOD_FIXTURE_PATH)
    payload = resolve_payload_for_snapshot_mode(fx)
    assert "color" in payload
    assert payload["color"]["accent"] == "#ff0064"


def test_resolve_payload_skips_when_live_only(tmp_path: Path) -> None:
    """Fixture flagged live_extraction_only raises SkipFixture."""
    fixture = tmp_path / "live_only.yaml"
    fixture.write_text(
        "schema_version: resemblio_ground_truth_v1\n"
        "brand_slug: live_only_test\n"
        "source_url: https://example.com/\n"
        "fixture_authored_at: '2026-06-04'\n"
        "fixture_author: meta-test\n"
        "live_extraction_only: true\n"
        "ground_truth:\n"
        "  color:\n"
        "    bg: '#ffffff'\n",
        encoding="utf-8",
    )
    fx = load_fixture(fixture)
    with pytest.raises(SkipFixture, match="live_extraction_only"):
        resolve_payload_for_snapshot_mode(fx)


def test_resolve_payload_skips_when_snapshot_absent(tmp_path: Path) -> None:
    """Fixture without snapshot AND without live_only flag still skips."""
    fixture = tmp_path / "no_snapshot.yaml"
    fixture.write_text(
        "schema_version: resemblio_ground_truth_v1\n"
        "brand_slug: no_snapshot_test\n"
        "source_url: https://example.com/\n"
        "fixture_authored_at: '2026-06-04'\n"
        "fixture_author: meta-test\n"
        "ground_truth:\n"
        "  color:\n"
        "    bg: '#ffffff'\n",
        encoding="utf-8",
    )
    fx = load_fixture(fixture)
    with pytest.raises(SkipFixture, match="extracted_payload_snapshot missing"):
        resolve_payload_for_snapshot_mode(fx)


# ---------------------------------------------------------------------------
# run_assertions verdict behavior (the canary)
# ---------------------------------------------------------------------------


def test_run_assertions_passes_on_good_meta_fixture() -> None:
    """Known-good fixture + snapshot produces zero failures."""
    fx = load_fixture(GOOD_FIXTURE_PATH)
    payload = resolve_payload_for_snapshot_mode(fx)
    result = run_assertions(fx, payload)
    assert result.passed, (
        f"good meta fixture must produce zero failures; got {result.failures!r}"
    )


def test_run_assertions_catches_every_defect_class() -> None:
    """Known-bad fixture trips ALL four failure classes.

    The bad fixture is hand-authored to violate every assertion class
    simultaneously. The harness MUST return all of them (vs. fail-fast).
    Catches the silent-coverage failure mode where a regression in the
    assertion runner hides defects behind the first failure.
    """
    fx = load_fixture(BAD_FIXTURE_PATH)
    payload = resolve_payload_for_snapshot_mode(fx)
    result = run_assertions(fx, payload)
    assert not result.passed
    kinds = {f.kind for f in result.failures}
    # Every failure class the harness knows about must be represented.
    assert "color_missing" in kinds, kinds
    assert "color_forbidden_present" in kinds, kinds
    assert "palette_warning_mismatch" in kinds, kinds
    assert "font_mismatch" in kinds, kinds


def test_run_assertions_with_empty_behavior_passes_trivially() -> None:
    """Fixture without expected_extraction_behavior produces no failures.

    Authoring-in-progress case: a fixture may carry ground_truth +
    tolerance but no behavior block yet. The harness must not error;
    it must report zero failures so the fixture is benign in CI.
    """
    fx = load_fixture(GOOD_FIXTURE_PATH)
    fx_no_behavior = {k: v for k, v in fx.items() if k != "expected_extraction_behavior"}
    payload = resolve_payload_for_snapshot_mode(fx)  # type: ignore[arg-type]
    result = run_assertions(fx_no_behavior, payload)  # type: ignore[arg-type]
    assert result.passed


def test_run_assertions_default_tolerance_constant_holds() -> None:
    """Default tolerance constant has not drifted from the documented value.

    Sentinel: the harness README and screenshot_palette both pin this
    at 8.0. If a refactor moves the constant, both docs need to follow;
    this test fires first.
    """
    assert DEFAULT_COLOR_DISTANCE_MAX == 8.0

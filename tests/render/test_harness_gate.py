"""Phase 0.C RED test: visual harness gate logic.

Tests for harness_gate.py, the Phase 0/1+ gate that compares before/after
screenshot captures for all brands in the capture plan, not a hardcoded
Phase 5.2 brand subset.

All tests here are pure-data (no network, no browser, no live URL). Synthetic
1x1 or small Pillow images stand in as reference and candidate captures.

Decision reference: D16 (pixel proof is the readiness definition) in
projects/OptSus Team/missions/resemblio-library-public-view-readiness-tdd-plan-v5.md
"""
from __future__ import annotations

import json
import pathlib

import pytest

from tests.render.harness_gate import (  # noqa: E402 - RED until harness_gate.py exists
    HarnessGateResult,
    build_reference_index,
    evaluate_harness_gate,
    render_gate_manifest,
)
from tests.render.capture_plan import (
    CaptureTarget,
    Surface,
    build_capture_plan,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_URL = "https://resemblio.com"
_BRANDS = ["apple", "stripe", "gwern"]


def _make_png(tmp_path: pathlib.Path, name: str, color: tuple[int, int, int]) -> pathlib.Path:
    """Create a small solid-color PNG at tmp_path/name."""
    pil = pytest.importorskip("PIL.Image")
    img = pil.new("RGB", (16, 16), color=color)
    p = tmp_path / name
    img.save(p)
    return p


# ---------------------------------------------------------------------------
# build_reference_index
# ---------------------------------------------------------------------------


def test_reference_index_finds_files_matching_plan(tmp_path: pathlib.Path) -> None:
    """build_reference_index locates PNGs matching capture plan filenames."""
    plan = build_capture_plan(["stripe"], base_url=_BASE_URL)
    # Populate only the landing/desktop file.
    landing_desktop = next(
        t for t in plan
        if t.surface == Surface.LANDING and t.viewport_label == "desktop"
    )
    ref_dir = tmp_path / "refs"
    ref_dir.mkdir()
    _make_png(ref_dir, landing_desktop.output_filename, (200, 100, 50))

    index = build_reference_index(plan=plan, reference_dir=ref_dir)
    assert landing_desktop.output_filename in index
    assert index[landing_desktop.output_filename].exists()


def test_reference_index_missing_returns_empty_entry(tmp_path: pathlib.Path) -> None:
    """Targets with no reference file are absent from the index.

    The gate uses the index to decide which targets have a reference to
    compare against (skip if absent, compare if present).
    """
    plan = build_capture_plan(["stripe"], base_url=_BASE_URL)
    empty_dir = tmp_path / "refs"
    empty_dir.mkdir()
    index = build_reference_index(plan=plan, reference_dir=empty_dir)
    assert len(index) == 0, "Expected empty index when no reference files exist"


# ---------------------------------------------------------------------------
# evaluate_harness_gate: self-skip when no references
# ---------------------------------------------------------------------------


def test_evaluate_gate_skips_when_no_references(tmp_path: pathlib.Path) -> None:
    """Gate result is SKIP when no reference images are present.

    The no-reference SKIP semantics preserve the self-skip-safe contract:
    running the suite offline (before any capture) never produces a FAIL.
    After Phase 0.D populates references, the gate compares instead.
    """
    plan = build_capture_plan(["stripe"], base_url=_BASE_URL)
    ref_dir = tmp_path / "refs"
    ref_dir.mkdir()
    candidate_dir = tmp_path / "candidates"
    candidate_dir.mkdir()

    result = evaluate_harness_gate(
        plan=plan,
        reference_dir=ref_dir,
        candidate_dir=candidate_dir,
    )
    assert result.aggregate == "SKIP", (
        f"Expected SKIP when no references; got {result.aggregate!r}"
    )


# ---------------------------------------------------------------------------
# evaluate_harness_gate: PASS for identical images
# ---------------------------------------------------------------------------


def test_evaluate_gate_passes_for_identical_images(tmp_path: pathlib.Path) -> None:
    """Gate PASSES when candidate is a byte-identical copy of the reference."""
    pil = pytest.importorskip("PIL.Image")

    plan = build_capture_plan(["stripe"], base_url=_BASE_URL)
    ref_dir = tmp_path / "refs"
    candidate_dir = tmp_path / "candidates"
    ref_dir.mkdir()
    candidate_dir.mkdir()

    # Populate reference and candidate with identical 16x16 images.
    color = (100, 150, 200)
    for target in plan:
        _make_png(ref_dir, target.output_filename, color)
        _make_png(candidate_dir, target.output_filename, color)

    result = evaluate_harness_gate(
        plan=plan,
        reference_dir=ref_dir,
        candidate_dir=candidate_dir,
    )
    assert result.aggregate == "PASS", (
        f"Expected PASS for identical images; got {result.aggregate!r}. "
        f"Failing targets: {[e for e in result.entries if e.status == 'FAIL']}"
    )


# ---------------------------------------------------------------------------
# evaluate_harness_gate: FAIL for highly divergent images
# ---------------------------------------------------------------------------


def test_evaluate_gate_fails_for_divergent_images(tmp_path: pathlib.Path) -> None:
    """Gate FAILS when candidate is a completely different color from reference."""
    pil = pytest.importorskip("PIL.Image")

    plan = build_capture_plan(["stripe"], base_url=_BASE_URL)
    ref_dir = tmp_path / "refs"
    candidate_dir = tmp_path / "candidates"
    ref_dir.mkdir()
    candidate_dir.mkdir()

    # Reference is white; candidate is black. SSIM will be near 0.
    for target in plan:
        _make_png(ref_dir, target.output_filename, (255, 255, 255))
        _make_png(candidate_dir, target.output_filename, (0, 0, 0))

    result = evaluate_harness_gate(
        plan=plan,
        reference_dir=ref_dir,
        candidate_dir=candidate_dir,
    )
    assert result.aggregate == "FAIL", (
        f"Expected FAIL for fully divergent (white vs black) images; "
        f"got {result.aggregate!r}"
    )


# ---------------------------------------------------------------------------
# evaluate_harness_gate: missing candidate is SKIP per-target
# ---------------------------------------------------------------------------


def test_evaluate_gate_skips_missing_candidate(tmp_path: pathlib.Path) -> None:
    """A target whose candidate file is absent is SKIPped, not FAILed."""
    pil = pytest.importorskip("PIL.Image")

    # One target only (1 brand x landing x desktop).
    plan = build_capture_plan(["stripe"], base_url=_BASE_URL)
    ref_dir = tmp_path / "refs"
    candidate_dir = tmp_path / "candidates"
    ref_dir.mkdir()
    candidate_dir.mkdir()

    # Populate all references but NO candidates.
    for target in plan:
        _make_png(ref_dir, target.output_filename, (100, 100, 100))

    result = evaluate_harness_gate(
        plan=plan,
        reference_dir=ref_dir,
        candidate_dir=candidate_dir,
    )
    # All targets have a reference but no candidate -> all SKIP -> aggregate SKIP.
    assert result.aggregate == "SKIP", (
        f"Expected SKIP when candidates absent; got {result.aggregate!r}"
    )
    for entry in result.entries:
        assert entry.status == "SKIP", (
            f"Expected SKIP per target when candidate absent; "
            f"got {entry.status!r} for {entry.filename!r}"
        )


# ---------------------------------------------------------------------------
# HarnessGateResult schema
# ---------------------------------------------------------------------------


def test_harness_gate_result_has_required_fields(tmp_path: pathlib.Path) -> None:
    """HarnessGateResult has schema_version, aggregate, entries, summary fields."""
    plan = build_capture_plan(["stripe"], base_url=_BASE_URL)
    ref_dir = tmp_path / "refs"
    ref_dir.mkdir()
    candidate_dir = tmp_path / "candidates"
    candidate_dir.mkdir()

    result = evaluate_harness_gate(
        plan=plan,
        reference_dir=ref_dir,
        candidate_dir=candidate_dir,
    )
    assert hasattr(result, "schema_version")
    assert hasattr(result, "aggregate")
    assert hasattr(result, "entries")
    assert hasattr(result, "pass_count")
    assert hasattr(result, "fail_count")
    assert hasattr(result, "skip_count")
    assert result.schema_version == "harness_gate_result_v1"


# ---------------------------------------------------------------------------
# render_gate_manifest: JSON output
# ---------------------------------------------------------------------------


def test_render_gate_manifest_produces_valid_json(tmp_path: pathlib.Path) -> None:
    """render_gate_manifest returns a JSON-serialisable string."""
    plan = build_capture_plan(["stripe"], base_url=_BASE_URL)
    ref_dir = tmp_path / "refs"
    ref_dir.mkdir()
    candidate_dir = tmp_path / "candidates"
    candidate_dir.mkdir()

    result = evaluate_harness_gate(
        plan=plan,
        reference_dir=ref_dir,
        candidate_dir=candidate_dir,
    )
    manifest_str = render_gate_manifest(result)
    parsed = json.loads(manifest_str)
    assert parsed.get("schema_version") == "harness_gate_result_v1"
    assert "aggregate" in parsed
    assert "entries" in parsed

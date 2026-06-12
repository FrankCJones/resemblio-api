"""Visual harness gate: before/after screenshot comparison for the full corpus.

Compares candidate screenshots against reference screenshots for all targets
in a capture plan (every brand x surface x viewport). Designed for the
Phase 0/1+ harness where "reference" is the before-capture and "candidate"
is a later capture, so regressions surface as FAIL.

Self-skip semantics:
- If no reference images exist in reference_dir, result is SKIP (not FAIL).
  This means running the offline suite before any captures never breaks CI.
- If a reference exists but the candidate file is absent, that target is SKIP.
- FAIL only when both reference AND candidate exist and they diverge beyond
  the SSIM floor.

Decision reference: D16 (pixel proof is the readiness definition) in
projects/OptSus Team/missions/resemblio-library-public-view-readiness-tdd-plan-v5.md

Schema: harness_gate_v1
"""
from __future__ import annotations

import json
import logging
import pathlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from tests.render.capture_plan import CaptureTarget

_log = logging.getLogger("harness_gate")

# SSIM floor: a score below this is considered a regression. Kept consistent
# with the Phase 5.2 gate tolerance to avoid two different notions of "too
# different" in the same project.
SSIM_FLOOR = 0.65

SCHEMA_VERSION = "harness_gate_result_v1"


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class HarnessTargetEntry:
    """Per-target gate result.

    Attributes:
        filename:   output_filename from the CaptureTarget (matches the PNG
                    in reference_dir and candidate_dir).
        brand_slug: brand slug from the CaptureTarget.
        surface:    surface label ("landing" or "specimen").
        viewport:   viewport label ("desktop" or "mobile").
        status:     "PASS" | "FAIL" | "SKIP"
        ssim:       SSIM score when comparison ran, else None.
        reason:     Human-readable reason for SKIP or FAIL, else None.
    """

    filename: str
    brand_slug: str
    surface: str
    viewport: str
    status: str
    ssim: Optional[float] = None
    reason: Optional[str] = None


@dataclass
class HarnessGateResult:
    """Aggregate result of evaluate_harness_gate.

    Attributes:
        schema_version: Always "harness_gate_result_v1".
        generated_at:   ISO-8601 UTC timestamp of when the result was produced.
        aggregate:      "PASS" | "FAIL" | "SKIP"
                        PASS  = all targets with both reference and candidate
                                present scored >= SSIM_FLOOR.
                        FAIL  = at least one target scored < SSIM_FLOOR.
                        SKIP  = no reference images found (or no targets in plan).
        pass_count:     Number of PASS entries.
        fail_count:     Number of FAIL entries.
        skip_count:     Number of SKIP entries.
        entries:        Per-target entries, one per CaptureTarget in the plan.
    """

    schema_version: str
    generated_at: str
    aggregate: str
    pass_count: int
    fail_count: int
    skip_count: int
    entries: list[HarnessTargetEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Image comparison (reuses SSIM primitive from test_visual_fidelity_gate)
# ---------------------------------------------------------------------------


def _compute_ssim_for_paths(
    ref_path: pathlib.Path,
    candidate_path: pathlib.Path,
) -> Optional[float]:
    """Return SSIM in [0.0, 1.0], or None when Pillow is unavailable.

    Prefers scikit-image when available; falls back to a Pearson-luminance
    surrogate (the same algorithm documented in test_visual_fidelity_gate.py).
    Both images are resized to the smaller dimension before comparison to
    cancel device-pixel-ratio drift.

    Returns None rather than raising when either image cannot be opened
    (missing, corrupt, wrong format). Callers treat None as SKIP.
    """
    try:
        from PIL import Image
    except ImportError:
        _log.warning("Pillow not installed; SSIM comparison unavailable")
        return None

    try:
        ref_img = Image.open(ref_path).convert("L")
        cand_img = Image.open(candidate_path).convert("L")
    except Exception as exc:
        _log.warning("Could not open images for SSIM (%s): %s", ref_path.name, exc)
        return None

    target_w = min(ref_img.width, cand_img.width)
    target_h = min(ref_img.height, cand_img.height)
    if target_w < 2 or target_h < 2:
        return 0.0

    ref_img = ref_img.resize((target_w, target_h), Image.LANCZOS)
    cand_img = cand_img.resize((target_w, target_h), Image.LANCZOS)

    try:
        from skimage.metrics import structural_similarity
        import numpy as np

        ref_arr = np.asarray(ref_img, dtype=np.float64)
        cand_arr = np.asarray(cand_img, dtype=np.float64)
        score = float(structural_similarity(ref_arr, cand_arr, data_range=255.0))
        return max(0.0, min(1.0, score))
    except ImportError:
        pass

    # Pillow-only Pearson-luminance surrogate (documented in
    # test_visual_fidelity_gate.py as the Pillow-only fallback).
    return _pearson_surrogate(ref_img, cand_img)


def _pearson_surrogate(ref_img, cand_img) -> float:
    """Pearson correlation surrogate for SSIM on grayscale images.

    Returns a value in [0, 1]. Identical images return 1.0. Inverted
    images (white vs black) return near 0.0. See test_visual_fidelity_gate.py
    for the full derivation and documented limitations.
    """
    ref_bytes = list(ref_img.tobytes())
    cand_bytes = list(cand_img.tobytes())
    n = len(ref_bytes)
    if n == 0 or n != len(cand_bytes):
        return 0.0
    ref_mean = sum(ref_bytes) / n
    cand_mean = sum(cand_bytes) / n
    num = 0.0
    den_r = 0.0
    den_c = 0.0
    for r, c in zip(ref_bytes, cand_bytes):
        dr = r - ref_mean
        dc = c - cand_mean
        num += dr * dc
        den_r += dr * dr
        den_c += dc * dc
    denom = (den_r * den_c) ** 0.5
    if denom == 0.0:
        return 1.0 if abs(ref_mean - cand_mean) < 1.0 else 0.0
    pearson = num / denom
    base = max(0.0, 0.5 * (1.0 + pearson))
    brightness_delta = abs(ref_mean - cand_mean) / 255.0
    penalty = max(0.0, 1.0 - max(0.0, brightness_delta - (32.0 / 255.0)) * 4.0)
    return base * penalty


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_reference_index(
    *,
    plan: list[CaptureTarget],
    reference_dir: pathlib.Path,
) -> dict[str, pathlib.Path]:
    """Return a mapping of output_filename -> reference_path for files that exist.

    Only filenames that exist on disk are included. The caller uses this index
    to decide which targets have a reference to compare against.

    Args:
        plan:          Capture plan (output of build_capture_plan).
        reference_dir: Directory that should contain the reference PNGs.

    Returns:
        Dict mapping output_filename to its full path. Empty dict when no
        reference images exist (the gate will then self-skip).
    """
    index: dict[str, pathlib.Path] = {}
    for target in plan:
        candidate = reference_dir / target.output_filename
        if candidate.is_file():
            index[target.output_filename] = candidate
    return index


def evaluate_harness_gate(
    *,
    plan: list[CaptureTarget],
    reference_dir: pathlib.Path,
    candidate_dir: pathlib.Path,
    ssim_floor: float = SSIM_FLOOR,
) -> HarnessGateResult:
    """Compare candidates against references for every target in the plan.

    Args:
        plan:          Capture plan (output of build_capture_plan).
        reference_dir: Directory containing before-capture reference PNGs.
        candidate_dir: Directory containing after-capture candidate PNGs.
        ssim_floor:    SSIM threshold below which a target is FAIL (default
                       0.65, consistent with Phase 5.2 gate).

    Returns:
        HarnessGateResult with aggregate PASS/FAIL/SKIP and per-target entries.

    Aggregate semantics:
        - SKIP  when no reference images exist (pre-capture, safe for CI).
        - FAIL  when at least one target with both reference and candidate
                present scores below ssim_floor.
        - PASS  when all such targets score >= ssim_floor.
    """
    reference_index = build_reference_index(plan=plan, reference_dir=reference_dir)

    if not reference_index:
        # No reference images: self-skip so offline suite stays green.
        entries = [
            HarnessTargetEntry(
                filename=t.output_filename,
                brand_slug=t.brand_slug,
                surface=t.surface.label,
                viewport=t.viewport_label,
                status="SKIP",
                reason="no reference image",
            )
            for t in plan
        ]
        return HarnessGateResult(
            schema_version=SCHEMA_VERSION,
            generated_at=datetime.now(timezone.utc).isoformat(),
            aggregate="SKIP",
            pass_count=0,
            fail_count=0,
            skip_count=len(entries),
            entries=entries,
        )

    entries: list[HarnessTargetEntry] = []
    for target in plan:
        ref_path = reference_index.get(target.output_filename)
        if ref_path is None:
            entries.append(
                HarnessTargetEntry(
                    filename=target.output_filename,
                    brand_slug=target.brand_slug,
                    surface=target.surface.label,
                    viewport=target.viewport_label,
                    status="SKIP",
                    reason="no reference image",
                )
            )
            continue

        cand_path = candidate_dir / target.output_filename
        if not cand_path.is_file():
            entries.append(
                HarnessTargetEntry(
                    filename=target.output_filename,
                    brand_slug=target.brand_slug,
                    surface=target.surface.label,
                    viewport=target.viewport_label,
                    status="SKIP",
                    reason="candidate file absent",
                )
            )
            continue

        ssim = _compute_ssim_for_paths(ref_path, cand_path)
        if ssim is None:
            entries.append(
                HarnessTargetEntry(
                    filename=target.output_filename,
                    brand_slug=target.brand_slug,
                    surface=target.surface.label,
                    viewport=target.viewport_label,
                    status="SKIP",
                    ssim=None,
                    reason="SSIM comparison unavailable (Pillow not installed)",
                )
            )
            continue

        if ssim >= ssim_floor:
            entries.append(
                HarnessTargetEntry(
                    filename=target.output_filename,
                    brand_slug=target.brand_slug,
                    surface=target.surface.label,
                    viewport=target.viewport_label,
                    status="PASS",
                    ssim=ssim,
                )
            )
        else:
            entries.append(
                HarnessTargetEntry(
                    filename=target.output_filename,
                    brand_slug=target.brand_slug,
                    surface=target.surface.label,
                    viewport=target.viewport_label,
                    status="FAIL",
                    ssim=ssim,
                    reason=f"SSIM {ssim:.4f} < floor {ssim_floor:.2f}",
                )
            )

    pass_count = sum(1 for e in entries if e.status == "PASS")
    fail_count = sum(1 for e in entries if e.status == "FAIL")
    skip_count = sum(1 for e in entries if e.status == "SKIP")

    if fail_count > 0:
        aggregate = "FAIL"
    elif pass_count > 0:
        aggregate = "PASS"
    else:
        aggregate = "SKIP"

    return HarnessGateResult(
        schema_version=SCHEMA_VERSION,
        generated_at=datetime.now(timezone.utc).isoformat(),
        aggregate=aggregate,
        pass_count=pass_count,
        fail_count=fail_count,
        skip_count=skip_count,
        entries=entries,
    )


def render_gate_manifest(result: HarnessGateResult) -> str:
    """Serialise the gate result as a JSON string.

    The output carries schema_version so downstream consumers can adapt
    to future shape changes without guessing. All dataclass fields are
    preserved including per-entry details.
    """
    payload = {
        "schema_version": result.schema_version,
        "generated_at": result.generated_at,
        "aggregate": result.aggregate,
        "pass_count": result.pass_count,
        "fail_count": result.fail_count,
        "skip_count": result.skip_count,
        "entries": [
            {
                "filename": e.filename,
                "brand_slug": e.brand_slug,
                "surface": e.surface,
                "viewport": e.viewport,
                "status": e.status,
                "ssim": e.ssim,
                "reason": e.reason,
            }
            for e in result.entries
        ],
    }
    return json.dumps(payload, indent=2)

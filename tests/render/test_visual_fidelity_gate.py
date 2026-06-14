"""Phase 5 visual-fidelity gate test.

Compares the live Resemblio library page render at
``https://resemblio.com/library/<brand>/<category>`` against the reference
capture from the real brand site under
``_verification/library-inspirado-correction-20260604/reference_captures/``,
for every (brand, category, viewport) tuple in the reference manifest.

Gate logic (D-5.1 Option A, 2026-06-13, Opus/Jim locked):

  Inspirado-no-copiado rationale: Resemblio renders brand-stripped type
  specimens inspired by real brand design systems, not copies of the real
  brand site. A full-page SSIM comparison between a Resemblio specimen
  (single-template, brand-stripped) and a real brand marketing page
  (photo-rich, multi-section) cannot clear 0.65 by construction - and a
  score that high would *contradict* the product's legal and brand posture.
  The thing the product actually promises is token fidelity: faithful color,
  typography, and spacing extraction, which is exactly what the structural
  dimensions measure. Therefore:

  1. Structural gate (PRIMARY):
       - color_bucket_overlap (top-N quantized buckets)   >= 3 buckets match
       AND dominant_font_family_match (per-spec assertion) is satisfied
                                                          -> PASS
  2. Otherwise                                            -> FAIL

  Raw SSIM is computed and stored in every TupleOutcome as an informational
  field (useful for debugging drift) but does NOT gate the tuple's verdict.

Acceptance: at least 3 distinct (brand, category) combinations pass on ALL
their viewports. Below that the test fails and points at which dimension
drifted (color / font).

Prior gate logic (v2, RZ-A, superseded by D-5.1):
  SSIM >= ssim_floor was the primary gate; structural was the fallback.
  All 5 passes in the 2026-06-13 baseline run were via structural fallback
  (SSIM 0.000-0.539, never clearing 0.65). D-5.1 promotes the actual
  gating dimension to primary and retires the SSIM gate.

Schema versions
---------------
in  : reference_capture_manifest_v1 (reference_captures/manifest.json)
in  : visual_fidelity_tolerance_v1   (tolerance_config.yml)
in  : fidelity_spec_v2               (reference_captures/specs/*.json)
out : library_visual_fidelity_gate_report_v3 (gate_report.json + .md)
      compat_schema_version=v2 written alongside for one cycle so
      prior v2 consumers keep reading until their own bump.
      Deprecation date: re-evaluate after Phase 6 lands (2026-06-13).

HEAD pre-flight (added 2026-06-05 per RZ-A)
-------------------------------------------
Every tuple HEAD-probes the live URL before SSIM. If the final status
code is >= 400 the tuple FAILs with drift=["route_missing"] and SSIM
is NOT computed; the PNG would be a 404 shell and SSIM-scoring it
against a brand reference produces false PASS at low-entropy
viewport-clips (the bug RZ-A closes). See
projects/OptSus Team/architecture-briefs/2026-06-05-resemblio-rz-plan-revision.md
Section "Gate pre-flight spec".

Skip semantics
--------------
The test is pure-skip-safe when the runtime environment cannot reach
the live URL (no Playwright, no network, no resemblio.com auth, etc.).
Skip messages name the missing piece so the caller knows what to fix.

Run command (from workspace root)
---------------------------------
    pytest \\
        "projects/Resemblio/code/api/tests/render/test_visual_fidelity_gate.py" \\
        -v

Optional environment variables
------------------------------
    LIBRARY_BASIC_AUTH       "user:password" for resemblio.com basic auth
    RESEMBLIO_BASE_URL       Override base URL (default https://resemblio.com)
    VISUAL_FIDELITY_GATE_OUT Directory for gate_report.{json,md}; defaults
                             under _verification/.../fidelity_gate_runs/
    WORKSPACE_ROOT           Override workspace root resolution
    FIDELITY_LIVE_SWEEP      "1" opts the live full-corpus sweep into the
                             workspace manifest (whose brand-site PNGs are
                             co-located). Unset (default) keeps the manifest on
                             the PNG-less in-repo corpus so a bare pytest run
                             SKIPS the live sweep on CI and dev. The gate-run
                             box / scheduled job sets this to "1". See
                             conftest.resolve_manifest_path.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import pathlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import pytest

# Test sub-package conftest exports REFERENCE_ROOT + WORKSPACE_ROOT + CORPUS_ROOT
# + resolve_manifest_path. CORPUS_ROOT prefers the in-repo ``reference_corpus/``
# copy (available on any CI checkout) over REFERENCE_ROOT (workspace
# _verification/ tree, dev-only). The brand-site PNGs and the live-fetch
# full-corpus sweep still use REFERENCE_ROOT, gated by the FIDELITY_LIVE_SWEEP
# opt-in (see resolve_manifest_path + MANIFEST_PATH below).
from .conftest import (
    CORPUS_ROOT,
    REFERENCE_ROOT,
    WORKSPACE_ROOT,
    resolve_manifest_path,
)

# D-5.1 (2026-06-13): bumped v2->v3 for Option A gate-basis rebasis.
# SSIM demoted to informational; structural dims are now the primary gate.
SCHEMA_VERSION = "library_visual_fidelity_gate_report_v3"
# One-cycle compat for v2 consumers. Remove when Phase 6 lands.
COMPAT_SCHEMA_VERSION = "library_visual_fidelity_gate_report_v2"

# HEAD pre-flight constants (RZ-A). Status >= 400 short-circuits SSIM and
# marks the tuple as route_missing. Timeout is the HEAD budget only; the
# Playwright budget (tolerance.timeout_ms) is unaffected.
HEAD_PROBE_TIMEOUT_SECONDS = 5.0
HEAD_PROBE_FAIL_STATUS_THRESHOLD = 400

_log = logging.getLogger("test_visual_fidelity_gate")


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReferenceRecord:
    """One reference capture as recorded in the manifest.

    Mirrors ``reference_capture_manifest_v1.records[i]`` minus the
    operator metadata the test does not need.
    """

    tuple_id: str
    brand: str
    category: str
    viewport: str
    source_url: str
    reference_path: pathlib.Path
    capture_mode: str


@dataclass(frozen=True)
class ToleranceConfig:
    """Parsed tolerance_config.yml.

    Kept frozen so a downstream test cannot mutate it mid-run.
    """

    ssim_floor: float
    color_bucket_overlap_min: int
    color_bucket_top_n: int
    color_quantization_bits: int
    dominant_font_family_required: bool
    brand_x_category_pass_minimum: int
    skip_on_missing_live_url: bool
    url_pattern: str
    basic_auth_env: str
    timeout_ms: int
    wait_until: str


@dataclass
class TupleOutcome:
    """Per-tuple gate result.

    `gate` is the path the tuple took:
      - "ssim"        -> primary SSIM gate passed
      - "structural"  -> SSIM gate failed, structural fallback passed
      - "fail"        -> both gates failed; see `drift_dimensions`
      - "skip"        -> live render unavailable; tuple counted as skip
    """

    tuple_id: str
    brand: str
    category: str
    viewport: str
    status: str  # PASS | FAIL | SKIP
    gate: str
    ssim: Optional[float] = None
    color_bucket_overlap: Optional[int] = None
    font_family_match: Optional[bool] = None
    drift_dimensions: List[str] = field(default_factory=list)
    error_message: Optional[str] = None
    # RZ-A: HTTP status returned by the HEAD pre-flight against the live
    # URL. None when the probe could not run (network blocked, requests
    # unavailable); an int otherwise. >= 400 means the route is missing
    # and the tuple short-circuits to FAIL with drift=["route_missing"]
    # before any SSIM call. Surfacing it lets the operator distinguish
    # "render is wrong" (SSIM low, status 200) from "page does not
    # exist" (status 404), which the v1 schema conflated.
    live_status_code: Optional[int] = None


@dataclass
class GateReport:
    """Aggregate gate report. Persisted as JSON + Markdown."""

    schema_version: str
    generated_at_utc: str
    workspace_root: str
    reference_root: str
    resemblio_base: str
    tolerance: Dict[str, object]
    total_tuples: int
    pass_count: int
    fail_count: int
    skip_count: int
    brand_x_category_passes: int
    aggregate: str  # PASS | FAIL | SKIP
    tuples: List[TupleOutcome]
    # RZ-A: one-cycle compat. Downstream consumers (Phase 7 diagnostic
    # v7) read v1 fields unchanged; the v2 schema is a superset.
    compat_schema_version: str = COMPAT_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Structural-tier text artifacts (tolerance + specs) resolve from CORPUS_ROOT:
# the in-repo ``reference_corpus/`` copy on a CI checkout, or REFERENCE_ROOT
# (workspace) on a dev machine running the full gate. Both share the same
# subdirectory layout, so derived paths work identically in both. This is what
# makes the structural tier (test_linear_font_spec_* etc.) RUN on CI instead of
# self-skipping. See the Phase 8 handoff + reference_corpus/README.md.
TOLERANCE_PATH = CORPUS_ROOT / "tolerance_config.yml"
SPECS_DIR = CORPUS_ROOT / "reference_captures" / "specs"

# Opt-in env flag for the live full-corpus sweep. When "1", the manifest (and
# thus the PNG resolution that drives the live pixel diff) resolves to the
# workspace REFERENCE_ROOT, whose PNGs are co-located. Default (unset) keeps
# the manifest on the PNG-less in-repo copy so a bare ``pytest -q`` SKIPS the
# live sweep everywhere - CI and dev alike - rather than firing slow, flaky
# network captures against resemblio.com. The gate-run box sets this to "1".
ENV_LIVE_SWEEP = "FIDELITY_LIVE_SWEEP"

# The reference manifest is co-located with the brand-site PNGs it indexes
# (load_manifest resolves each PNG relative to the manifest's parent dir). The
# PNGs live only in the workspace tree, never this public repo - so the manifest
# root decides whether the live sweep can find PNGs. resolve_manifest_path
# encodes the safe-by-default / explicit-opt-in policy; see its docstring.
MANIFEST_PATH = resolve_manifest_path(
    corpus_root=CORPUS_ROOT,
    reference_root=REFERENCE_ROOT,
    live_sweep_opt_in=os.environ.get(ENV_LIVE_SWEEP) == "1",
)

# Output stays workspace-only: gate run artifacts (live PNGs, reports) are
# not committed; REFERENCE_ROOT on CI points at a non-existent path and the
# live sweep skips before any write happens.
DEFAULT_OUTPUT_DIR = REFERENCE_ROOT / "fidelity_gate_runs"
ENV_OUTPUT_DIR = "VISUAL_FIDELITY_GATE_OUT"
ENV_RESEMBLIO_BASE = "RESEMBLIO_BASE_URL"
DEFAULT_RESEMBLIO_BASE = "https://resemblio.com"

# Channel-byte sentinel for the quantized color histogram. 4 bits per
# channel = 16 levels = 4096 total buckets. Kept as a module constant so
# the test and a future operator analysis read the same value.
_DEFAULT_QUANT_BITS = 4


# ---------------------------------------------------------------------------
# Tolerance loader
# ---------------------------------------------------------------------------


def load_tolerance(path: pathlib.Path) -> ToleranceConfig:
    """Read tolerance_config.yml and validate the shape.

    Raises ``pytest.skip.Exception`` (via ``pytest.skip``) when PyYAML is
    not installed or the file is missing; the gate is then a SKIP, not a
    failure, because the missing dependency is an environment issue and
    not a regression of the system under test.
    """
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("PyYAML not installed; cannot read tolerance_config.yml")
    if not path.exists():
        pytest.skip(f"tolerance config not found at {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        pytest.fail(f"tolerance config not a mapping: {path}")
    if data.get("schema_version") != "visual_fidelity_tolerance_v1":
        pytest.fail(
            "tolerance schema_version mismatch; expected "
            f"'visual_fidelity_tolerance_v1', got {data.get('schema_version')!r}",
        )
    structural = data.get("structural_fallback") or {}
    acceptance = data.get("acceptance") or {}
    live = data.get("live_capture") or {}
    return ToleranceConfig(
        ssim_floor=float(data["ssim_floor"]),
        color_bucket_overlap_min=int(structural.get("color_bucket_overlap_min", 3)),
        color_bucket_top_n=int(structural.get("color_bucket_top_n", 5)),
        color_quantization_bits=int(
            structural.get("color_quantization_bits", _DEFAULT_QUANT_BITS),
        ),
        dominant_font_family_required=(
            str(structural.get("dominant_font_family_match", "required")).lower()
            == "required"
        ),
        brand_x_category_pass_minimum=int(
            acceptance.get("brand_x_category_pass_minimum", 3),
        ),
        skip_on_missing_live_url=bool(
            acceptance.get("skip_on_missing_live_url", True),
        ),
        url_pattern=str(
            live.get("url_pattern", "https://resemblio.com/library/{brand}/{category}"),
        ),
        basic_auth_env=str(live.get("basic_auth_env", "LIBRARY_BASIC_AUTH")),
        timeout_ms=int(live.get("timeout_ms", 30000)),
        wait_until=str(live.get("wait_until", "networkidle")),
    )


# ---------------------------------------------------------------------------
# Manifest loader
# ---------------------------------------------------------------------------


def load_manifest(path: pathlib.Path) -> List[ReferenceRecord]:
    """Read reference manifest and return one ReferenceRecord per entry.

    Resolves reference image paths against the reference_captures
    directory (sibling of the manifest) so the records survive a moved
    workspace root. Records whose PNG is missing on disk are dropped
    with a warning rather than raising; the test then has fewer tuples
    to consider but does not crash mid-suite.
    """
    if not path.exists():
        pytest.skip(f"reference manifest not found at {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != "reference_capture_manifest_v1":
        pytest.fail(
            "manifest schema_version mismatch; expected "
            f"'reference_capture_manifest_v1', got {data.get('schema_version')!r}",
        )
    capture_dir = path.parent
    out: List[ReferenceRecord] = []
    for raw in data.get("records", []):
        ref_name = f"{raw['brand']}_{raw['category']}_{raw['viewport']}.png"
        ref_path = capture_dir / ref_name
        if not ref_path.exists():
            _log.warning(
                "manifest entry %s points at missing file %s; dropping",
                raw.get("tuple_id"), ref_path,
            )
            continue
        out.append(
            ReferenceRecord(
                tuple_id=raw["tuple_id"],
                brand=raw["brand"],
                category=raw["category"],
                viewport=raw["viewport"],
                source_url=raw.get("source_url", ""),
                reference_path=ref_path,
                capture_mode=raw.get("capture_mode", "full_page"),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Image similarity primitives
# ---------------------------------------------------------------------------


def _require_pillow():
    """Import Pillow or skip the test.

    Pillow is the visual_fidelity sub-package's already-declared second
    runtime dep (per workspace CLAUDE.md "Page to Image Utility" line).
    """
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        pytest.skip("Pillow not installed; cannot compute pixel similarity")


def compute_ssim(reference_path: pathlib.Path, live_path: pathlib.Path) -> float:
    """Return the SSIM between two PNGs, in [0.0, 1.0].

    Prefers scikit-image's ``structural_similarity`` when available.
    Falls back to a deterministic Pillow-based luminance-correlation
    surrogate that approximates SSIM for the brand-stripped-render use
    case (correlation-of-luminance on resized grayscale; documented in
    docstring so the operator knows what they are reading).

    Both paths resize the larger image to match the smaller one's
    dimensions before computing, which cancels device-pixel-ratio drift
    between reference and live captures.
    """
    from PIL import Image  # local: keep skip semantics clean

    ref_img = Image.open(reference_path).convert("L")
    live_img = Image.open(live_path).convert("L")

    # Pick the smaller dimensions so we never up-sample (interpolation
    # artifacts would lower SSIM artificially).
    target_w = min(ref_img.width, live_img.width)
    target_h = min(ref_img.height, live_img.height)
    if target_w < 8 or target_h < 8:
        return 0.0
    ref_img = ref_img.resize((target_w, target_h), Image.LANCZOS)
    live_img = live_img.resize((target_w, target_h), Image.LANCZOS)

    try:
        from skimage.metrics import structural_similarity  # type: ignore
        import numpy as np  # type: ignore

        ref_arr = np.asarray(ref_img, dtype=np.float64)
        live_arr = np.asarray(live_img, dtype=np.float64)
        score = float(
            structural_similarity(ref_arr, live_arr, data_range=255.0)
        )
        # Clamp to [0, 1] for downstream comparison stability; SSIM is
        # mathematically bounded but float drift can push us just past.
        return max(0.0, min(1.0, score))
    except ImportError:
        pass

    # Pillow-only fallback: Pearson correlation of luminance pixels +
    # a mean-difference penalty, mapped into [0, 1]. This is NOT true
    # SSIM but tracks it closely on the same-page reload baseline tests
    # we ran during Phase 5.1; documented as a surrogate.
    return _pearson_luminance_surrogate(ref_img, live_img)


def _pearson_luminance_surrogate(ref_img, live_img) -> float:
    """Surrogate SSIM via Pearson correlation on luminance bytes.

    Returns a float in [0, 1]. Used only when scikit-image is not
    available. The mapping is ``max(0, 0.5 * (1 + r)) * brightness_penalty``,
    where ``r`` is the Pearson correlation coefficient of the two
    flattened grayscale arrays and ``brightness_penalty`` discounts
    matches whose mean luminance differs by more than 32/255.
    """
    ref_bytes = list(ref_img.tobytes())
    live_bytes = list(live_img.tobytes())
    n = len(ref_bytes)
    if n == 0 or n != len(live_bytes):
        return 0.0
    ref_mean = sum(ref_bytes) / n
    live_mean = sum(live_bytes) / n
    num = 0.0
    den_r = 0.0
    den_l = 0.0
    for r, l in zip(ref_bytes, live_bytes):
        dr = r - ref_mean
        dl = l - live_mean
        num += dr * dl
        den_r += dr * dr
        den_l += dl * dl
    denom = (den_r * den_l) ** 0.5
    if denom == 0.0:
        # Both images are flat. Treat as identical if means agree.
        return 1.0 if abs(ref_mean - live_mean) < 1.0 else 0.0
    pearson = num / denom
    base = max(0.0, 0.5 * (1.0 + pearson))
    brightness_delta = abs(ref_mean - live_mean) / 255.0
    penalty = max(0.0, 1.0 - max(0.0, brightness_delta - (32.0 / 255.0)) * 4.0)
    return base * penalty


# ---------------------------------------------------------------------------
# Color-bucket histogram (structural fallback dimension #1)
# ---------------------------------------------------------------------------


def dominant_color_buckets(
    path: pathlib.Path,
    top_n: int,
    quantization_bits: int,
) -> List[int]:
    """Return the top-N quantized-RGB bucket IDs by pixel count.

    ``quantization_bits`` is per-channel; total buckets = (2**bits)**3.
    With 4 bits that's 16 levels per channel and 4096 buckets total;
    enough resolution to distinguish indigo from royal blue while
    forgiving the sub-pixel hinting noise that PNG re-encoders inject.

    Returns the bucket IDs in descending count order. Pure-data; tested.
    """
    from PIL import Image

    img = Image.open(path).convert("RGB")
    shift = 8 - quantization_bits
    counts: Dict[int, int] = {}
    pixels = img.getdata()
    for r, g, b in pixels:
        bucket = (
            ((r >> shift) << (2 * quantization_bits))
            | ((g >> shift) << quantization_bits)
            | (b >> shift)
        )
        counts[bucket] = counts.get(bucket, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return [bucket for bucket, _ in ranked[:top_n]]


def color_bucket_overlap(
    ref_path: pathlib.Path,
    live_path: pathlib.Path,
    top_n: int,
    quantization_bits: int,
) -> int:
    """Count how many of the reference's top-N buckets appear in live's top-N.

    Set intersection on the two top-N bucket lists. Maximum return value
    is ``top_n``; minimum is 0.
    """
    ref_buckets = set(
        dominant_color_buckets(ref_path, top_n, quantization_bits)
    )
    live_buckets = set(
        dominant_color_buckets(live_path, top_n, quantization_bits)
    )
    return len(ref_buckets & live_buckets)


# ---------------------------------------------------------------------------
# Dominant font-family check (structural fallback dimension #2)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Assertion-evaluation helpers (Phase 10 refactor: moved to assertion_eval.py)
# Re-exported here so existing callers (test_spec_coverage.py) keep working
# without import churn.
# ---------------------------------------------------------------------------

from .assertion_eval import (  # noqa: F401  (re-exports; used by test_spec_coverage.py)
    evaluate_font_family_against_live_html,
    expected_token_from_assertion,
    font_family_assertion_from_spec,
)


# ---------------------------------------------------------------------------
# HEAD pre-flight (RZ-A)
# ---------------------------------------------------------------------------


def probe_live_status(
    url: str,
    *,
    timeout_seconds: float = HEAD_PROBE_TIMEOUT_SECONDS,
    basic_auth: Optional[Tuple[str, str]] = None,
) -> Optional[int]:
    """Return the HTTP status of a HEAD probe against ``url``, or None.

    Follows redirects so the project's `/library/foo/` -> `/library/foo`
    trailing-slash middleware maps to the eventual served route. Returns
    the final response's status_code (an int) when the probe completes,
    or None when ``requests`` is not installed or the call raises any
    network-layer exception (DNS, TLS, timeout, refused). None is
    treated by the caller as "could not determine status; do not gate";
    a real 4xx/5xx is treated as route_missing.

    Pure-function relative to the network: no side effects beyond the
    HEAD request itself. Unit-tested via a stub callable rather than a
    live network, so the test suite stays offline-safe.

    The 5s timeout is well below the existing 30s Playwright budget;
    no separate config knob needed (CTO brief Section
    "Gate pre-flight spec", item 4).
    """
    try:
        import requests  # type: ignore[import-not-found]
    except ImportError:
        _log.warning("requests not installed; HEAD pre-flight unavailable")
        return None
    try:
        response = requests.head(
            url,
            allow_redirects=True,
            timeout=timeout_seconds,
            auth=basic_auth,
        )
    except Exception as exc:  # pragma: no cover - network dependent
        _log.warning("HEAD pre-flight failed for %s: %s", url, exc)
        return None
    return int(response.status_code)


def classify_live_status(
    status_code: Optional[int],
    *,
    fail_threshold: int = HEAD_PROBE_FAIL_STATUS_THRESHOLD,
) -> bool:
    """Return True when the HEAD-probed status warrants a route_missing FAIL.

    None (probe could not run) is NOT a route_missing signal: returning
    False here lets the existing SSIM ladder run, which already has its
    own SKIP semantics for unreachable URLs. A real >= 400 status is a
    deterministic miss.

    Pure-data; tested.
    """
    if status_code is None:
        return False
    return status_code >= fail_threshold


# ---------------------------------------------------------------------------
# Live render capture
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveRender:
    """Bundle of artifacts captured from one live URL fetch."""

    png_path: pathlib.Path
    html: str


def capture_live_render(
    url: str,
    viewport: str,
    output_dir: pathlib.Path,
    tuple_id: str,
    tolerance: ToleranceConfig,
) -> Optional[LiveRender]:
    """Capture a live screenshot + page HTML via Playwright.

    Returns None when Playwright is not importable, the browser launch
    fails, or the page does not respond within ``tolerance.timeout_ms``.
    Callers treat None as "live URL not capturable; SKIP this tuple".

    The screenshot is full-page. Output PNG path is
    ``<output_dir>/live_<tuple_id>.png``.

    Idempotent: a second call for the same ``tuple_id`` overwrites the
    PNG and re-fetches the HTML (the gate is a one-shot artifact run,
    not a daemon; caching live output across runs would mask drift).
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        _log.warning("playwright not installed; live capture unavailable")
        return None

    width_s, height_s = viewport.split("x")
    width = int(width_s)
    height = int(height_s)
    output_dir.mkdir(parents=True, exist_ok=True)
    png_path = output_dir / f"live_{tuple_id}.png"

    basic_auth = os.environ.get(tolerance.basic_auth_env, "")
    http_credentials = None
    if basic_auth and ":" in basic_auth:
        user, password = basic_auth.split(":", 1)
        http_credentials = {"username": user, "password": password}

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            context = browser.new_context(
                viewport={"width": width, "height": height},
                http_credentials=http_credentials,
            )
            page = context.new_page()
            page.set_default_timeout(tolerance.timeout_ms)
            page.goto(url, wait_until=tolerance.wait_until)
            html = page.content()
            page.screenshot(path=str(png_path), full_page=True)
            browser.close()
    except Exception as exc:  # pragma: no cover - environment dependent
        _log.warning("live capture failed for %s: %s", url, exc)
        return None

    if not png_path.exists():
        return None
    return LiveRender(png_path=png_path, html=html)


# ---------------------------------------------------------------------------
# Per-tuple evaluation
# ---------------------------------------------------------------------------


def evaluate_tuple(
    record: ReferenceRecord,
    tolerance: ToleranceConfig,
    output_dir: pathlib.Path,
    resemblio_base: str,
) -> TupleOutcome:
    """Run the full gate ladder on one tuple. Returns TupleOutcome.

    Pure-data ladder; the live-render side-effect is isolated to
    ``capture_live_render`` so a future caller can swap in a recorded
    fixture for offline runs.
    """
    url = tolerance.url_pattern.format(
        brand=record.brand, category=record.category,
    )
    # Allow base override
    if resemblio_base and resemblio_base != DEFAULT_RESEMBLIO_BASE:
        url = url.replace(DEFAULT_RESEMBLIO_BASE, resemblio_base.rstrip("/"))

    # RZ-A: HEAD pre-flight before any expensive capture or SSIM. If
    # the route is missing (>= 400) the tuple FAILs with drift=
    # ["route_missing"] and SSIM is NOT computed. This is the fix for
    # the 4 vercel viewport-clip tuples that false-PASSed by SSIM-
    # scoring a 404 shell against a brand reference at low entropy.
    basic_auth_env = os.environ.get(tolerance.basic_auth_env, "")
    head_auth: Optional[Tuple[str, str]] = None
    if basic_auth_env and ":" in basic_auth_env:
        user, password = basic_auth_env.split(":", 1)
        head_auth = (user, password)
    live_status = probe_live_status(url, basic_auth=head_auth)
    if classify_live_status(live_status):
        _log.info(
            "tuple %s: HEAD pre-flight returned %s; route_missing FAIL",
            record.tuple_id, live_status,
        )
        return TupleOutcome(
            tuple_id=record.tuple_id,
            brand=record.brand,
            category=record.category,
            viewport=record.viewport,
            status="FAIL",
            gate="route_missing",
            drift_dimensions=["route_missing"],
            error_message=f"HEAD {url} returned {live_status}",
            live_status_code=live_status,
        )

    live = capture_live_render(
        url=url,
        viewport=record.viewport,
        output_dir=output_dir,
        tuple_id=record.tuple_id,
        tolerance=tolerance,
    )
    if live is None:
        return TupleOutcome(
            tuple_id=record.tuple_id,
            brand=record.brand,
            category=record.category,
            viewport=record.viewport,
            status="SKIP",
            gate="skip",
            error_message=f"live render unavailable for {url}",
            live_status_code=live_status,
        )

    # Compute SSIM for informational purposes only (D-5.1 Option A).
    # SSIM is NOT a gating dimension under the v3 semantics; it is stored
    # in the TupleOutcome so the operator can track it over time without it
    # affecting the PASS/FAIL verdict. See module docstring for rationale.
    try:
        ssim: Optional[float] = compute_ssim(record.reference_path, live.png_path)
    except Exception as exc:  # pragma: no cover - environment dependent
        _log.warning("ssim computation failed for %s: %s", record.tuple_id, exc)
        ssim = None

    # Primary gate (D-5.1 Option A): structural dimensions.
    # color_bucket_overlap >= min AND dominant_font_family assertion passes
    # -> PASS regardless of SSIM. Both must pass; either failing -> FAIL.
    overlap = color_bucket_overlap(
        record.reference_path,
        live.png_path,
        top_n=tolerance.color_bucket_top_n,
        quantization_bits=tolerance.color_quantization_bits,
    )
    color_ok = overlap >= tolerance.color_bucket_overlap_min

    font_assertion = font_family_assertion_from_spec(
        SPECS_DIR, record.brand, record.category,
    )
    if font_assertion is None:
        # Spec missing a font assertion. If config requires the font
        # check, treat as a soft-failure (None becomes False); else
        # treat as satisfied (the dimension is not checkable).
        font_ok: Optional[bool] = (
            False if tolerance.dominant_font_family_required else True
        )
    else:
        font_ok = evaluate_font_family_against_live_html(
            font_assertion, live.html,
        )

    if color_ok and font_ok:
        return TupleOutcome(
            tuple_id=record.tuple_id,
            brand=record.brand,
            category=record.category,
            viewport=record.viewport,
            status="PASS",
            gate="structural",
            ssim=ssim,
            color_bucket_overlap=overlap,
            font_family_match=font_ok,
            live_status_code=live_status,
        )

    drift: List[str] = []
    if not color_ok:
        drift.append("color")
    if not font_ok:
        drift.append("font")
    return TupleOutcome(
        tuple_id=record.tuple_id,
        brand=record.brand,
        category=record.category,
        viewport=record.viewport,
        status="FAIL",
        gate="fail",
        ssim=ssim,
        color_bucket_overlap=overlap,
        font_family_match=font_ok,
        drift_dimensions=drift,
        live_status_code=live_status,
    )


# ---------------------------------------------------------------------------
# Aggregation + reporting
# ---------------------------------------------------------------------------


def aggregate_brand_category_passes(outcomes: List[TupleOutcome]) -> int:
    """Count (brand, category) pairs that pass on ALL viewports.

    A pair with one PASS and one SKIP does NOT count as passing; the
    acceptance criterion is "all viewports for the pair pass". This
    keeps the bar honest: a tuple that we could not even run does not
    move the gate forward.
    """
    by_pair: Dict[Tuple[str, str], List[str]] = {}
    for outcome in outcomes:
        by_pair.setdefault((outcome.brand, outcome.category), []).append(
            outcome.status,
        )
    return sum(
        1 for statuses in by_pair.values()
        if statuses and all(s == "PASS" for s in statuses)
    )


def render_markdown(report: GateReport) -> str:
    """Render the aggregate gate report as Markdown for human review."""
    lines: List[str] = []
    lines.append("# Library Visual Fidelity Gate Report (Phase 5.2)")
    lines.append("")
    lines.append(f"- Schema: `{report.schema_version}`")
    lines.append(f"- Compat schema (one cycle): `{report.compat_schema_version}`")
    lines.append(f"- Generated (UTC): {report.generated_at_utc}")
    lines.append(f"- Workspace: `{report.workspace_root}`")
    lines.append(f"- Reference root: `{report.reference_root}`")
    lines.append(f"- Resemblio base: `{report.resemblio_base}`")
    lines.append(
        f"- Aggregate: **{report.aggregate}** "
        f"({report.pass_count} PASS / {report.fail_count} FAIL / "
        f"{report.skip_count} SKIP of {report.total_tuples})"
    )
    lines.append(
        f"- Brand x category passes: {report.brand_x_category_passes} "
        f"(acceptance floor "
        f"{report.tolerance.get('brand_x_category_pass_minimum')})"
    )
    lines.append("")
    lines.append("## Tolerance applied")
    lines.append("")
    for key, val in report.tolerance.items():
        lines.append(f"- `{key}`: {val}")
    lines.append("")
    lines.append("## Per-tuple outcomes")
    lines.append("")
    lines.append("| Tuple | Status | Gate | Live | SSIM | Colors | Font | Drift |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for t in report.tuples:
        ssim_s = f"{t.ssim:.3f}" if t.ssim is not None else "-"
        colors_s = (
            str(t.color_bucket_overlap)
            if t.color_bucket_overlap is not None else "-"
        )
        font_s = (
            "yes" if t.font_family_match is True
            else "no" if t.font_family_match is False
            else "-"
        )
        drift = ", ".join(t.drift_dimensions) if t.drift_dimensions else "-"
        live_s = (
            str(t.live_status_code)
            if t.live_status_code is not None else "-"
        )
        lines.append(
            f"| {t.tuple_id} | {t.status} | {t.gate} | {live_s} | "
            f"{ssim_s} | {colors_s} | {font_s} | {drift} |"
        )
    lines.append("")
    if report.fail_count:
        lines.append("## Triage hints")
        lines.append("")
        lines.append("- `color` drift -> Phase 2 color-propagation tests")
        lines.append("- `font` drift -> Phase 1 font fidelity stack")
        lines.append(
            "- `structure` drift -> Phase 4 per-category fidelity scaffold"
        )
        lines.append(
            "- `route_missing` drift -> the live route returned >= 400 "
            "before SSIM ran; indexer drain or library_pages population "
            "is the fix, not a rendering change (see RZ-A brief)"
        )
        lines.append("")
    return "\n".join(lines) + "\n"


def write_report(report: GateReport, output_dir: pathlib.Path) -> Tuple[
    pathlib.Path, pathlib.Path,
]:
    """Persist the aggregate report as gate_report.{json,md}.

    Idempotent: each run overwrites the prior gate_report.* under the
    same output dir. A per-run subdir (one per timestamp) is the
    caller's responsibility via the VISUAL_FIDELITY_GATE_OUT env var.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "gate_report.json"
    md_path = output_dir / "gate_report.md"
    payload = dataclasses.asdict(report)
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return (json_path, md_path)


# ---------------------------------------------------------------------------
# The pytest entry point
# ---------------------------------------------------------------------------


def test_library_render_within_tolerance_of_brand_reference() -> None:
    """Gate: live Resemblio library renders stay within tolerance of references.

    This is the **live full-corpus sweep** (Tier 2). It needs the brand-site
    reference PNGs, which live only in the workspace ``_verification/`` tree -
    never this public repo. By default ``MANIFEST_PATH`` resolves to the
    PNG-less in-repo corpus, so ``load_manifest`` finds zero records on disk and
    this test SKIPS on CI and on a routine dev ``pytest -q``. The gate-run box /
    scheduled job sets ``FIDELITY_LIVE_SWEEP=1`` to point the manifest at the
    workspace tree (PNGs co-located) and actually run the sweep. The structural
    tier (test_linear_font_spec_* and the pure-data tests below) runs on CI
    regardless via the vendored in-repo specs - that is the Phase 8 deliverable.

    Pass condition: at least
    ``tolerance.brand_x_category_pass_minimum`` distinct (brand,
    category) combinations pass on all their viewports via the
    structural gate (D-5.1 Option A; SSIM is informational only).

    Skip condition: no reference records are present (the default,
    PNG-less, manifest path) OR the live URL cannot be reached for ALL
    tuples (Playwright not installed, network unavailable, basic auth
    absent, etc.) and ``tolerance.skip_on_missing_live_url`` is true.

    Fail condition: live URLs ARE reachable but the brand-x-category
    pass count falls below the acceptance floor. The aggregate report
    Markdown points at which dimension drifted per failing tuple.
    """
    _require_pillow()
    tolerance = load_tolerance(TOLERANCE_PATH)
    records = load_manifest(MANIFEST_PATH)
    if not records:
        pytest.skip("no reference records present; nothing to gate against")

    resemblio_base = os.environ.get(ENV_RESEMBLIO_BASE, DEFAULT_RESEMBLIO_BASE)
    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_root_override = os.environ.get(ENV_OUTPUT_DIR)
    if out_root_override:
        output_dir = pathlib.Path(out_root_override).resolve()
    else:
        output_dir = DEFAULT_OUTPUT_DIR / run_stamp

    outcomes: List[TupleOutcome] = []
    for record in records:
        outcome = evaluate_tuple(
            record=record,
            tolerance=tolerance,
            output_dir=output_dir,
            resemblio_base=resemblio_base,
        )
        outcomes.append(outcome)
        _log.info(
            "tuple %s -> %s (%s)",
            outcome.tuple_id, outcome.status, outcome.gate,
        )

    pass_count = sum(1 for o in outcomes if o.status == "PASS")
    fail_count = sum(1 for o in outcomes if o.status == "FAIL")
    skip_count = sum(1 for o in outcomes if o.status == "SKIP")
    bxc_passes = aggregate_brand_category_passes(outcomes)

    if skip_count == len(outcomes) and tolerance.skip_on_missing_live_url:
        # All tuples skipped because the live URL was unreachable. Write
        # the report (so the operator can see the skip rationale) but
        # raise pytest.skip rather than fail.
        report = GateReport(
            schema_version=SCHEMA_VERSION,
            generated_at_utc=datetime.now(timezone.utc).isoformat(),
            workspace_root=str(WORKSPACE_ROOT),
            reference_root=str(REFERENCE_ROOT),
            resemblio_base=resemblio_base,
            tolerance=dataclasses.asdict(tolerance),
            total_tuples=len(outcomes),
            pass_count=0,
            fail_count=0,
            skip_count=skip_count,
            brand_x_category_passes=0,
            aggregate="SKIP",
            tuples=outcomes,
        )
        write_report(report, output_dir)
        pytest.skip(
            "all tuples skipped (live URL unreachable). "
            f"Report at {output_dir}",
        )

    if bxc_passes >= tolerance.brand_x_category_pass_minimum:
        aggregate = "PASS"
    else:
        aggregate = "FAIL"

    report = GateReport(
        schema_version=SCHEMA_VERSION,
        generated_at_utc=datetime.now(timezone.utc).isoformat(),
        workspace_root=str(WORKSPACE_ROOT),
        reference_root=str(REFERENCE_ROOT),
        resemblio_base=resemblio_base,
        tolerance=dataclasses.asdict(tolerance),
        total_tuples=len(outcomes),
        pass_count=pass_count,
        fail_count=fail_count,
        skip_count=skip_count,
        brand_x_category_passes=bxc_passes,
        aggregate=aggregate,
        tuples=outcomes,
    )
    json_path, md_path = write_report(report, output_dir)
    _log.info("gate report written: %s / %s", json_path, md_path)

    assert aggregate == "PASS", (
        f"Visual fidelity gate FAILED: only {bxc_passes} brand x "
        f"category pair(s) passed; needed "
        f"{tolerance.brand_x_category_pass_minimum}. "
        f"Per-tuple report: {md_path}"
    )


# ---------------------------------------------------------------------------
# Pure-data tests (no network, no Playwright)
# ---------------------------------------------------------------------------


def test_load_tolerance_validates_schema(tmp_path: pathlib.Path) -> None:
    """load_tolerance fails fast on schema_version mismatch."""
    bad = tmp_path / "bad.yml"
    bad.write_text(
        "schema_version: wrong_version\nssim_floor: 0.5\n",
        encoding="utf-8",
    )
    # pytest.fail() raises _pytest.outcomes.Failed which inherits from
    # BaseException, not Exception, so catching BaseException is the
    # robust pattern across pytest versions.
    with pytest.raises(BaseException):
        load_tolerance(bad)


def test_aggregate_brand_category_passes_counts_all_viewport_pass() -> None:
    """A pair counts only when every viewport passes."""
    outcomes = [
        TupleOutcome(
            tuple_id="a__alphabet__1440x900",
            brand="a", category="alphabet", viewport="1440x900",
            status="PASS", gate="ssim",
        ),
        TupleOutcome(
            tuple_id="a__alphabet__375x812",
            brand="a", category="alphabet", viewport="375x812",
            status="PASS", gate="ssim",
        ),
        TupleOutcome(
            tuple_id="b__alphabet__1440x900",
            brand="b", category="alphabet", viewport="1440x900",
            status="PASS", gate="ssim",
        ),
        TupleOutcome(
            tuple_id="b__alphabet__375x812",
            brand="b", category="alphabet", viewport="375x812",
            status="FAIL", gate="fail",
        ),
        TupleOutcome(
            tuple_id="c__alphabet__1440x900",
            brand="c", category="alphabet", viewport="1440x900",
            status="PASS", gate="ssim",
        ),
        TupleOutcome(
            tuple_id="c__alphabet__375x812",
            brand="c", category="alphabet", viewport="375x812",
            status="SKIP", gate="skip",
        ),
    ]
    # Only `a` has all-PASS; `b` has a FAIL, `c` has a SKIP. Count = 1.
    assert aggregate_brand_category_passes(outcomes) == 1


def test_pearson_luminance_surrogate_self_compare_is_one() -> None:
    """Identical images score 1.0 on the Pillow-only surrogate."""
    pillow = pytest.importorskip("PIL.Image")
    img = pillow.new("L", (16, 16), color=128)
    score = _pearson_luminance_surrogate(img, img.copy())
    assert score == pytest.approx(1.0, abs=1e-6)


def test_font_family_assertion_extracts_includes_token(tmp_path: pathlib.Path) -> None:
    """The JS-evaluator path pulls the font-family token out of `.includes(...)`."""
    spec_file = tmp_path / "x_alphabet.json"
    spec_file.write_text(
        json.dumps({
            "schema_version": "fidelity_spec_v2",
            "assertions": [{
                "id": "x-font-family-uses-free-alt",
                "evaluate": (
                    "(() => { const fam = ''; "
                    "return fam.toLowerCase().includes('inter'); })()"
                ),
                "expected": True,
            }],
        }),
        encoding="utf-8",
    )
    assertion = font_family_assertion_from_spec(tmp_path, "x", "alphabet")
    assert assertion is not None
    # Live HTML contains the token: pass.
    assert evaluate_font_family_against_live_html(
        assertion, "<style>font-family: Inter, sans-serif;</style>",
    )
    # Live HTML missing the token: fail.
    assert not evaluate_font_family_against_live_html(
        assertion, "<style>font-family: Times, serif;</style>",
    )


def test_dominant_color_buckets_top_n_bound(tmp_path: pathlib.Path) -> None:
    """top_n caps the returned bucket list length."""
    pytest.importorskip("PIL.Image")  # skip cleanly when Pillow absent (CI [test] extra)
    from PIL import Image
    img_path = tmp_path / "swatch.png"
    # Build a 4-color stripe so we have a known small palette.
    img = Image.new("RGB", (8, 8))
    pixels = img.load()
    palette = [(0, 0, 0), (255, 0, 0), (0, 255, 0), (0, 0, 255)]
    for x in range(8):
        for y in range(8):
            pixels[x, y] = palette[(x * 8 + y) % 4]
    img.save(img_path)
    buckets = dominant_color_buckets(img_path, top_n=2, quantization_bits=4)
    assert len(buckets) == 2


def test_classify_live_status_none_returns_false() -> None:
    """A None status (probe could not run) is NOT route_missing.

    The semantic is "do not gate on what we could not measure"; the
    SSIM/structural ladder still gets its turn. Only a real 4xx/5xx
    response from the live URL short-circuits to FAIL.
    """
    assert classify_live_status(None) is False


def test_classify_live_status_200_returns_false() -> None:
    """A 200 OK does not trigger route_missing."""
    assert classify_live_status(200) is False


def test_classify_live_status_404_returns_true() -> None:
    """A 404 Not Found triggers route_missing FAIL.

    This is the case the RZ-A patch closes: a 404 shell was being
    SSIM-compared to a brand reference and the low entropy floated
    above the 0.65 ssim_floor, producing a false PASS.
    """
    assert classify_live_status(404) is True


def test_classify_live_status_5xx_returns_true() -> None:
    """A 5xx error triggers route_missing FAIL (same semantic as 4xx)."""
    assert classify_live_status(503) is True


def test_classify_live_status_respects_custom_threshold() -> None:
    """The fail_threshold is overridable for callers with stricter bars."""
    assert classify_live_status(399, fail_threshold=400) is False
    assert classify_live_status(400, fail_threshold=400) is True


def test_probe_live_status_calls_head_with_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """probe_live_status issues HEAD with allow_redirects=True + timeout.

    Mocks ``requests.head`` so the test stays offline. Confirms the
    function returns the integer status_code and that the call was
    shaped per the CTO brief: HEAD, follow redirects, bounded timeout.
    Catches the regression where a future refactor swaps HEAD for GET
    or drops allow_redirects (which would miss the trailing-slash
    normalizer redirect documented in the brief).
    """
    requests = pytest.importorskip("requests")

    calls: List[Dict[str, object]] = []

    class _StubResponse:
        status_code = 200

    def _fake_head(url: str, **kwargs: object) -> object:
        calls.append({"url": url, **kwargs})
        return _StubResponse()

    monkeypatch.setattr(requests, "head", _fake_head)
    status = probe_live_status("https://example.test/library/foo/buttons/")
    assert status == 200
    assert len(calls) == 1
    call = calls[0]
    assert call["allow_redirects"] is True
    assert call["timeout"] == pytest.approx(HEAD_PROBE_TIMEOUT_SECONDS)


def test_probe_live_status_returns_404_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live URL returning 404 surfaces as an int 404 from probe."""
    requests = pytest.importorskip("requests")

    class _StubResponse:
        status_code = 404

    monkeypatch.setattr(
        requests, "head", lambda url, **kwargs: _StubResponse(),
    )
    assert probe_live_status("https://example.test/library/missing/x/") == 404


def test_probe_live_status_swallows_network_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Network exceptions yield None rather than raising.

    The caller treats None as "probe could not run; let the SSIM ladder
    decide." Raising would make a flaky network into a gate FAIL, which
    is not the semantic the brief asks for.
    """
    requests = pytest.importorskip("requests")

    def _raise(url: str, **kwargs: object) -> object:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(requests, "head", _raise)
    assert probe_live_status("https://example.test/library/x/y/") is None


def test_rz_a_dry_run_vercel_tuples_now_fail_route_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Dry-run: 4 vercel viewport-clip tuples now FAIL with route_missing.

    Replays the RZ-A failure mode against the prior baseline
    (`fidelity_gate_runs/20260605T031858Z/`): the vercel routes return
    404 on resemblio.com (no `library_pages` rows per CTO Finding 1).
    Under the v1 gate, SSIM-scoring the 404 shell against the brand
    reference floated above the 0.65 floor and false-PASSed. Under v2
    (this patch), the HEAD pre-flight short-circuits to FAIL with
    drift=["route_missing"] before any SSIM call.

    Verifies (without live network) that ``evaluate_tuple`` correctly
    routes a 404 status to route_missing FAIL for every tuple in the
    prior baseline that false-PASSed. The mock stands in for
    ``resemblio.com/library/vercel/<cat>/`` returning 404.
    """
    requests = pytest.importorskip("requests")
    pytest.importorskip("PIL.Image")

    # Build a tolerance config in-memory (avoid YAML round-trip).
    tolerance = ToleranceConfig(
        ssim_floor=0.65,
        color_bucket_overlap_min=3,
        color_bucket_top_n=5,
        color_quantization_bits=4,
        dominant_font_family_required=True,
        brand_x_category_pass_minimum=3,
        skip_on_missing_live_url=True,
        url_pattern="https://resemblio.com/library/{brand}/{category}",
        basic_auth_env="LIBRARY_BASIC_AUTH_UNSET",
        timeout_ms=30000,
        wait_until="networkidle",
    )

    # Build a synthetic reference PNG so the manifest "exists" check
    # never fires before the HEAD probe. A 1x1 image is enough; the
    # gate must reach the route_missing branch before SSIM is touched.
    from PIL import Image
    ref_path = tmp_path / "vercel_alphabet_1440x900.png"
    Image.new("RGB", (16, 16), color=(120, 60, 200)).save(ref_path)

    # The 4 vercel viewport-clip tuples that false-PASSed in the prior
    # baseline (run 20260605T031858Z). All four return 404 on
    # resemblio.com because vercel has no library_pages rows.
    false_pass_tuples = [
        ("vercel", "alphabet", "1440x900"),
        ("vercel", "buttons", "1440x900"),
        ("vercel", "about-team", "1440x900"),
        ("vercel", "alphabet", "375x812"),
    ]

    # Stub the live HEAD to return 404 (the prod-reality per Finding 1).
    class _StubResponse404:
        status_code = 404

    monkeypatch.setattr(
        requests, "head", lambda url, **kwargs: _StubResponse404(),
    )

    # Belt and suspenders: if route_missing fails to short-circuit and
    # the code falls through to capture_live_render, force it to return
    # None so the test SKIPs rather than tries Playwright. A correct v2
    # patch never reaches this stub.
    import sys as _sys
    monkeypatch.setattr(
        _sys.modules[__name__],
        "capture_live_render",
        lambda **kwargs: None,
    )

    out_dir = tmp_path / "out"
    for brand, category, viewport in false_pass_tuples:
        record = ReferenceRecord(
            tuple_id=f"{brand}__{category}__{viewport}",
            brand=brand,
            category=category,
            viewport=viewport,
            source_url="https://vercel.com/",
            reference_path=ref_path,
            capture_mode="viewport_clip",
        )
        outcome = evaluate_tuple(
            record=record,
            tolerance=tolerance,
            output_dir=out_dir,
            resemblio_base=DEFAULT_RESEMBLIO_BASE,
        )
        assert outcome.status == "FAIL", (
            f"{record.tuple_id}: expected FAIL under v2, got {outcome.status}; "
            "the false-PASS bug RZ-A closes has regressed"
        )
        assert outcome.gate == "route_missing", (
            f"{record.tuple_id}: expected gate=route_missing, got {outcome.gate}"
        )
        assert outcome.drift_dimensions == ["route_missing"], (
            f"{record.tuple_id}: expected drift=['route_missing'], "
            f"got {outcome.drift_dimensions}"
        )
        assert outcome.live_status_code == 404, (
            f"{record.tuple_id}: expected live_status_code=404, "
            f"got {outcome.live_status_code}"
        )
        assert outcome.ssim is None, (
            f"{record.tuple_id}: SSIM must NOT be computed when route is "
            f"missing; got ssim={outcome.ssim}"
        )


def test_schema_version_is_v3() -> None:
    """The gate report schema_version is the v3 bump from D-5.1 (Option A rebasis).

    Pins the bump. Regressing to v2 reintroduces the SSIM-primary gate.
    v2 introduced the RZ-A HEAD pre-flight (still present in v3).
    """
    assert SCHEMA_VERSION == "library_visual_fidelity_gate_report_v3"
    assert COMPAT_SCHEMA_VERSION == "library_visual_fidelity_gate_report_v2"


def test_color_bucket_overlap_self_compare_max(tmp_path: pathlib.Path) -> None:
    """An image overlaps itself at top_n."""
    pytest.importorskip("PIL.Image")  # skip cleanly when Pillow absent (CI [test] extra)
    from PIL import Image
    img_path = tmp_path / "swatch.png"
    img = Image.new("RGB", (8, 8), color=(120, 60, 200))
    img.save(img_path)
    overlap = color_bucket_overlap(
        img_path, img_path, top_n=3, quantization_bits=4,
    )
    assert overlap == 1  # Only one distinct color in the image.


# ---------------------------------------------------------------------------
# Phase 5.2 tests: linear font drift root cause + regression pins
# ---------------------------------------------------------------------------


def test_linear_font_spec_matches_actual_live_disclosure() -> None:
    """Linear font specs correctly assert the actual live disclosure text.

    Root cause of the linear 'font: no' drift (2026-06-13 gate run):
    The linear alphabet + about-team specs originally asserted "Default free
    type system" and were then corrected to "ships no captured brand font".
    Both were wrong. The live prod DB probe (2026-06-13) showed:

        "Linear uses Inter. Rendered here with Inter (free, designed by
        Rasmus Andersson)."

    Linear's DRL tokens carry Inter as the display-slot font.
    _slot_first_preference("display", tokens) -> "Inter".
    lookup("inter") -> brand_font_name="Inter", free_alternative_name="Inter".
    brand_font_first_preference is NOT None -> render_font_disclosure_html
    uses the "uses {brand_font_name}" headline, not the "ships no captured"
    headline.

    After the fix: the assertion expected_text is "linear uses inter" which
    IS a substring of the live disclosure (case-insensitive contains check).

    Canonical live HTML shape (stable as long as linear's DRL tokens stay Inter):
        '<aside ...><strong>Linear uses Inter.</strong> Rendered here with
        <a href="...">Inter</a> (free, designed by Rasmus Andersson).</aside>'
    """
    # Self-skip on the standalone resemblio-api CI checkout, where the
    # workspace _verification/ spec tree (SPECS_DIR) is not present. Matches
    # the self-skip discipline every other test in this module follows
    # (load_tolerance / load_manifest call pytest.skip on missing files).
    # Without this guard, font_family_assertion_from_spec returns None on a
    # missing spec file and the assert below turns the CI deploy red - which
    # is exactly the regression that blocked deploys 2026-06-13. The test
    # still runs in full wherever the specs exist (dev + the gate run box).
    if not SPECS_DIR.exists():
        pytest.skip(f"reference specs dir not found at {SPECS_DIR}")

    # Canonical live HTML for linear: Inter is the display font in DRL tokens.
    live_html = (
        '<aside class="rs-font-attribution" data-rs-class="font-attribution">'
        "<strong>Linear uses Inter.</strong> "
        "Rendered here with "
        '<a href="https://fonts.googleapis.com/css2?family=Inter:wght@300..700&display=swap">'
        "Inter</a> (free, designed by Rasmus Andersson)."
        "</aside>"
    )

    for category in ("alphabet", "about-team"):
        spec_path = SPECS_DIR / f"linear_{category}.json"
        if not spec_path.exists():
            pytest.skip(f"linear_{category} spec not found at {spec_path}")
        assertion = font_family_assertion_from_spec(SPECS_DIR, "linear", category)
        assert assertion is not None, (
            f"linear_{category} spec must have at least one assertion with "
            f"'font' or 'family' in the id"
        )
        result = evaluate_font_family_against_live_html(assertion, live_html)
        assert result is True, (
            f"linear_{category} font assertion fails against the actual live disclosure HTML.\n"
            f"Expected the spec assertion expected_text to be a substring of the disclosure. "
            f"Live disclosure says 'Linear uses Inter.' because linear's DRL tokens carry "
            f"Inter as the display font (lookup('inter')->brand_font_name='Inter'). "
            f"The spec assertion expected_text should be 'linear uses inter'.\n"
            f"Assertion: {assertion}"
        )


# ---------------------------------------------------------------------------
# Phase 5.1 RED tests: Option A gate-basis rebasis (D-5.1, 2026-06-13)
#
# These tests are RED under the v2 gate and GREEN once evaluate_tuple is
# rebased on structural dims as the primary gate (SSIM demoted to
# informational). See handoff _HANDOFF_2026-06-13_library-v5-phase5-*.md
# ---------------------------------------------------------------------------


def test_schema_version_is_v3_option_a_gate_rebasis() -> None:
    """Gate report schema_version reflects the Option A (D-5.1) rebasis bump.

    D-5.1 decision (2026-06-13, Opus/Jim locked): demote raw full-page SSIM to
    informational; make structural dimensions (color-bucket overlap + font-family
    match) the primary gate. Inspirado-no-copiado rationale: Resemblio renders
    brand-stripped type specimens, not copies of the real brand site. A render
    scoring high SSIM against the real site would contradict the product's legal
    and brand posture. Regressing to v2 reintroduces the SSIM-primary gate.
    """
    assert SCHEMA_VERSION == "library_visual_fidelity_gate_report_v3"


def test_compat_schema_version_is_v2_after_option_a_bump() -> None:
    """One-cycle compat schema covers prior v2 gate consumers.

    Per the file's established deprecation discipline: compat covers one cycle,
    removed when the next bump lands. Dated note: demoted 2026-06-13 (D-5.1).
    """
    assert COMPAT_SCHEMA_VERSION == "library_visual_fidelity_gate_report_v2"


def test_ssim_above_floor_not_sole_pass_path_option_a(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Under Option A, SSIM >= ssim_floor alone does not cause a PASS.

    D-5.1 decision (2026-06-13, Opus/Jim locked): structural dims are the
    primary gate; SSIM is informational only.

    Scenario: SSIM = 0.90 (above 0.65 floor), color_overlap = 1 (below min 3),
    no font spec for this synthetic brand (dominant_font_family_required=True
    -> font_ok=False).

    Under the v2 gate (prior): PASS via the SSIM primary gate.
    Under the v3 gate (Option A): FAIL via structural primary gate
    (color_ok=False, font_ok=False).

    Verifies that the structural gate is checked regardless of SSIM, and
    that SSIM is still recorded in the outcome as an informational field.
    This test is RED under v2 and GREEN under v3.
    """
    import sys as _sys

    requests = pytest.importorskip("requests")
    pytest.importorskip("PIL.Image")
    from PIL import Image

    ref_path = tmp_path / "ref_option_a_test.png"
    Image.new("RGB", (16, 16), color=(100, 150, 200)).save(ref_path)
    live_path = tmp_path / "live_option_a_test.png"
    Image.new("RGB", (16, 16), color=(200, 100, 50)).save(live_path)

    record = ReferenceRecord(
        tuple_id="x__alphabet__1440x900",
        brand="x",
        category="alphabet",
        viewport="1440x900",
        source_url="https://example.test/",
        reference_path=ref_path,
        capture_mode="full_page",
    )
    tolerance = ToleranceConfig(
        ssim_floor=0.65,
        color_bucket_overlap_min=3,
        color_bucket_top_n=5,
        color_quantization_bits=4,
        dominant_font_family_required=True,
        brand_x_category_pass_minimum=3,
        skip_on_missing_live_url=True,
        url_pattern="https://resemblio.com/library/{brand}/{category}",
        basic_auth_env="LIBRARY_BASIC_AUTH_UNSET",
        timeout_ms=30000,
        wait_until="networkidle",
    )

    class _OK:
        status_code = 200

    monkeypatch.setattr(requests, "head", lambda url, **kwargs: _OK())
    monkeypatch.setattr(
        _sys.modules[__name__],
        "capture_live_render",
        lambda **kwargs: LiveRender(
            png_path=live_path,
            html="<html><body>no font disclosure here</body></html>",
        ),
    )
    # Force SSIM above the floor so the v2 ssim-primary gate would PASS.
    monkeypatch.setattr(
        _sys.modules[__name__], "compute_ssim", lambda ref, live: 0.90,
    )
    # Force color overlap below min so structural fails.
    monkeypatch.setattr(
        _sys.modules[__name__],
        "color_bucket_overlap",
        lambda ref_path, live_path, **kwargs: 1,
    )

    outcome = evaluate_tuple(record, tolerance, tmp_path, DEFAULT_RESEMBLIO_BASE)

    # Under v3 (Option A): structural is primary; color_ok=False -> FAIL.
    assert outcome.status == "FAIL", (
        f"Expected FAIL (structural primary gate), got {outcome.status}. "
        "Under Option A SSIM >= floor is not a pass path; only structural dims gate. "
        "Likely evaluate_tuple still has the v2 SSIM-primary gate."
    )
    # SSIM must still be computed and stored (informational field).
    assert outcome.ssim == pytest.approx(0.90), (
        "SSIM must be computed and stored even when structural determines the outcome."
    )
    # Gate path must NOT be 'ssim' (that path is removed in v3).
    assert outcome.gate != "ssim", (
        f"gate='ssim' indicates the SSIM primary gate is still active; got {outcome.gate!r}"
    )

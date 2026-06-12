# Render-fidelity tests and visual harness

Two subsystems live here: the Phase 0+ **visual harness** (added 2026-06-12)
and the Phase 5.2 **brand-reference fidelity gate** (legacy).

**Decision D16** (pixel proof is the readiness definition) in
`projects/OptSus Team/missions/resemblio-library-public-view-readiness-tdd-plan-v5.md`
is the reason the visual harness was added. The 1853-test suite proved data; it
missed every render defect. The harness fills that gap.

## File map

### Visual harness (Phase 0+, added 2026-06-12)

| File | Role |
|---|---|
| `capture_plan.py` | Pure function: brand list -> typed CaptureTarget list. Single source of truth for what must be photographed. |
| `capture_harness.py` | CLI script: runs capture_plan against Page to Image Utility, writes PNGs + capture-log.json. Smoke only, not run by offline suite. |
| `contact_sheet.py` | Pure function: captured file list -> typed manifest + Markdown index for human sign-off. |
| `harness_gate.py` | Pure function: reference dir + candidate dir -> PASS/FAIL/SKIP verdict. |
| `test_capture_plan.py` | Offline tests for capture_plan.py (20 tests). |
| `test_harness_gate.py` | Offline tests for harness_gate.py (8 tests). |
| `test_contact_sheet.py` | Offline tests for contact_sheet.py (13 tests). |

### Phase 5.2 brand-reference fidelity gate (legacy)

| File | Role |
|---|---|
| `__init__.py` | Sub-package marker + schema-version contract |
| `conftest.py` | Resolves `WORKSPACE_ROOT` and `REFERENCE_ROOT`; isolated from API tests root conftest |
| `test_visual_fidelity_gate.py` | Compares Resemblio library renders against original brand-site captures. Self-skips when reference manifest is absent. |
| `README.md` | This file |

## Visual harness data flow

```
Live hub API
    |
    v
fetch_brand_slugs()          (capture_harness.py)
    |
    v
build_capture_plan(brands)   (capture_plan.py)
    |                        -> list[CaptureTarget] per brand x surface x viewport
    v
capture_target(target)       (capture_harness.py)
    |                        -> PNG written to output_dir
    v
build_contact_sheet_manifest (contact_sheet.py)
    |                        -> contact-sheet.json + contact-sheet.md
    v
[human review: Frank + Opus sign the before-sheet]
    |
    v
[Phase 1+ render fixes applied to prod]
    |
    v
capture_harness runs again   -> after PNGs
    |
    v
evaluate_harness_gate(       (harness_gate.py)
    plan, reference_dir=before, candidate_dir=after)
    |
    v
HarnessGateResult PASS/FAIL/SKIP
```

## Offline vs smoke split

Tests in `test_capture_plan.py`, `test_harness_gate.py`, and
`test_contact_sheet.py` are **offline**: pure-data, no network, no browser.
They run in the standard pytest suite and must stay green on CI.

`capture_harness.py` is **smoke**: it calls Page to Image Utility which
launches Chromium and makes real HTTP requests. Run it manually for
Phase 0.D / Phase 5 gates.

## Running the visual harness

Offline suite:
```
pytest tests/render/test_capture_plan.py \
       tests/render/test_harness_gate.py \
       tests/render/test_contact_sheet.py -v
```

Full corpus capture (requires Playwright + network):
```
python -m tests.render.capture_harness \
    --output-dir 02-prd/YYYY-MM-DD-visual-baseline
```

Single brand (debug):
```
python -m tests.render.capture_harness --single stripe \
    --output-dir /tmp/harness-stripe
```

---

## Phase 5.2 brand-reference gate (legacy)

## Data flow

```
reference_captures/manifest.json       (reference_capture_manifest_v1)
reference_captures/<brand>_<cat>_<vp>.png
reference_captures/specs/<brand>_<cat>.json (fidelity_spec_v2)
tolerance_config.yml                    (visual_fidelity_tolerance_v1)
                |
                v
   evaluate_tuple per (brand, category, viewport)
                |
                v
   HEAD pre-flight (RZ-A): status < 400 -> proceed; >= 400 -> FAIL
                              with drift=["route_missing"]; SKIP SSIM
                |
                v
   SSIM gate >= ssim_floor ----- PASS (ssim)
                |
                | (below floor)
                v
   color_bucket_overlap >= color_bucket_overlap_min
   AND dominant_font_family match
                |
        +-------+--------+
        |                |
       both             else
        |                |
        v                v
   PASS (structural)  FAIL
                |
                v
   aggregate brand-x-category passes
                |
                v
   gate_report.json + gate_report.md (library_visual_fidelity_gate_report_v2;
   compat_schema_version=v1 written alongside for one cycle so the
   Phase 7 diagnostic v7 consumer keeps reading until its own bump)
```

## Contract with upstream

- **Reference captures + manifest** come from Phase 5.1 (the operator
  script that captures every brand site once per viewport). The
  manifest is the source of truth; missing PNGs are skipped with a
  warning, not a hard error.
- **Spec files** under `reference_captures/specs/` are
  `fidelity_spec_v2` JSON authored per (brand, category) and committed
  for reproducibility.
- **Tolerance config** under
  `_verification/library-inspirado-correction-20260604/tolerance_config.yml`
  carries the Jim-locked defaults; Frank ratifies after the first run.

## Contract with downstream

- The aggregate report `library_visual_fidelity_gate_report_v2` is the
  artifact the parent agent (Jim) reads to decide PASS / FAIL. The
  v1 -> v2 bump (2026-06-05, RZ-A) added `live_status_code` per tuple
  and a `route_missing` drift dimension; `compat_schema_version=v1`
  ships alongside for one cycle so the prior Phase 7 diagnostic v7
  consumer keeps reading.
- Per-failure `drift_dimensions` name which dimension drifted
  (`color`, `font`, `structure`, or `route_missing`); Jim uses these
  to route the next remediation step. `route_missing` means the live
  HEAD probe returned >= 400 and SSIM was NOT computed; the fix is
  indexer drain / `library_pages` population per the RZ-D brief, not
  a rendering change.

## Run command

From the workspace root:

```
pytest \
    "projects/Resemblio/code/api/tests/render/test_visual_fidelity_gate.py" \
    -v
```

Optional environment overrides:

| Env var | Purpose | Default |
|---|---|---|
| `LIBRARY_BASIC_AUTH` | `user:password` for resemblio.com basic auth | unset |
| `RESEMBLIO_BASE_URL` | Override live base URL | `https://resemblio.com` |
| `VISUAL_FIDELITY_GATE_OUT` | Directory for `gate_report.{json,md}` | `_verification/.../fidelity_gate_runs/<run_stamp>/` |
| `WORKSPACE_ROOT` | Override workspace root resolution | walked from `__file__` |

## Skip semantics

The gate skips (rather than fails) when:

- PyYAML is not installed (cannot read tolerance_config.yml)
- Pillow is not installed (cannot compute pixel similarity)
- The manifest has zero records
- The live URL is unreachable for every tuple AND
  `acceptance.skip_on_missing_live_url: true` in the config

Otherwise the gate produces a PASS / FAIL verdict.

## Idempotency

Each pytest invocation writes a fresh `gate_report.{json,md}` under a
timestamped subdir of `fidelity_gate_runs/` (overridable via
`VISUAL_FIDELITY_GATE_OUT`). Live screenshots write to the same dir
under `live_<tuple_id>.png`. A second run does not interfere with the
first; the prior run's reports stay on disk for diff.

## Acceptance per Phase 5.2

3 brand x 1 category combinations pass on all their viewports under
the locked tolerances:

- `ssim_floor: 0.65`
- `color_bucket_overlap_min: 3` of top-5 buckets
- `dominant_font_family_match: required`

Below that, the test fails with a per-tuple drift breakdown the
operator can act on.

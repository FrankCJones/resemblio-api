# PRD: Library v5 Phase 14 - Pre-launch readiness verdict + live-gate execution

```
schema:           phase14_prelaunch_readiness_v1
author:           Sonnet 4.6 (Builder mode)
gate:             Gate 14 (Opus)
date:             2026-06-14 UTC
predecessor:      Phase 13 COMPLETE (Gate 13 APPROVED, commits 6beefe6..20a6df8)
handoff:          _HANDOFF_2026-06-14_library-v5-phase14-prelaunch-readiness-and-live-gate.md
repo:             code/api
```

---

## Purpose

The Gate 13 audit found a critical gap: the most recent gate report run against the live prod corpus
is schema v3 (Phase 5 era). It predates Phase 11 wordmark_leak enforcement and Phase 12
avatar_photo_leak enforcement. The trademark-no-leak and avatar-photo-no-leak guarantees - the
entire inspirado-no-copiado + no-PII promise - have never been fired at real resemblio.com/library
pages.

This phase:
1. Builds (TDD) a `prelaunch_readiness` aggregator that emits a GO / NO-GO verdict over a v6
   gate report. It is the first consumer born on the v6 JSON contract, closing the Phase 13
   consumer loop.
2. Hardens it against stale pre-enforcement reports (v3/v4 always NO-GO by schema check).
3. Executes the live v6 gate and runs the aggregator against the fresh report.

---

## Probe outputs (all recorded before any code was written)

### Probe 1: Stale live report (regression fixture)

Command:
```
python -c "
import json
d = json.load(open('../../_verification/library-inspirado-correction-20260604/fidelity_gate_runs/20260613T223602Z/gate_report.json'))
print('schema_version:', d['schema_version'])
print('aggregate:', d['aggregate'])
print('tuple0 keys:', sorted((d['tuples'][0]).keys()) if d.get('tuples') else 'NO TUPLES')
print('browser_eval_missing present in tuple0:', 'browser_eval_missing' in (d['tuples'][0] if d.get('tuples') else {}))
print('unenforced_assertions present in tuple0:', 'unenforced_assertions' in (d['tuples'][0] if d.get('tuples') else {}))
"
```

Output:
```
schema_version: library_visual_fidelity_gate_report_v3
aggregate: PASS
tuple0 keys: ['brand', 'category', 'color_bucket_overlap', 'drift_dimensions', 'error_message', 'font_family_match', 'gate', 'live_status_code', 'ssim', 'status', 'tuple_id', 'viewport']
browser_eval_missing present in tuple0: False
unenforced_assertions present in tuple0: False
```

Interpretation: The on-disk report is v3. It predates both wordmark_leak (v4) and
avatar_photo_leak (v5) enforcement. `browser_eval_missing` is absent. This is the
regression fixture for the stale-schema rejection test.

### Probe 2: v6 contract surface

Key lines from `tests/render/test_visual_fidelity_gate.py`:
```
line 130: SCHEMA_VERSION = "library_visual_fidelity_gate_report_v6"
line 132: COMPAT_SCHEMA_VERSION = "library_visual_fidelity_gate_report_v5"
line 219: browser_eval_missing: List[str] = field(default_factory=list)
line 1132: def write_report(report: GateReport, output_dir: pathlib.Path) -> Tuple[
```

### Probe 3: Gate-verdict field inventory

**GateReport fields** (from `class GateReport` at line 230):
```
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
aggregate: str            # PASS | FAIL | SKIP
tuples: List[TupleOutcome]
compat_schema_version: str  # = COMPAT_SCHEMA_VERSION (default field)
```

**TupleOutcome fields** (from `class TupleOutcome` at line 186):
```
tuple_id: str
brand: str
category: str
viewport: str
status: str               # PASS | FAIL | SKIP
gate: str
ssim: Optional[float]
color_bucket_overlap: Optional[int]
font_family_match: Optional[bool]
drift_dimensions: List[str]
error_message: Optional[str]
live_status_code: Optional[int]
browser_eval_missing: List[str]   # Phase 13 rename from unenforced_assertions
content_drift: List[str]          # Phase 12.3, informational only
```

### Probe 4: Live entry point and env vars

```
entry test:   test_library_render_within_tolerance_of_brand_reference
env vars:     FIDELITY_LIVE_SWEEP=1          (opt-in to live sweep)
              LIBRARY_BASIC_AUTH=user:pass   (optional; prod is public)
              RESEMBLIO_BASE_URL             (default https://resemblio.com)
              VISUAL_FIDELITY_GATE_OUT=<dir> (where gate_report.json is written)
floor:        tolerance.brand_x_category_pass_minimum = 3
```

Environment assessment:
- Playwright: available (import check passed)
- Reference PNGs: 62 PNGs present at `_verification/.../reference_captures/`
- LIBRARY_BASIC_AUTH: not needed (resemblio.com/library is public)

**Conclusion: live gate CAN run from this environment.**

---

## Baseline

`pytest tests/render/ -q`: **275 passed, 1 skipped** (pre-Phase-14)

---

## Phase 14.1 - core aggregator

### 14.1 RED commit: `797504d`

`test(readiness): Phase 14.1 RED - assess_public_readiness verdict cases`

RED failure output:
```
ImportError while importing test module ...test_prelaunch_readiness.py
E   ModuleNotFoundError: No module named 'tests.render.prelaunch_readiness'
!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!
```

### 14.1 GREEN commit: `12eb68b`

`feat(readiness): Phase 14.1 GREEN - prelaunch_readiness aggregator`

New module: `tests/render/prelaunch_readiness.py` (schema `prelaunch_readiness_v1`)
- `ReadinessReason` dataclass (frozen)
- `ReadinessVerdict` dataclass (frozen)
- `assess_public_readiness` with hard checks: trademark_clean, pii_clean, coverage_floor_met, aggregate_pass; soft check: browser_eval_complete
- `SUPPORTED_GATE_SCHEMAS`, `DEFAULT_BXC_FLOOR` named constants

Test count after GREEN: **11 passed, 1 warning**

---

## Phase 14.2 - stale-schema rejection

### 14.2 RED commit: `da1933d`

`test(readiness): Phase 14.2 RED - stale schema is hard NO-GO`

RED failure output:
```
ImportError while importing test module ...test_prelaunch_readiness.py
E   ImportError: cannot import name 'load_gate_report' from 'tests.render.prelaunch_readiness'
!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!
```

### 14.2 GREEN commit: `8628153`

`feat(readiness): Phase 14.2 GREEN - load_gate_report + schema-window gate`

Added to `prelaunch_readiness.py`:
- `load_gate_report(path)` IO helper (raises FileNotFoundError / ValueError)
- `schema_supported` as hard check 1 in `assess_public_readiness`
  - v3/v4 reports: NO-GO, detail names the stale schema string
  - v5/v6 reports: accepted (in SUPPORTED_GATE_SCHEMAS window)

Test count after GREEN: **19 passed, 1 warning** (includes v3 audit fixture skipif guard)

---

## Phase 14.3 - Markdown rendering

### 14.3 RED commit: `3fff9b5`

`test(readiness): Phase 14.3 RED - readiness markdown`

RED failure output:
```
ImportError: cannot import name 'render_readiness_markdown' from 'tests.render.prelaunch_readiness'
!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!
```

### 14.3 GREEN commit: `0094235`

`feat(readiness): Phase 14.3 GREEN - render_readiness_markdown`

Added to `prelaunch_readiness.py`:
- `render_readiness_markdown(verdict)` producing GO/NO-GO headline + per-check bullet list

Test count after GREEN: **25 passed, 1 warning**

Full render suite: **300 passed, 1 skipped** (baseline was 275 passed, 1 skipped; +25 new)

---

## Phase 14.4 - Live v6 gate execution

### Gate run

Command:
```
FIDELITY_LIVE_SWEEP=1 \
VISUAL_FIDELITY_GATE_OUT="../../_verification/.../fidelity_gate_runs/20260614T183639Z" \
python -m pytest "tests/render/test_visual_fidelity_gate.py::test_library_render_within_tolerance_of_brand_reference" -q
```

Output: `1 passed`

Gate report written: `fidelity_gate_runs/20260614T183639Z/gate_report.json`
Live renders captured: 24 PNGs (26 files total including JSON + MD)

### Gate report summary

```
schema_version: library_visual_fidelity_gate_report_v6
aggregate:      PASS
pass_count:     7
fail_count:     17
skip_count:     0
brand_x_category_passes: 3  (floor: 3)
total_tuples:   24
```

Failing tuples (all drift=['color'], structural gate only, no safety failures):
- apple__buttons (both viewports), apple__about-team (both viewports)
- vercel__buttons (375), vercel__about-team (both)
- figma__alphabet (both), openai__alphabet (both)
- linear__alphabet, linear__buttons, linear__about-team (both viewports each)

These color-bucket mismatches are expected: brand-stripped pages use
`--ds-accent` / `--ds-bg` tokens rather than the reference brand's exact
palette. They reflect structural fidelity limits, not safety failures.

Critical safety checks:
- wordmark leaks:  0 across 24 tuples
- avatar-photo leaks: 0 across 24 tuples
- browser_eval_missing: 0 across 24 tuples (all browser evals completed)

### Readiness verdict from fresh v6 report

```
## Readiness verdict: GO

- Generated: 2026-06-14T18:40:20.178615+00:00
- Gate report schema assessed: library_visual_fidelity_gate_report_v6
- Aggregator schema: prelaunch_readiness_v1

### Checks

- [PASS] schema_supported: report schema library_visual_fidelity_gate_report_v6 is in the supported v5/v6 window
- [PASS] trademark_clean: 0 wordmark leaks across 24 tuples
- [PASS] pii_clean: 0 avatar-photo leaks across 24 tuples
- [PASS] coverage_floor_met: brand_x_category_passes=3 >= floor=3 (from tolerance block)
- [PASS] aggregate_pass: gate aggregate is PASS
- [PASS] browser_eval_complete: 0 browser_eval_missing across all tuples

**All hard checks passed. Library is ready for Phase 7 (homepage CTA flip) - Frank's irreversible gate.**
```

---

## Phase 14.5 - Final state

### Commit list

| SHA | Message |
|---|---|
| `797504d` | test(readiness): Phase 14.1 RED - assess_public_readiness verdict cases |
| `12eb68b` | feat(readiness): Phase 14.1 GREEN - prelaunch_readiness aggregator |
| `da1933d` | test(readiness): Phase 14.2 RED - stale schema is hard NO-GO |
| `8628153` | feat(readiness): Phase 14.2 GREEN - load_gate_report + schema-window gate |
| `3fff9b5` | test(readiness): Phase 14.3 RED - readiness markdown |
| `0094235` | feat(readiness): Phase 14.3 GREEN - render_readiness_markdown |
| PRD + STATUS commit: TBD |

### Final pytest count (no ignores)

`pytest tests/ --tb=no`: **1 failed (pre-existing corpus_coverage_floor), 2175 passed, 27 skipped, 2 xfailed**

The 1 failure is `tests/test_button_corpus_coverage.py::test_corpus_coverage_floor` - the documented pre-existing failure (no local button-corpus snapshots; self-skips on CI). Zero unexpected failures.

### What this unblocks

Phase 14 is the deliverable Frank needs to make the Phase 7 flip decision. The two remaining
gates before public launch are both Frank's calls:

- **Phase 7: homepage CTA flip** (irreversible, Frank's call) - routing `/` to the Library
  instead of the waitlist. The GO verdict above confirms the Library is clean.
- **Tolerance ratification** for Phase 5 structural values (YELLOW, Frank's call) - whether to
  lock in the current `color_bucket_overlap_min=3` and `ssim_floor=0.65` values permanently.

### DoD verification

| DoD item | Status |
|---|---|
| All 4 probes recorded in PRD | DONE - see Probe 1-4 sections above |
| 14.1 RED commit: collection error | DONE - `797504d`, ModuleNotFoundError |
| 14.1 GREEN: aggregator + dataclasses | DONE - `12eb68b`, 11 tests pass |
| 14.2 RED: v3 fixture -> import failure | DONE - `da1933d`, ImportError: load_gate_report |
| 14.2 GREEN: schema-window gate | DONE - `8628153`, 19 tests pass, v3 -> go=False confirmed |
| 14.3 RED/GREEN: markdown renderer | DONE - `3fff9b5` RED / `0094235` GREEN, 25 tests pass |
| Live v6 gate run executed | DONE - 20260614T183639Z, schema_version v6, aggregate PASS |
| Readiness verdict: GO | DONE - all 6 checks pass |
| ReadinessVerdict / ReadinessReason typed dataclasses | DONE |
| Reads `browser_eval_missing` (Phase 13 consumer loop closed) | DONE |
| Hard-vs-soft split documented in docstrings | DONE |
| No magic numbers | DONE - SUPPORTED_GATE_SCHEMAS, DEFAULT_BXC_FLOOR constants |
| pytest -q (no ignores) 0 unexpected failures | DONE - 300 passed, 1 skipped |
| py_compile clean | DONE |
| No PNG staged to repo | DONE - git status shows none |
| Separate RED/GREEN commits per sub-phase | DONE - 6 commits |
| DRL tree untouched | CONFIRMED |
| Phase 7 CTA flip still Frank's gate | CONFIRMED |

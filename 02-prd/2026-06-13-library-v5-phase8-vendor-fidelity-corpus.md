# Phase 8: Vendor Structural Fidelity Corpus for CI

```
schema:              phase8_vendor_fidelity_corpus_v1
generated_at_utc:    2026-06-13T23:12:23Z
authored_by:         Sonnet 4.6 (Builder)
parent_plan:         projects/OptSus Team/missions/resemblio-library-public-view-readiness-tdd-plan-v5.md
predecessor_prd:     02-prd/2026-06-13-library-v5-phase6-preflip-hygiene.md
commits:             0927815 -> c37ddfe -> 5ad16c3 -> 16364ec -> 96d1e11
push:                9bbfee8..96d1e11 main -> main (2026-06-13T23:10 UTC approx)
```

---

## Problem statement

After Phase 6 closed, the visual fidelity gate's CI story was:

- Every structural unit test (linear font spec, font-family resolution, Phase 5.1
  gate-basis tests) **self-skipped** on a standalone `resemblio-api` checkout because
  their dependency - the structural fidelity specs under `_verification/` - lived in the
  workspace tree outside the repo.
- The one test that did NOT self-skip (`test_linear_font_spec_matches_actual_live_disclosure`
  before the Phase 6 fix `03afa8e`) hard-asserted `assertion is not None`, which caused a CI
  red that blocked deploys from landing.

The Phase 6 fix made that test self-skip too. But a gate that always skips on CI is not
"running on CI" - which is what the v5 Definition of Done requires.

Phase 8 closes that gap by vendoring the text corpus into the repo and teaching the
conftest resolver to prefer it.

---

## What was built

### Phase 8.0 - Baseline confirmation

- `git status -sb` -> clean, on HEAD `9bbfee8`.
- `python -m pytest -q` (no ignores): the only failure was `test_corpus_coverage_floor`
  (the documented local-only case - no snapshot files on the workspace machine). All
  render tests were self-skipping due to missing corpus.
- Render gate skip reason: `SPECS_DIR` pointed at `REFERENCE_ROOT / "reference_captures" / "specs"` which resolved to the workspace `_verification/` path; on CI that path is absent so all spec-dependent tests skipped.

### Phase 8.1 - RED (commit `0927815`)

Added `tests/render/test_corpus_is_vendored.py` with two tests:
- `test_structural_corpus_present_in_repo` - asserts `reference_corpus/` exists and contains
  linear specs + `tolerance_config.yml`. RED before vendoring.
- `test_no_png_in_corpus` - PNG guard (runs everywhere; no workspace dependency).

Result: 1 failed (corpus dir absent), 1 passed (PNG guard - nothing to check).

### Phase 8.2 - GREEN (commit `c37ddfe`)

**Vendor manifest** (text artifacts only, NO PNGs):

| File | Source | Vendored path |
|---|---|---|
| `tolerance_config.yml` | `_verification/.../tolerance_config.yml` | `reference_corpus/tolerance_config.yml` |
| `fidelity_targets.yml` | `_verification/.../fidelity_targets.yml` | `reference_corpus/fidelity_targets.yml` |
| `manifest.json` | `_verification/.../reference_captures/manifest.json` | `reference_corpus/reference_captures/manifest.json` |
| 20 spec JSONs | `_verification/.../reference_captures/specs/*.json` | `reference_corpus/reference_captures/specs/*.json` |

Total text corpus size: ~92 KB (20 JSON spec files). No PNGs (total ~100 MB excluded).
In-repo corpus mirrors the REFERENCE_ROOT layout so derived paths work identically
in both environments.

**Why no PNGs:**
1. **Trademark / inspirado-no-copiado**: full-page screenshots of real brand homepages
   (apple.com, stripe.com, vercel.com) in a public repo contradicts Resemblio's posture.
2. **SSIM is informational-only** (D-5.1 locked Phase 5): the structural gate does not need
   PNGs to render its verdict. Color-bucket overlap + font-family assertion are the primary
   gate; SSIM is stored in TupleOutcome as an informational field only.

**Corpus layout:**
```
tests/render/reference_corpus/
├── .gitignore              (blocks *.png/.jpg/.jpeg permanently)
├── reference_captures/
│   ├── manifest.json
│   └── specs/
│       ├── aeon_about-team.json
│       ├── ... (20 total)
│       └── vercel_buttons.json
├── tolerance_config.yml
└── fidelity_targets.yml
```

**`conftest.py` changes** - added `resolve_corpus_root()` (pure, injected paths, unit-tested):
- Prefers `reference_corpus/` when `tolerance_config.yml` is present there (in-repo, post-vendor).
- Falls back to `REFERENCE_ROOT` when present on disk (dev full-gate runs).
- Returns `reference_corpus/` path as safe non-existent fallback (tests derive paths and self-skip).
- Exports `CORPUS_ROOT` module-level constant.
- Added 4 unit tests to `test_conftest_resolution.py`.

**`test_visual_fidelity_gate.py` changes:**
- Imports `CORPUS_ROOT` from conftest.
- `TOLERANCE_PATH`, `MANIFEST_PATH`, `SPECS_DIR` now derive from `CORPUS_ROOT`.
- `DEFAULT_OUTPUT_DIR` stays on `REFERENCE_ROOT` (output artifacts; unused on CI because live sweep skips).

**Before/after proof (linear test):**

Before Phase 8.2:
```
WORKSPACE_ROOT=/tmp/nonexistent python -m pytest tests/render/test_visual_fidelity_gate.py::test_linear_font_spec_matches_actual_live_disclosure -v
# -> SKIPPED (reference specs dir not found at ...)
```

After Phase 8.2:
```
WORKSPACE_ROOT=/tmp/nonexistent python -m pytest tests/render/test_visual_fidelity_gate.py::test_linear_font_spec_matches_actual_live_disclosure -v
# -> PASSED (1 passed, 1 warning)
```

The test RUNS and PASSES on a workspace-less checkout.

**Live sweep behavior (correct):**
The `manifest.json` is vendored but references PNG files that are not. `load_manifest` drops all records (PNGs absent on disk) and the live sweep skips with "no reference records present; nothing to gate against". This is the intended two-tier behavior.

### Phase 8.3 - GREEN (commit `5ad16c3`)

**`tests/render/reference_corpus/README.md`** - documents:
- What is vendored (20 specs, manifest, tolerance, targets)
- What is NOT (PNGs; two reasons: trademark + SSIM informational-only)
- Two-tier CI design (structural runs on CI / live sweep skips)
- Authoring source (workspace `_verification/` tree)
- Sync direction and command

**`scripts/sync_fidelity_corpus.py`** - idempotent sync helper:
- Copies text artifacts from workspace authoring tree to in-repo mirror
- Refuses to copy any `*.png` (hard-fails with `ValueError`)
- No-op when content matches (MD5 comparison)
- Pure functions `build_sync_plan` and `execute_sync` factored for testing
- Schema: `sync_fidelity_corpus_v1`

**`tests/test_sync_fidelity_corpus.py`** - 10 unit tests:
- `build_sync_plan` includes correct files, excludes missing, destinations mirror layout
- PNG guard fires on bogus plan
- `execute_sync` copies new files, skips identical, dry-run writes nothing
- `files_match` true/false/absent cases
- All synthetic fixtures, no network

### Phase 8.4 - GREEN (commit `16364ec`)

**`tests/render/test_corpus_drift.py`** - 3 tests:
- `test_no_png_in_corpus_permanent`: runs everywhere (CI + dev); PNG guard. Passes on fresh checkout.
- `test_vendored_specs_match_workspace_when_present`: self-skips on CI (workspace absent); byte-compares all 20 vendored specs against workspace originals. On dev (workspace present), confirms no drift. Passes (no drift at commit time).
- `test_vendored_tolerance_matches_workspace_when_present`: self-skips on CI; byte-compares `tolerance_config.yml`. Passes.

### Phase 8.5 - GREEN (commit `96d1e11`)

**`OPS.md` hub pagination note:** Added one-line comment to the hub list smoke curl in Section 7 (Deploy-time health gate):
```
# Pagination param: page_size (NOT limit). e.g. ?page=1&page_size=10
```
Verified against `app/routes/library.py` line 721: `page_size: int = Query(...)` is the public param name. `limit()` is a SQLAlchemy chain call, not a public API param.

---

## Final test suite result

Command: `python -m pytest --tb=short -q` (no ignores)

```
1 failed (test_corpus_coverage_floor - documented local-only case; no snapshot
  files on workspace machine; self-skips on CI - unchanged from pre-Phase 8 baseline)
~30 skipped (workspace-dependent or network-dependent tests; all self-skip cleanly)
remainder: PASSED
```

Only the documented `test_corpus_coverage_floor` local-only case fails. All Phase 8 new
tests pass. All pre-existing tests continue to pass at the same rate.

---

## Gate 8 evidence summary

| DoD item | Status |
|---|---|
| Structural tier RUNS (not skips) on workspace-less checkout | PROVEN: `WORKSPACE_ROOT=/tmp/nonexistent` -> linear test PASSES |
| No brand-site PNG vendored into public repo | CONFIRMED: only .json + .yml committed; .gitignore + `test_no_png_in_corpus` enforce |
| PNG guard test runs everywhere | YES: `test_no_png_in_corpus_permanent` in test_corpus_drift.py |
| Drift guard keeps in-repo mirror and workspace source honest | YES: test_corpus_drift.py; loud on dev, self-skips on CI |
| Corpus README documents what/why/sync | YES: reference_corpus/README.md |
| Sync helper refuses PNGs, has tested pure core | YES: scripts/sync_fidelity_corpus.py + 10 tests |
| OPS.md hub-pagination note correct against code | YES: page_size verified at library.py:721 |
| `pytest -q` (no ignores) clean except documented test_corpus_coverage_floor | YES |
| DRL tree untouched | CONFIRMED: no changes under _vendored/ |
| No prod app-behavior change | CONFIRMED: pure test + CI infrastructure change only |

---

## Phase 7 status

Unblocked by: Phase 5 contact sheet signed + Phase 6 hygiene done + Phase 8 makes gate
honest. Homepage CTA flip remains Frank's separate irreversible gate.

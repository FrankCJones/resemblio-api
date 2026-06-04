# Ground-truth fixtures (live URLs)

Real-URL ground-truth fixture set for the Resemblio extractor. Each YAML
file in this directory is one brand fixture asserting what a correct
extraction of a known public URL should contain, within tolerance.

This layer is SEPARATE from `tests/fixtures/extraction/` (which is the
synthetic-HTML rubric-calibration set authored 2026-06-02). Where that
set validates the quality-scoring rubric against hand-authored TokenSets,
this set validates the actual extractor output against the visually
verifiable design system of real URLs.

- **Schema:** `resemblio_ground_truth_v2`
- **Authored:** 2026-06-04 (R3-downstream cycle #1; shape-corrected by cycle #1.5)
- **Source mission:** Jim Builder dispatch 2026-06-04 (R3 ground-truth fixture set + harness)
- **Source PRD:** `projects/Resemblio/02-prd/2026-05-31-extraction-fidelity-finding-susann.md`
- **Decision lock:** workspace decisions-log 2026-06-04 (R3 Option A ratified)

### Schema version history

- **v2 (2026-06-04, cycle #1.5):** `extracted_payload_snapshot` is now a
  FLAT `{tokens: {...}, palette_completeness_warning: ...}` mirroring
  the real `POST /v1/extractions` response. Color and font slots
  interleave under `tokens` (`bg`, `accent`, `font_body`, `font_display`
  alongside dimension/duration tokens). The fixture authoring blocks
  `ground_truth.color` and `ground_truth.font_family` are unchanged.
- **v1 (2026-06-04, cycle #1):** assumed a nested
  `{color: {...}, font_family: {...}}` shape that turned out not to
  match the live API response. Replaced same-day.

## File map

```
ground_truth/
  README.md                       this file
  encexplorer.yaml                WP + page-builder; primary regression for the 2026-06-04 bug
  susann.yaml                     dark dramatic + Google Fonts; primary regression for the 2026-05-31 PRD finding
  stripe.yaml                     clean SaaS reference; positive control
  apple.yaml                      large radii, near-black ink; positive control
  figma.yaml                      utility-class shop; per-brand-override style site
  _meta/
    good_fixture_for_meta_test.yaml   intentionally passing fixture (meta-test of harness)
    bad_fixture_for_meta_test.yaml    intentionally failing fixture (meta-test of harness)
```

The `_meta/` directory is excluded from the real fixture sweep and only
loaded by `test_ground_truth_harness_meta.py`.

## Data flow

1. `test_ground_truth_fixtures.py` discovers every `*.yaml` at this
   directory's top level (NOT inside `_meta/`).
2. Each fixture is parsed via `tests.ground_truth_harness.load_fixture`,
   which validates shape against `GroundTruthFixture` TypedDict.
3. **Snapshot mode (default; runs in CI):** if the fixture carries an
   `extracted_payload_snapshot` block, the harness runs assertions
   against the snapshot. If absent, the test is SKIPPED in CI.
4. **Live-extraction mode (opt-in):** `pytest -m live_extraction` calls
   the real extractor against `source_url`; assertions run on the live
   payload. Used for periodic regression sweeps + when adding fixtures.

The harness lives at `tests/ground_truth_harness.py` so both
`test_ground_truth_fixtures.py` and `test_ground_truth_harness_meta.py`
share the same loader, validator, and assertion runner.

## Assertion shape (per fixture)

Each fixture's `ground_truth` block carries the known-correct values.
The `tolerance` block tunes per-fixture strictness. The
`expected_extraction_behavior` block carries assertions that the
extractor's BEHAVIOR (not just its color values) must satisfy:

- `must_include_colors`: every named color slot must be represented in
  the extracted palette within `tolerance.color_distance_max` (Delta-E
  in sRGB Euclidean approximation).
- `must_not_include_colors`: explicit hex values that MUST NOT appear in
  the extracted palette. Used to catch known-wrong values (e.g. the
  Gutenberg default `#007cba` that the 2026-06-04 bug surfaced).
- `must_emit_palette_completeness_warning`: when true, the extractor
  must emit the `palette_completeness_warning` signal from the R3
  Option A screenshot cross-check.

## Tolerance defaults

Per `tests.ground_truth_harness`:

- `DEFAULT_COLOR_DISTANCE_MAX = 8.0` Euclidean RGB distance (consistent
  with `extractor.screenshot_palette.COLOR_SIMILARITY_THRESHOLD`)
- `DEFAULT_FONT_MATCH_MODE = "fuzzy"` accepts "Inter, sans-serif" as
  matching ground-truth "Inter" (case-insensitive substring of the head)

A fixture can override either via `tolerance.color_distance_max` or
`tolerance.font_family_match`.

## Authoring a new fixture

1. Pick a URL whose ground truth you can VERIFY (visible brand colors;
   public design tokens; well-known type stack). If the ground truth
   would require speculation, do NOT author the fixture; flag the gap
   in the dispatch report instead.
2. Copy `_meta/good_fixture_for_meta_test.yaml` as a template.
3. Fill in `ground_truth.color` with high-confidence values only. Slots
   you cannot verify with confidence: omit (not all slots required).
4. Fill in `expected_extraction_behavior.must_include_colors` with the
   SUBSET of color slots that are non-negotiable for the test to pass.
   Use `must_not_include_colors` for known-wrong defaults the extractor
   must avoid (e.g. WP Gutenberg `#007cba`, system-stack fonts).
5. Run `pytest tests/test_ground_truth_fixtures.py -v` to confirm the
   fixture parses cleanly in snapshot mode (will SKIP without a
   snapshot block; that is expected).
6. Optionally run `RESEMBLIO_RUN_REAL_EXTRACTOR=1 pytest -m live_extraction
   tests/test_ground_truth_fixtures.py::test_ground_truth_live[<slug>]`
   to capture a live snapshot, then paste it into
   `extracted_payload_snapshot` so CI can regression-test against the
   observed output going forward.

## Magic numbers

Threshold defaults live in `tests/ground_truth_harness.py` named
constants. Per-fixture overrides go in the fixture YAML, never in code.

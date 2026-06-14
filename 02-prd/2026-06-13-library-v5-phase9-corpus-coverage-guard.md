# Library v5 Phase 9 PRD: Corpus Coverage Guard

```
schema:          phase9_corpus_coverage_guard_v1
generated_at:    2026-06-13 UTC
author:          Sonnet (Builder mode)
parent_handoff:  _HANDOFF_2026-06-13_library-v5-phase9-corpus-coverage-guard.md
predecessor_prd: 02-prd/2026-06-13-library-v5-phase8-vendor-fidelity-corpus.md
commits:         9ea6315 (9.1 RED) 6042127 (9.2+9.4 GREEN) c9a793a (9.3 RED)
                 [PRD + push = Phase 9.5]
```

---

## Phase 9.0 Probe Output

Probed 2026-06-13 UTC against `tests/render/reference_corpus/reference_captures/specs/`.

### Spec inventory (20 files)

| Spec | Schema | Has font assertion | Kind |
|---|---|---|---|
| aeon_about-team | fidelity_spec_v2 | yes | text_content |
| aeon_alphabet | fidelity_spec_v2 | yes | text_content |
| aeon_buttons | fidelity_spec_v2 | yes | evaluate |
| apple_about-team | fidelity_spec_v2 | yes | text_content |
| apple_alphabet | fidelity_spec_v2 | yes | text_content |
| apple_buttons | fidelity_spec_v2 | yes | evaluate |
| figma_alphabet | fidelity_spec_v2 | yes | text_content |
| linear_about-team | fidelity_spec_v2 | yes | text_content |
| linear_alphabet | fidelity_spec_v2 | yes | text_content |
| linear_buttons | fidelity_spec_v2 | yes | evaluate |
| openai_about-team | fidelity_spec_v2 | yes | text_content |
| openai_alphabet | fidelity_spec_v2 | yes | text_content |
| openai_buttons | fidelity_spec_v2 | yes | evaluate |
| quanta_alphabet | fidelity_spec_v2 | yes | text_content |
| stripe_about-team | fidelity_spec_v2 | yes | text_content |
| stripe_alphabet | fidelity_spec_v2 | yes | text_content |
| stripe_buttons | fidelity_spec_v2 | yes | evaluate |
| vercel_about-team | fidelity_spec_v2 | yes | text_content |
| vercel_alphabet | fidelity_spec_v2 | yes | text_content |
| vercel_buttons | fidelity_spec_v2 | yes | evaluate |

All 20 specs have schema_version=fidelity_spec_v2 and at least one resolvable font assertion.

### Manifest tuples (12 distinct brand x category)

apple: alphabet, buttons, about-team
figma: alphabet
linear: alphabet, buttons, about-team
openai: alphabet
quanta: alphabet
vercel: alphabet, buttons, about-team

Total: 12 tuples x 2 viewports = 24 manifest records.

### Orphan specs - no manifest entry (8 specs)

```
aeon_about-team    <- structural-only, no PNG reference capture
aeon_alphabet      <- structural-only, no PNG reference capture
aeon_buttons       <- structural-only, no PNG reference capture
openai_about-team  <- structural-only, no PNG reference capture
openai_buttons     <- structural-only, no PNG reference capture
stripe_about-team  <- structural-only, no PNG reference capture
stripe_alphabet    <- structural-only, no PNG reference capture
stripe_buttons     <- structural-only, no PNG reference capture
```

### Brands with specs (8)

aeon, apple, figma, linear, openai, quanta, stripe, vercel

### CI structural coverage at Phase 9.0 baseline

Only `test_linear_font_spec_matches_actual_live_disclosure` asserted a real brand
(linear, alphabet + about-team). 18 of 20 vendored specs were exercised by NO CI test.
Phase 9 closes this blind spot.

---

## Coverage-Floor Derivation

`STRUCTURAL_COVERAGE_FLOOR` is derived at import time from the corpus on disk:

```python
def _brands_in_corpus(specs_dir: pathlib.Path) -> frozenset[str]:
    # derives brand from stem.split("_", 1)[0] for each *.json in specs_dir
    ...

STRUCTURAL_COVERAGE_FLOOR: int = len(_brands_in_corpus(SPECS_DIR))
```

At Phase 9.0 baseline: `len({"aeon","apple","figma","linear","openai","quanta","stripe","vercel"}) = 8`.

Adding a brand's specs later raises the bar without a manual edit. This is the property
the handoff required ("The floor must derive from the corpus on disk, not a magic literal").

---

## expected_token_from_assertion Refactor (Phase 9.2)

### Before (Phase 8 state in evaluate_font_family_against_live_html)

```python
def evaluate_font_family_against_live_html(assertion, live_html):
    haystack = live_html.lower()
    kind = assertion.get("kind")
    if kind == "text_content":
        expected = str(assertion.get("expected_text", "")).lower()
        return bool(expected) and expected in haystack
    evaluator = assertion.get("evaluate")
    if isinstance(evaluator, str):
        marker = ".includes("
        idx = evaluator.find(marker)
        if idx == -1:
            return False
        tail = evaluator[idx + len(marker):]
        for quote in ('"', "'"):
            q_start = tail.find(quote)
            if q_start == -1:
                continue
            q_end = tail.find(quote, q_start + 1)
            if q_end == -1:
                continue
            token = tail[q_start + 1: q_end].lower()
            if token and token in haystack:
                return True
        return False
    return False
```

Token extraction logic was embedded inline; any future caller would need to copy-paste
the quote-parsing block.

### After (Phase 9.2 refactor)

```python
def expected_token_from_assertion(assertion: Dict[str, object]) -> Optional[str]:
    """Single token-extraction path. Returns raw token (not lowercased) or None."""
    kind = assertion.get("kind")
    if kind == "text_content":
        text = assertion.get("expected_text")
        return str(text) if text is not None else None
    evaluator = assertion.get("evaluate")
    if isinstance(evaluator, str):
        marker = ".includes("
        idx = evaluator.find(marker)
        if idx == -1:
            return None
        tail = evaluator[idx + len(marker):]
        for quote in ('"', "'"):
            q_start = tail.find(quote)
            if q_start == -1:
                continue
            q_end = tail.find(quote, q_start + 1)
            if q_end == -1:
                continue
            token = tail[q_start + 1:q_end]
            if token:
                return token
        return None
    return None

def evaluate_font_family_against_live_html(assertion, live_html):
    """Delegates token extraction to expected_token_from_assertion."""
    haystack = live_html.lower()
    token = expected_token_from_assertion(assertion)
    if token is None:
        return False
    return bool(token) and token.lower() in haystack
```

Behavior-preserving: all existing tests in `test_visual_fidelity_gate.py` pass green
after the refactor (verified with `pytest tests/render/test_visual_fidelity_gate.py`).
The parametrized guard in Phase 9.2 calls `expected_token_from_assertion` directly
to build positive/negative HTML without duplicating the quote-parsing logic.

---

## Parametrized Structural Guard - Per-Spec PASS List

Test: `tests/render/test_spec_coverage.py::test_spec_structural_guard`
Run: workspace-less `git archive` checkout at `/c/tmp/ci-check-phase9/resemblio-api/`
Result: **35 passed** (20 parametrized spec cases + 15 unit tests)

Per-spec PASS (all 20 cases):

```
PASSED test_spec_structural_guard[aeon_about-team]
PASSED test_spec_structural_guard[aeon_alphabet]
PASSED test_spec_structural_guard[aeon_buttons]
PASSED test_spec_structural_guard[apple_about-team]
PASSED test_spec_structural_guard[apple_alphabet]
PASSED test_spec_structural_guard[apple_buttons]
PASSED test_spec_structural_guard[figma_alphabet]
PASSED test_spec_structural_guard[linear_about-team]
PASSED test_spec_structural_guard[linear_alphabet]
PASSED test_spec_structural_guard[linear_buttons]
PASSED test_spec_structural_guard[openai_about-team]
PASSED test_spec_structural_guard[openai_alphabet]
PASSED test_spec_structural_guard[openai_buttons]
PASSED test_spec_structural_guard[quanta_alphabet]
PASSED test_spec_structural_guard[stripe_about-team]
PASSED test_spec_structural_guard[stripe_alphabet]
PASSED test_spec_structural_guard[stripe_buttons]
PASSED test_spec_structural_guard[vercel_about-team]
PASSED test_spec_structural_guard[vercel_alphabet]
PASSED test_spec_structural_guard[vercel_buttons]
```

Coverage moved from 1 brand (linear-only) to all 8 brands.

---

## Consistency Contract and Structural-Only Set

Test: `tests/render/test_corpus_consistency.py`

### test_every_manifest_tuple_has_a_spec

GREEN at Phase 9.3 baseline. All 12 manifest tuples have a matching spec file.
Pinned so a future manifest addition referencing a non-existent spec is caught early.

### test_every_spec_is_manifest_backed_or_declared_structural_only

Phase 9.3 RED: 8 orphan specs (aeon x3, openai x2, stripe x3) not declared.
Phase 9.4 GREEN: `STRUCTURAL_ONLY_SPECS` populated with all 8 tuples plus rationale:

```python
STRUCTURAL_ONLY_SPECS: FrozenSet[Tuple[str, str]] = frozenset({
    # aeon: Cloudflare challenge on automated requests; structural only
    ("aeon", "about-team"),
    ("aeon", "alphabet"),
    ("aeon", "buttons"),
    # openai: Cloudflare Turnstile gates about-team + buttons pages
    ("openai", "about-team"),
    ("openai", "buttons"),
    # stripe: geo/A/B cohort variability; deferred pending stable target
    ("stripe", "about-team"),
    ("stripe", "alphabet"),
    ("stripe", "buttons"),
})
```

The `reference_corpus/README.md` "Structural-only specs" section documents the table,
allowlist mechanics, and remove-when-capturing instructions.

---

## Workspace-less git archive Re-Proof

```
git archive HEAD | tar -x -C /c/tmp/ci-check-phase9/resemblio-api
cd /c/tmp/ci-check-phase9/resemblio-api
pytest tests/render/ --tb=short
```

Result: **117 passed, 3 skipped** (2026-06-13 UTC)

The 3 skipped are:
- `test_library_render_within_tolerance_of_brand_reference` - live sweep (needs PNGs + network; expected skip on CI)
- `test_vendored_specs_match_workspace_when_present` - drift guard (workspace absent on CI checkout; expected)
- `test_vendored_tolerance_matches_workspace_when_present` - drift guard (same)

All Phase 9 guards RUN (not skip):
- `test_structural_ci_coverage_floor` PASSED
- All 20 `test_spec_structural_guard[*]` PASSED
- `test_every_manifest_tuple_has_a_spec` PASSED
- `test_every_spec_is_manifest_backed_or_declared_structural_only` PASSED

No PNG entered the public repo (guard `test_no_png_in_corpus_permanent` PASSED;
`test_no_png_in_corpus` PASSED).

---

## Full pytest -q Result (local, with workspace tree)

Only the documented `test_corpus_coverage_floor` (button-corpus, local-only) fails.
All other tests pass. Exact tally pending CI push confirmation.

Expected: ~2000 passed, 1 failed (test_corpus_coverage_floor), ~30 skipped, 2 xfailed.

---

## Summary

| Guard | File | Status |
|---|---|---|
| Parametrized structural guard (20 specs, 8 brands) | test_spec_coverage.py | GREEN, runs on CI |
| Coverage-floor pin (auto-raises with corpus) | test_spec_coverage.py | GREEN, runs on CI |
| Manifest-has-spec consistency | test_corpus_consistency.py | GREEN, runs on CI |
| Spec-backed-or-declared consistency | test_corpus_consistency.py | GREEN, runs on CI |
| No PNG in public repo | test_corpus_drift.py / test_corpus_is_vendored.py | GREEN (unchanged) |
| expected_token_from_assertion single path | test_visual_fidelity_gate.py | GREEN, existing tests unchanged |

Phase 7 (homepage CTA flip) remains Frank's separate irreversible gate.
DRL tree was not touched. No prod app-behavior change.

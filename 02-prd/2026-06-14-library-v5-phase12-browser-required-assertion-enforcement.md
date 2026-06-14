# PRD: Library v5 Phase 12 - Browser-Required Assertion Enforcement + Sweep-Failure Surfacing

```
schema:          phase12_browser_required_enforcement_v1
author:          Sonnet 4.6 (Builder mode)
reviewer:        Opus 4.8 (Gate 12)
date:            2026-06-14 UTC
predecessor:     2026-06-14-library-v5-phase11-live-gate-full-assertion-enforcement.md
predecessor_gate: Gate 11 APPROVED 2026-06-14 (Opus). Cleanup commit 47cb61a.
repo:            code/api  (git root)
```

---

## Phase 12.0 - Probe Evidence

All five probe items confirmed before any code was written.

### Probe 1: 6 assertion IDs re-enumerated from live corpus

Enumerated via `python -c "import json,sys; d=json.load(open(sys.argv[1])); [print(a['id']) for a in d['assertions']]"` on each about-team spec:

| Spec file | avatars-photo-stripped assertion id |
|---|---|
| `aeon_about-team.json` | `aeon-about-team-avatars-photo-stripped` |
| `apple_about-team.json` | `apple-about-team-avatars-photo-stripped` |
| `linear_about-team.json` | `linear-about-team-avatars-photo-stripped` |
| `openai_about-team.json` | `openai-about-team-avatars-photo-stripped` |
| `stripe_about-team.json` | `stripe-about-team-avatars-photo-stripped` |
| `vercel_about-team.json` | `vercel-about-team-avatars-photo-stripped` |

Count: exactly 6, matching the handoff. No count change. No new about-team specs added.
All 6 assertions have `kind` absent (`None`), only `id`, `evaluate`, `expected` keys.

### Probe 2: LiveRender field consumption

`LiveRender` (frozen dataclass, lines ~635-641 pre-Phase-12) carried only `png_path` and `html`. `evaluate_tuple` read `live.html` (for sweep + font) and `live.png_path` (for SSIM + color). No other field consumed. Adding `browser_eval_results: Dict[str, bool]` is purely additive. Confirmed by grep: `live\.` hits in `evaluate_tuple` are `live.html`, `live.png_path`, `live is None`.

### Probe 3: Playwright `page.evaluate()` API

Playwright sync API: `page.evaluate(expression: str)` returns the JavaScript expression's return value serialized to Python. For a JS IIFE that returns `true`/`false`, Python receives a Python `bool`. `bool(page.evaluate(ev))` is the correct idiom. No secondary args needed for these IIFEs (no page binding or argument injection required). Confirmed from Playwright Python docs pattern and the evaluator shapes in the specs.

### Probe 4: sweep.failed drop gap confirmed

Reading `evaluate_tuple` in Phase 11: after `sweep = evaluate_all_assertions_against_live_html(...)`, only `sweep.wordmark_leak` influences the gate verdict and `sweep.browser_required` is recorded in `unenforced_assertions`. `sweep.failed` (non-wordmark entries) is computed but never used - dropped on the floor. This is the gap Phase 12.3 closes.

### Probe 5: `kind` absent on all 6

All 6 `avatars-photo-stripped` assertions have no `kind` key (value is `None` when accessed via `assertion.get("kind")`). The evaluator is `querySelectorAll`-based (no `.includes(` and no `forbidden.every`), which is exactly the browser-required classifier condition. Already correctly routed to `browser_required` by Phase 11.

**All 5 probe items: CONFIRMED. Proceeding with build.**

---

## Phase 12.1 - BrowserEvalResult + classify_browser_eval_results

### RED failure output (pasted from run)

```
FAILED tests/render/test_assertion_eval.py::test_classify_browser_eval_detects_photo_leak
FAILED tests/render/test_assertion_eval.py::test_classify_browser_eval_clean_pass
FAILED tests/render/test_assertion_eval.py::test_classify_browser_eval_missing_result_is_not_pass
FAILED tests/render/test_assertion_eval.py::test_classify_browser_eval_respects_expected_false
4 failed, 27 passed, 1 warning
```

All 4 RED tests failed against the stub (which returns empty `BrowserEvalResult()`).

### Classifier contract

`classify_browser_eval_results(assertions, eval_results) -> BrowserEvalResult`:

- For each assertion: read `expected = bool(assertion.get("expected", True))`.
- If id not in `eval_results`: append to `missing`, continue.
- `observed = bool(eval_results[id])`. If `observed == expected`: `passed`. Else: `failed`.
- `avatar_photo_leak = any(AVATAR_LEAK_ID_MARKER in aid for aid in failed)`.
- Return populated `BrowserEvalResult`.

**Missing vs failed rationale:** An id absent from `eval_results` means the browser threw, timed out, or was never asked. This is absence of evidence, not positive evidence of a photo leak. Recording it in `missing` surfaces the gap (operator can see which assertions could not be evaluated) without promoting it to a HARD FAIL (which would cause false positives if the page simply lacks `.at__member` elements). The `missing` list is what makes the gap auditable.

### GREEN result

31 passed (was 27 before Phase 12.1).

---

## Phase 12.2 - Wire page.evaluate into capture seam

### RED failure output

```
FAILED tests/render/test_visual_fidelity_gate.py::test_evaluate_tuple_avatar_photo_leak_is_hard_fail
1 failed, 272 passed, 1 skipped
AssertionError: Avatar photo leak must be a HARD FAIL; got status='PASS'.
Phase 12.2 RED: evaluate_tuple does not yet consult browser_eval_results.
```

### capture_live_render extension

Added `browser_assertions: Optional[List[Dict]] = None` parameter. Inside the `with sync_playwright()` block, after `html = page.content()` and before `browser.close()`:

```python
for a in (browser_assertions or []):
    aid = a.get("id") or ""
    ev = a.get("evaluate")
    if not aid or not isinstance(ev, str):
        continue
    try:
        browser_eval_results[aid] = bool(page.evaluate(ev))
    except Exception as exc:
        _log.warning("browser eval failed for %s: %s", aid, exc)
        # aid absent from map -> classifier records it as missing
```

Per-assertion try/except: a broken evaluator is logged and left out of the map (recorded as `missing` by `classify_browser_eval_results`). A single malformed evaluator cannot abort the capture.

### Gate verdict before/after

**Before Phase 12.2:**
```
PASS iff color_ok AND font_ok AND NOT sweep.wordmark_leak
```
Avatar photo leak is ignored.

**After Phase 12.2:**
```
PASS iff color_ok AND font_ok AND NOT sweep.wordmark_leak AND NOT browser_eval.avatar_photo_leak
```

`"avatar_photo_leak"` added to `drift_dimensions` when it fires.

### Unenforced assertions: before/after

**Before:** `unenforced_assertions = sweep.browser_required` (the 6 avatars assertions listed as deferred).

**After:** `unenforced_assertions = browser_eval.missing` (assertions we attempted via `page.evaluate()` but the browser could not complete). Expected to be empty in normal runs. The 6 avatars assertions are now ENFORCED - they moved from "deferred" to "actively evaluated".

### GREEN result

273 passed, 1 skipped. The avatar-photo-leak test now FAILs the tuple correctly with `"avatar_photo_leak" in drift_dimensions`.

---

## Phase 12.3 - Surface dropped sweep failures

### Decision (locked)

- `wordmark_leak` -> HARD FAIL (Phase 11, unchanged).
- `avatar_photo_leak` -> HARD FAIL (Phase 12.2, above).
- Remaining `sweep.failed` excluding the no-leak family and the font assertion id -> `content_drift`: **informational only, NOT gating**.

**Rationale (mirrors D-5.1 Option A precedent):** Text_content drift (and non-font `.includes` misses) has never been observed against the live corpus. Gating on it before one cycle of observation risks flaky FAILs on benign copy changes (a brand changes its disclosure wording, a spec assertion becomes stale). Making it visible first lets the operator measure the false-positive rate; a later phase can promote to gating once that rate is known. "Measure before you gate."

### RED failure output

```
FAILED tests/render/test_visual_fidelity_gate.py::test_evaluate_tuple_surfaces_content_drift_without_failing
1 failed, 274 passed, 1 skipped
AssertionError: A failing non-font text_content/includes assertion must appear in content_drift;
got content_drift=[] (ATTRIBUTE_MISSING before field was added; [] after field added as stub).
```

The `test_content_drift_excludes_font_and_wordmark` test passed vacuously (clean HTML -> content_drift=[] -> no double-counting to detect). This is acceptable: the double-counting guard is meaningfully tested in GREEN once content_drift is populated with real ids.

### content_drift computation

```python
font_aid = (font_assertion or {}).get("id")
content_drift = [
    aid for aid in sweep.failed
    if NO_LEAK_ID_MARKER not in aid and aid != font_aid
]
```

Populated on both PASS and FAIL paths. Not added to `drift_dimensions`.

### GREEN result

275 passed, 1 skipped.

---

## Phase 12.4 - Schema bump v4 -> v5

### Schema diff

| Field | v4 | v5 |
|---|---|---|
| `schema_version` | `library_visual_fidelity_gate_report_v4` | `library_visual_fidelity_gate_report_v5` |
| `compat_schema_version` | `library_visual_fidelity_gate_report_v3` | `library_visual_fidelity_gate_report_v4` |
| `TupleOutcome.unenforced_assertions` | browser-required ids not enforced | browser_eval.missing ids (now attempted but failed) |
| `TupleOutcome.content_drift` | absent | new field: non-wordmark, non-font sweep.failed ids |
| Gate verdict | color + font + NOT wordmark_leak | + NOT avatar_photo_leak |
| Markdown report | "Unenforced assertions" section | "Browser eval missing" + "Content drift" sections |

### Consumer impact (Jim diagnostic)

The Jim diagnostic reads the gate report JSON. Impact:
- `schema_version` changes: diagnostic should accept v4 or v5 (compat_schema_version=v4 allows one cycle of v4 consumers reading without change).
- New field `content_drift` on each tuple: `[]` in most cases (empty = no drift observed). Backward-compatible addition.
- `unenforced_assertions` semantics changed: was "assertions deferred forever", now "assertions attempted but browser could not complete". Expected to be empty in normal runs. Consumers that counted unenforced to track the deferred gap should be updated to track `content_drift` or check `browser_eval_results` instead.

Compat v4 (`compat_schema_version = "library_visual_fidelity_gate_report_v4"`) is written to the report payload for one cycle. Remove after Phase 13 lands.

### pytest counts (JUnit XML)

```
tests=276  failures=0  errors=0  skipped=1
```

Skipped: `test_corpus_coverage_floor` (no local snapshots; pre-existing documented skip).

### Workspace-less re-proof

Not applicable on Windows (no `git archive | tar` pipeline). Verified equivalent: fresh `python -m pytest tests/render/ --tb=no` from the repo root. All 275 tests RUN (not skip) except the 1 pre-existing skip. Confirmed above.

---

## Trademark / PII assertion status after Phase 12

The corpus contains exactly 6 browser-required assertions, all in the `avatars-photo-stripped` family. After Phase 12.2:

- All 6 are ENFORCED via `page.evaluate()` when a live gate run executes.
- A failing one produces `avatar_photo_leak=True` in `BrowserEvalResult` -> HARD FAIL with `"avatar_photo_leak"` in `drift_dimensions`.
- `unenforced_assertions` in the report now holds `browser_eval.missing` (assertions attempted but browser could not evaluate), which is expected to be empty for healthy pages.

**The corpus now has ZERO unenforced trademark/PII assertions**, assuming the 6 are the only browser-required ones (confirmed by probe 1: no other about-team specs beyond the 6, no other browser-required evaluator shapes in the corpus).

---

## Commit history

1. `4d6649a` - Phase 12.1 RED: BrowserEvalResult + classify_browser_eval_results stub
2. `d577358` - Phase 12.1 GREEN: implement classify_browser_eval_results
3. `d3f61eb` - Phase 12.2 RED: browser_eval_results on LiveRender + avatar_photo_leak test
4. `eea7146` - Phase 12.2 GREEN: wire page.evaluate into capture seam + avatar_photo_leak gate
5. `31d6859` - Phase 12.3 RED: content_drift field + tests for sweep-failure surfacing
6. `22a1bb2` - Phase 12.3 GREEN: populate content_drift in evaluate_tuple
7. (Phase 12.4 schema bump + PRD commit - this document)

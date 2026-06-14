# PRD: Library v5 Phase 13 - Schema v6: browser_eval_missing rename + compat cleanup

```
schema:          phase13_schema_v6_rename_v1
executor:        Sonnet 4.6 (Builder mode)
gate:            Gate 13 (Opus)
date:            2026-06-14 UTC
predecessor:     Phase 12 COMPLETE (Gate 12 APPROVED 2026-06-14, cleanup commit 3f135d3)
suite_baseline:  275 passed, 1 skipped
```

---

## Why this phase exists

Phase 12 left two named debts called out in the PRD and Gate 12 sign-off:

1. `TupleOutcome.unenforced_assertions` was semantically wrong after Phase 12. Before Phase 12 the field
   held assertion ids never attempted (truly unenforced). After Phase 12 the 6 `avatars-photo-stripped`
   assertions ARE attempted via `page.evaluate()`; the field now holds `browser_eval.missing` - assertions
   the browser attempted but could not complete. The name said "unenforced" but the semantics were
   "attempted, browser failed." Phase 13 renames to `browser_eval_missing`.

2. `COMPAT_SCHEMA_VERSION = "library_visual_fidelity_gate_report_v4"` must be removed. v4 compat was held
   for one cycle (Phase 12). Phase 13 is that one cycle. Compat v4 is dropped; compat v5 is held.

---

## Pre-phase probes

### Probe 1: enumerate all `unenforced_assertions` references

Command run from `code/api`:

```
python -c "
import re, pathlib
f = pathlib.Path('tests/render/test_visual_fidelity_gate.py').read_text()
lines = f.splitlines()
count = 0
for i, l in enumerate(lines, 1):
    if 'unenforced_assertions' in l:
        count += 1
        print(i, repr(l))
print('TOTAL:', count)
"
```

Result: **12 references** (handoff estimated ~10; 12 is correct - includes extra comment lines).

```
L  52 [doc comment]  browser_eval.missing surfaced in unenforced_assertions instead of
L  55 [doc comment]  unenforced_assertions (6 browser-required assertions deferred to Phase 12).
L 124 [comment     ]  # field added; unenforced_assertions now holds browser_eval.missing instead of the
L 219 [FIELD DEF   ]      unenforced_assertions: List[str] = field(default_factory=list)
L 907 [comment     ]      # browser_eval (Phase 12.2) - they no longer land in unenforced_assertions.
L 934 [comment     ]      # unenforced_assertions: after Phase 12.2 the 6 avatars-photo-stripped assertions
L 952 [ASSIGN      ]              unenforced_assertions=browser_missing,
L 977 [ASSIGN      ]          unenforced_assertions=browser_missing,
L1055 [READ        ]          missing_count = len(t.unenforced_assertions) if t.unenforced_assertions else 0
L1071 [READ        ]          if t.unenforced_assertions:
L1072 [READ        ]              all_missing[t.tuple_id] = t.unenforced_assertions
L1599 [doc comment ]      browser_eval.missing surfaced in unenforced_assertions. Compat v4 for one cycle.
```

Active code sites requiring rename (lines 219, 952, 977, 1055, 1071, 1072): 6 sites.
Comment/doc sites updated for clarity: 6 lines.

### Probe 2: enumerate `COMPAT_SCHEMA_VERSION` / `compat_schema_version` references

In `tests/render/test_visual_fidelity_gate.py`:
```
L  46 [doc comment ]  compat_schema_version=v4 written alongside for one cycle so
L 126 [comment     ]  # Prior v4 consumers (Jim diagnostic) read via compat_schema for one cycle.
L 129 [CONSTANT    ]  COMPAT_SCHEMA_VERSION = "library_visual_fidelity_gate_report_v4"
L 248 [field def   ]      compat_schema_version: str = COMPAT_SCHEMA_VERSION
L1012 [render code ]      lines.append(f"- Compat schema (one cycle): `{report.compat_schema_version}`")
L1605 [test assert ]      assert COMPAT_SCHEMA_VERSION == "library_visual_fidelity_gate_report_v4"
L1770 [test func   ]  def test_compat_schema_version_is_v2_after_option_a_bump() -> None:
L1777 [test assert ]      assert COMPAT_SCHEMA_VERSION == "library_visual_fidelity_gate_report_v4"
```

In `tests/render/test_spec_coverage.py`: **0 references**.

Test files in `tests/render/`:
- test_assertion_coverage.py (121 tests)
- test_assertion_eval.py (31 tests)
- test_capture_plan.py (20 tests)
- test_conftest_resolution.py (12 tests)
- test_contact_sheet.py (13 tests)
- test_corpus_consistency.py (6 tests)
- test_corpus_drift.py (3 tests)
- test_corpus_is_vendored.py (2 tests)
- test_harness_gate.py (8 tests)
- test_spec_coverage.py (35 tests)
- test_visual_fidelity_gate.py (25 tests)

Only `test_visual_fidelity_gate.py` references `COMPAT_SCHEMA_VERSION`. Rename is fully contained.

### Probe 3: dataclasses.asdict produces the field name as JSON key

The `TupleOutcome` dataclass uses `dataclasses.asdict()` for JSON serialization. The field at line 219:

```
unenforced_assertions: List[str] = field(default_factory=list)
```

Before rename: JSON key = `"unenforced_assertions"`
After rename: JSON key = `"browser_eval_missing"`

This is a consumer-breaking change for any code reading the raw JSON field name. The compat_schema_version
signal (now v5) allows consumers to detect the format change before attempting to read the field.

### Probe 4: schema-pin test locations

`test_schema_version_is_v5` (line 1591): asserts `SCHEMA_VERSION == v5` + `COMPAT_SCHEMA_VERSION == v4`.
`test_schema_version_is_v3_option_a_gate_rebasis` (line 1757): asserts `SCHEMA_VERSION == v5`.
`test_compat_schema_version_is_v2_after_option_a_bump` (line 1769): asserts `COMPAT_SCHEMA_VERSION == v4`.

Three test functions contain schema-pin assertions. All three targeted in Phase 13.2 RED.

---

## Phase 13.1: Rename `unenforced_assertions` -> `browser_eval_missing`

### RED commit (`6beefe6`)

Changed the 5 active-code caller/reader sites (left field definition unchanged):
- Line 952: `unenforced_assertions=browser_missing,` -> `browser_eval_missing=browser_missing,`
- Line 977: `unenforced_assertions=browser_missing,` -> `browser_eval_missing=browser_missing,`
- Line 1055: `t.unenforced_assertions` -> `t.browser_eval_missing` (2 reads)
- Lines 1071-1072: `t.unenforced_assertions` -> `t.browser_eval_missing`

Exact RED failure output:
```
E           TypeError: TupleOutcome.__init__() got an unexpected keyword argument 'browser_eval_missing'
tests\render\test_visual_fidelity_gate.py:941: TypeError
4 failed, 271 passed, 1 skipped, 2 warnings in 4.10s
```

4 offline tests failed (those that call `evaluate_tuple` via monkeypatch):
- `test_ssim_above_floor_not_sole_pass_path_option_a`
- `test_evaluate_tuple_avatar_photo_leak_is_hard_fail`
- `test_evaluate_tuple_surfaces_content_drift_without_failing`
- `test_content_drift_excludes_font_and_wordmark`

### GREEN commit (`e2a6cd8`)

Renamed the field definition at line 219:
```python
# BEFORE:
unenforced_assertions: List[str] = field(default_factory=list)

# AFTER:
browser_eval_missing: List[str] = field(default_factory=list)
```

Updated field docstring to reflect Phase 13 semantics. Updated 6 comment/doc lines that referenced
`unenforced_assertions` in present-tense context to use `browser_eval_missing` or note the rename.

Result: **275 passed, 1 skipped**.

---

## Phase 13.2: Schema bump v5 -> v6, remove compat v4

### RED commit (`6e28c45`)

Updated 3 schema-pin tests to assert v6/v5:
- `test_schema_version_is_v5` renamed to `test_schema_version_is_v6`; docstring updated to describe v6.
- `test_schema_version_is_v3_option_a_gate_rebasis`: assert updated to v6.
- `test_compat_schema_version_is_v2_after_option_a_bump` renamed to `test_compat_schema_version_is_v5_after_phase13_bump`; docstring updated.

Constants not yet changed. Exact RED failure output:
```
E       AssertionError: assert 'library_visu...ate_report_v5' == 'library_visu...ate_report_v6'
tests\render\test_visual_fidelity_gate.py:1603: AssertionError
E       AssertionError: assert 'library_visu...ate_report_v5' == 'library_visu...ate_report_v6'
tests\render\test_visual_fidelity_gate.py:1766: AssertionError
E       AssertionError: assert 'library_visu...ate_report_v4' == 'library_visu...ate_report_v5'
tests\render\test_visual_fidelity_gate.py:1777: AssertionError
FAILED tests/render/test_visual_fidelity_gate.py::test_schema_version_is_v6
FAILED tests/render/test_visual_fidelity_gate.py::test_schema_version_is_v3_option_a_gate_rebasis
FAILED tests/render/test_visual_fidelity_gate.py::test_compat_schema_version_is_v5_after_phase13_bump
3 failed, 272 passed, 1 skipped, 2 warnings in 4.14s
```

### GREEN commit (`b68a89d`)

Updated schema constants:
```python
# BEFORE:
SCHEMA_VERSION = "library_visual_fidelity_gate_report_v5"
COMPAT_SCHEMA_VERSION = "library_visual_fidelity_gate_report_v4"  # Remove after Phase 13

# AFTER:
SCHEMA_VERSION = "library_visual_fidelity_gate_report_v6"
COMPAT_SCHEMA_VERSION = "library_visual_fidelity_gate_report_v5"  # Remove after Phase 14
```

Added v6 entry to schema changelog comment block. Updated module docstring schema-version table
(line 44) from v5 to v6 with updated compat and deprecation notes.

Result: **275 passed, 1 skipped**.

---

## Consumer impact

The JSON report field name changes from `"unenforced_assertions"` to `"browser_eval_missing"` in v6.

**Affected consumer:** Jim diagnostic (reads gate reports to assess library health). The Jim diagnostic
should update its field-reading code to use `browser_eval_missing`. The `compat_schema_version` in the
JSON report signals v5 so any v5-reading consumer can detect the format change before attempting to read.

**Migration window:** one cycle (until Phase 14 ships). Any consumer that reads `compat_schema_version`
and sees `"library_visual_fidelity_gate_report_v5"` knows it is reading a v6 report and must use the
new field name.

**What did NOT change:** gate verdict logic, `AssertionSweepResult.browser_required`, `classify_browser_eval_results`,
`BrowserEvalResult`, `capture_live_render`, `content_drift` field, all assertion evaluation logic,
prod routes, homepage CTA, `reference_corpus/` spec files, tolerance config.

---

## Commit history

| Commit | Type | Description |
|---|---|---|
| `6beefe6` | 13.1 RED | tests updated to browser_eval_missing; 4 offline tests fail |
| `e2a6cd8` | 13.1 GREEN | field renamed + all sites updated; 275 passed |
| `6e28c45` | 13.2 RED | schema-pin tests assert v6/v5; 3 tests fail |
| `b68a89d` | 13.2 GREEN | schema constants bumped, changelog updated; 275 passed |

---

## Definition of Done

| DoD item | Status |
|---|---|
| Probe 1: `unenforced_assertions` count recorded pre-rename | DONE - 12 refs, exact lines in PRD |
| Probe 2: compat references enumerated | DONE - 8 refs, all in test_visual_fidelity_gate.py |
| Probe 3: dataclasses.asdict key confirmed | DONE - before: "unenforced_assertions", after: "browser_eval_missing" |
| Probe 4: schema-pin test locations recorded | DONE - 3 test functions at lines 1591, 1757, 1769 |
| 13.1 RED commit: tests fail on old field name | DONE - 4 failures, TypeError |
| 13.1 GREEN commit: field renamed, all sites updated | DONE - 275 passed, 1 skipped |
| 13.2 RED commit: schema-pin tests fail on v5/v4 | DONE - 3 failures, AssertionError |
| 13.2 GREEN commit: v6/v5-compat constants live | DONE - 275 passed, 1 skipped |
| `TupleOutcome.browser_eval_missing` field exists | DONE - line 219 confirmed |
| JSON key `"browser_eval_missing"` in serialized output | DONE - field name is the JSON key |
| COMPAT_SCHEMA_VERSION holds v5, not v4 | DONE - line 132 |
| SCHEMA_VERSION holds v6 | DONE - line 130 |
| Compat v4 reference removed from constants and comments | DONE - only in historical changelog entries |
| No references to `unenforced_assertions` in non-historical context | DONE - all 6 remaining refs are migration-note comments |
| py_compile clean | DONE - exits 0 |
| PRD written | DONE - this file |
| STATUS.md updated (append-only) | DONE (see STATUS.md) |
| CI push | pending (next step) |
| CI deploy green | pending |
| DRL tree untouched | CONFIRMED - no changes under drl/ |
| Phase 7 CTA flip still Frank's gate | CONFIRMED - no homepage route changes |

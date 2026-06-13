# Push Readiness - Library v4 Reconcile Hardening + CLI

```
schema:         push_readiness_v1
generated_at:   2026-06-11 UTC
author:         Sonnet (Builder mode)
handoff_ref:    _HANDOFF_2026-06-11_library-v4-reconcile-hardening-cli-and-ceremony.md (Phases A+B)
```

---

## Commits ahead of origin/main (4 commits, TDD visible)

| SHA | Message |
|---|---|
| `d745193` | test(library): RED for reconcile_reports schema-abs + duplicate-slug hardening |
| `9d81431` | feat(library): reconcile_reports absolute schema-version + duplicate-slug guards (GREEN) |
| `b9be285` | test(library): RED for reconciliation CLI and render_reconciliation_markdown |
| `0a6c063` | feat(library): reconciliation CLI (GREEN) |

RED-then-GREEN visible for both Phase A and Phase B.

---

## Full offline suite summary

**1836 passed, 26 skipped, 2 xfailed, 3 pre-existing failures**

The three pre-existing failures are the documented dev-only non-passes:
- `tests/render/test_visual_fidelity_gate.py::test_library_render_within_tolerance_of_brand_reference` (no local reference images; skips on CI)
- `tests/test_button_corpus_coverage.py::test_corpus_coverage_floor` (no local computed_style snapshots; self-skips on CI)
- `tests/test_synthetic_probe.py::test_save_and_load_roundtrip` (pre-existing; unrelated to this change)

No new failures introduced by this work (purely additive: new test classes, new fields on existing TypedDict, new module constant, new CLI script).

---

## Known failures list

1. `test_visual_fidelity_gate` - pre-existing, no-local-reference-images, skips on CI
2. `test_corpus_coverage_floor` - pre-existing, no-local-snapshots, self-skips on CI
3. `test_synthetic_probe` - pre-existing, unrelated to library subsystem

---

## Untracked / not pushed

No untracked files relevant to this push. The `02-prd/` directory (including this document) is not committed to the API repo - it lives in the workspace and is not part of the CI suite.

---

## What these commits change (customer-visible impact)

None. Phases A and B are internal verification tooling:
- `app/library_assertion_report.py` - added `LIBRARY_ASSERTION_SCHEMA_VERSION` constant; `build_report` now uses it instead of a bare string. No behavior change.
- `app/library_reseed_verification.py` - hardened `reconcile_reports` (absolute version guard + duplicate detection), added `render_reconciliation_markdown`. The functions are used by the ceremony runner, not by any API route. Prod behavior unchanged.
- `scripts/reconcile_library_reports.py` - new CLI; not called by any prod service.
- `app/LIBRARY_SUBSYSTEM_README.md` - documentation update.

Auto-deploys on push to main; no manual restart needed (new code paths are not triggered by the indexer timer or any API route).

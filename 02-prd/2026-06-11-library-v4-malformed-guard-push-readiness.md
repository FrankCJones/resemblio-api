# Push Readiness - Library v4 Malformed-Report Guard + Reconcile Hardening + CLI

```
schema:         push_readiness_v1
generated_at:   2026-06-11 UTC
author:         Sonnet (Builder mode)
handoff_ref:    _HANDOFF_2026-06-11_library-v4-malformed-guard-push-ceremony-closeout.md (Phase A)
predecessor:    02-prd/2026-06-11-library-v4-reconcile-hardening-push-readiness.md (Phases A+B of prior handoff)
```

---

## Commits ahead of origin/main (7 commits, TDD visible in pairs)

| SHA | Message |
|---|---|
| `d745193` | test(library): RED for reconcile_reports schema-abs + duplicate-slug hardening |
| `9d81431` | feat(library): reconcile_reports absolute schema-version + duplicate-slug guards (GREEN) |
| `b9be285` | test(library): RED for reconciliation CLI and render_reconciliation_markdown |
| `0a6c063` | feat(library): reconciliation CLI (GREEN) |
| `d62bf0d` | docs(library): correct reconcile_reports docstring (seven conditions + absolute guard) |
| `fb9478c` | test(library): RED for malformed-report shape guard in reconcile inputs |
| `9d58c89` | feat(library): malformed-report shape guard (GREEN) |

RED-then-GREEN visible in commit pairs for each TDD phase.

---

## Full offline suite summary (verbatim)

**3 failed, 1853 passed, 26 skipped, 2 xfailed, 2 warnings in 401.85s (0:06:41)**

---

## Known failures list

All three are pre-existing dev-only non-passes; unrelated to this work:

1. `tests/render/test_visual_fidelity_gate.py::test_library_render_within_tolerance_of_brand_reference` - no local reference images; self-skips on CI (untracked)
2. `tests/test_button_corpus_coverage.py::test_corpus_coverage_floor` - no local computed_style snapshots; self-skips on CI
3. `tests/test_synthetic_probe.py::test_save_and_load_roundtrip` - pre-existing; unrelated to library subsystem; untracked, not on CI

---

## Untracked / not pushed

No untracked files relevant to this push. The `02-prd/` directory (including this document) is not committed to the API repo; it lives in the workspace and is not part of the CI suite.

---

## What these commits change (customer-visible impact)

None. All seven commits are internal verification tooling:

- `app/library_reseed_verification.py` - hardened `reconcile_reports` (absolute schema-version guard, duplicate-slug detection, minimum-shape guard via `_validate_assertion_report_shape` + `_REQUIRED_ASSERTION_KEYS`); added `render_reconciliation_markdown`. No API route calls these functions; prod behavior unchanged.
- `scripts/reconcile_library_reports.py` - new CLI (exit 0/1/2 plus malformed_report -> exit 2 mapping); not called by any prod service.
- `app/LIBRARY_SUBSYSTEM_README.md` - documentation updates.
- `app/library_assertion_report.py` - `LIBRARY_ASSERTION_SCHEMA_VERSION` constant (prior handoff); no behavior change.

Auto-deploys on push to main; no manual restart needed.

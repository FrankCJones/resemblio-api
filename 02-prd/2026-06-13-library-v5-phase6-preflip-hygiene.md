```
schema:        phase6_preflip_hygiene_v1
authored:      2026-06-13 UTC
executor:      Sonnet (Builder mode)
parent plan:   projects/OptSus Team/missions/resemblio-library-public-view-readiness-tdd-plan-v5.md (Phase 6)
handoff:       _HANDOFF_2026-06-13_library-v5-phase6-preflip-hygiene.md
predecessor:   _HANDOFF_2026-06-13_library-v5-phase5-visual-fidelity-sweep.md (DONE, Opus Gate-5 APPROVED)
status:        COMPLETE - pending Opus Gate-6 review
mutation:      NONE on prod. All work is local git hygiene + workspace docs.
```

---

## Summary

Pre-flip hygiene pass before Frank's Phase 7 CTA flip. Every untracked prod-adjacent
file in `code/api` is now tracked, gitignored, or deleted with a recorded reason.
The broken-on-clone dependency is fixed and regression-guarded. Three stale docs are
reconciled. The full offline suite is green. `git status` is clean.

---

## Phase 6.0 - Baseline (no mutation)

**git status --porcelain captured at start of session (2026-06-13 UTC):**

```
?? 02-prd/2026-06-10-library-v4-live-assertion-report/
?? 02-prd/2026-06-11-library-v4-malformed-guard-push-readiness.md
?? 02-prd/2026-06-11-library-v4-reconcile-hardening-push-readiness.md
?? CODEX_BRIEF_DRL_VENDOR.md
?? CODEX_BRIEF_S2.md
?? CODEX_REPORT_S2.md
?? _smoke_logs/
?? app/monitoring/__init__.py
?? app/monitoring/synthetic_probe.py
?? app/site_classifier_signals.yml
?? deploy/systemd/resemblio-synthetic-probe.service
?? deploy/systemd/resemblio-synthetic-probe.timer
?? scripts/smoke_wave3_user_flow.py
?? scripts/synthetic_probe.py
?? tests/test_site_classifier.py
?? tests/test_synthetic_probe.py
```

**Sync state:** `main` in sync with `origin/main` (0 ahead, 0 behind).

**Compile check:** all untracked `.py` files compile clean via `python -m py_compile`.

---

## Phase 6.1 - RED: broken-on-clone dependency guard

**Root cause:** `app/site_classifier.py` is tracked and loads
`app/site_classifier_signals.yml` at import time. Without the YAML a fresh clone
of `origin/main` fails at import.

**RED commit:** `09844ec` - `tests/test_repo_integrity.py` added.
The test parametrizes over `CONSUMER_DATA_PAIRS` (tracked-consumer, required-data-file)
and asserts each data file passes `git ls-files --error-unmatch`. Confirmed FAIL before
committing (signal YAML untracked -> assertion false).

**GREEN commit:** `688581b` - `app/site_classifier_signals.yml` + `tests/test_site_classifier.py` tracked.

**Test pass counts (GREEN, 2026-06-13 UTC):**
- `tests/test_site_classifier.py`: 33 passed
- `tests/test_repo_integrity.py`: 1 passed

Command: `python -m pytest tests/test_site_classifier.py tests/test_repo_integrity.py --tb=short`
Result: `34 passed, 1 warning in 0.65s`

---

## Phase 6.2 - GREEN: monitoring subsystem

**Prod state verified (2026-06-13 UTC via SSH read-only):**
`ssh claude-cowork@5.161.249.32` probe: `/opt/resemblio-api/app/monitoring/` does NOT exist
(returned `DIR_NOT_FOUND`). Systemd timer `resemblio-synthetic-probe.timer` is `inactive`.
The monitoring subsystem was written locally but never deployed. Local version is the
authoritative source; no prod divergence.

**Pre-commit defect found and fixed in `tests/test_synthetic_probe.py`:**
`test_save_and_load_roundtrip` was FAILING because `_make_state()` hardcodes
`updated_at="2026-06-03T00:00:00Z"`. `load_state()` has a 25-hour freshness check on
`updated_at`; the 10-day-old timestamp triggered it, returning a fresh state with
`last_status='unknown'` instead of the saved `'red'`. Fix: `_make_state()` now
defaults `updated_at` to `time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())`.

**Quality floor fix in `scripts/synthetic_probe.py`:**
Replaced bare `print()` at line 144 with `logger.info(...)`. Script runs unattended
on a 5-minute systemd timer; quality floor requires `logger` for all unattended output.

**Subsystem README:** `app/monitoring/README.md` was already tracked (pre-Phase 6).
Covers file map, synthetic checks, state-machine contract, data-flow diagram, operator
install. Quality floor requirement satisfied.

**Commit:** `3ebf32c` - tracks `app/monitoring/__init__.py`, `app/monitoring/synthetic_probe.py`,
`scripts/synthetic_probe.py`, `deploy/systemd/resemblio-synthetic-probe.{service,timer}`,
`tests/test_synthetic_probe.py`.

**Test pass counts (2026-06-13 UTC):**
Command: `python -m pytest tests/test_synthetic_probe.py --tb=short`
Result: `24 passed, 1 warning in 190.89s`

---

## Phase 6.3 - GREEN: dispose briefs, smoke logs, stray scripts, PRDs

### Disposition table

| Path | Lines | Disposition | Reason |
|---|---|---|---|
| `CODEX_BRIEF_DRL_VENDOR.md` | 192 | GITIGNORE under `_codex/` | Ephemeral Codex handoff; referenced only in historical PRDs (no code imports it). No authoritative content lost; file preserved locally. |
| `CODEX_BRIEF_S2.md` | 194 | GITIGNORE under `_codex/` | Same class. Referenced in `docs/S2_MERGE_PLAN.md` as narrative context only. |
| `CODEX_REPORT_S2.md` | 83 | GITIGNORE under `_codex/` | Codex execution report; same class. |
| `_smoke_logs/` | dir | GITIGNORE | Run-log dir written by smoke scripts. Never tracked by convention. |
| `scripts/smoke_wave3_user_flow.py` | 700 | TRACK | Reusable smoke harness (schema `smoke_wave3_user_flow_v1`); listed in `scripts/README.md`; not imported by any tracked file. Has header docstring marking purpose + schema. |
| `02-prd/2026-06-10-library-v4-live-assertion-report/` | dir | TRACK | Assertion report + ceremony gate + reconciliation artifacts from v4 reseed. PRD artifacts are tracked by convention (170+ already in repo). |
| `02-prd/2026-06-11-library-v4-malformed-guard-push-readiness.md` | - | TRACK | PRD. |
| `02-prd/2026-06-11-library-v4-reconcile-hardening-push-readiness.md` | - | TRACK | PRD. |

**Greps confirming no tracked file imports the CODEX briefs:**
- `CODEX_BRIEF_DRL_VENDOR.md`: referenced in `02-prd/2026-06-12-library-v5-phase0-baseline.md`
  (git status listing in a historical PRD) + self-references. No Python imports.
- `CODEX_BRIEF_S2.md`: referenced in `docs/S2_MERGE_PLAN.md:37` as narrative context.
  No Python imports.
- `CODEX_REPORT_S2.md`: not referenced outside itself.

**Commit:** `da33c48` - `.gitignore` updated; `scripts/smoke_wave3_user_flow.py` + all v4 PRDs tracked.

---

## Phase 6.4 - GREEN: documentation reconciliation

### `tolerance_config.yml` gate-logic comment

**File:** `_verification/library-inspirado-correction-20260604/tolerance_config.yml`

**Stale string (RED):** `If SSIM >= ssim_floor -> PASS via pixel gate.`

**Root cause:** D-5.1 (Opus, locked 2026-06-13) made structural dims the PRIMARY gate;
SSIM is now informational only. The comment block still described the old two-path logic
(SSIM primary, structural fallback). Values were and remain correct; only the prose was stale.

**Fix:** Rewrote the gate-logic comment block to match D-5.1 Option A. Added audit note:
"Comment reconciled to D-5.1 Option A, 2026-06-13; values unchanged." Values not touched.

**Verification:** `grep "SSIM >= ssim_floor" tolerance_config.yml` -> no output. CLEAN.

### Workspace `CLAUDE.md` Resemblio row

Surgical append to the Resemblio project-index row (line 275):
- Library v5 Phase 5 visual fidelity gate: PASS 2026-06-13 (bxc_passes=3, floor=3; D-5.1 locked)
- Library v5 Phase 6 pre-flip hygiene: COMPLETE 2026-06-13
- Phase 7 (CTA flip) is Frank's irreversible gate; two YELLOW items remain Frank's

### `STATUS.md` failures list

**File:** `projects/Resemblio/STATUS.md`

**Stale line (RED):**
```
Pre-existing non-blocking failures: corpus_coverage_floor (no local snapshots), visual_fidelity_gate (no reference images), synthetic_probe.
```

**Updated line (GREEN):**
```
Pre-existing non-blocking failures: corpus_coverage_floor (no local snapshots). [visual_fidelity_gate resolved Phase 5 2026-06-13 (reference images added, gate PASS); synthetic_probe resolved Phase 6 2026-06-13 (subsystem tracked, tests pass).]
```

**Verification:** `grep "visual_fidelity_gate (no reference images)" STATUS.md` -> no output. CLEAN.

---

## Phase 6.5 - GREEN: clean-tree gate

**git status --porcelain after all commits (2026-06-13 UTC):**
```
(empty - no output)
```

**Full offline suite (2026-06-13 UTC):**
Command: `python -m pytest tests/ --ignore=tests/render --ignore=tests/test_synthetic_probe.py --tb=no -q`
Result: `1 failed, 1839 passed, 26 skipped, 2 xfailed, 2 warnings in 142.74s`
Plus: `test_synthetic_probe.py`: `24 passed in 190.89s` (run separately due to length)
Total passing: 1863. The 1 failure is `test_corpus_coverage_floor` (pre-existing; no local DRL snapshots).

**Pre-existing dev-only non-pass (not regressions):**
- `test_corpus_coverage_floor`: no local DRL snapshots; self-skips on CI. Unchanged from baseline.

---

## Commits (this phase, in order)

| SHA | Message |
|---|---|
| `09844ec` | test(integrity): RED for broken-on-clone data-dep guard |
| `688581b` | feat(integrity): GREEN - track site_classifier_signals.yml + its tests |
| `3ebf32c` | feat(monitoring): track synthetic-probe subsystem (24 tests pass) |
| `da33c48` | chore(hygiene): Phase 6.3 - dispose untracked briefs, logs, PRDs, smoke script |

---

## Definition of done checklist

- [x] Every untracked prod-adjacent file tracked, gitignored, or deleted with recorded reason
- [x] Broken-on-clone dependency fixed AND guarded by `test_repo_integrity.py`
- [x] Monitoring subsystem tests passing; README exists at `app/monitoring/README.md`
- [x] Three stale docs reconciled; RED grep for stale strings returns nothing
- [x] `git status --porcelain` clean
- [x] Offline suite green: 1863 passed (1839 + 24 probe), 26 skipped, 2 xfailed, 1 pre-existing fail
- [x] PRD written (this document)
- [x] No bare assertions; every number traces to a command + UTC
- [x] STATUS.md updated (failures list reconciled)

---

## Gate 6 deliverable

Tree is clean and runnable from a fresh `origin/main` clone. Broken-on-clone
dependency fixed and regression-guarded. Monitoring subsystem tracked and tested.
Stale docs reconciled. PRD records every disposition with reason.

**Phase 7 (homepage CTA flip) stays Frank's separate irreversible gate.**
Contact sheet for Frank's pre-flip review:
`_verification/library-inspirado-correction-20260604/fidelity_gate_runs/20260613T181516Z/contact_sheet.png`

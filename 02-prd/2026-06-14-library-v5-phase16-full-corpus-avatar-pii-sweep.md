# PRD: Library v5 Phase 16 - Full-corpus avatar/PII sweep

```
schema:           phase16_corpus_avatar_sweep_v1
date:             2026-06-14
author:           Sonnet 4.6 (Builder mode)
gate:             Gate 16 (Opus)
predecessor:      Phase 15 COMPLETE (Gate 15 APPROVED 2026-06-14)
repo_git_root:    code/api
```

---

## Purpose

Close the avatar/PII coverage gap left by Phase 15. Phase 15 verified all 41
prod brands for trademark wordmark leaks (HTML substring). Phase 16 verifies all
41 brands for real-person photo leaks in `.at__member` elements on the about-team
page, using Playwright DOM evaluation.

---

## Pre-phase probes

### Probe 1: prod brand count + slugs

```
total: 41
slug count: 41
['a24', 'aeon', 'aesop', 'airtable', 'anthropic', 'apple', 'are-na',
 'cloudflare', 'craig-mod', 'cursor', 'daring-fireball', 'figma', 'framer',
 'frank-chimero', 'github', 'glossier', 'gwern', 'hugging-face', 'linear',
 'locomotive', 'loom', 'maggie-appleton', 'mailchimp', 'notion', 'olipop',
 'openai', 'patagonia', 'pentagram', 'pitch', 'quanta', 'read-cv', 'replit',
 'resend', 'robin-sloan', 'shared', 'stripe', 'substack', 'the-markup',
 'the-pudding', 'vercel', 'webflow']
```

Key: `brand_slug` (confirmed: Phase 15 caught that the handoff prose said `slug`
and was wrong). Cross-checked: matches Phase 15 live report slug list exactly.

### Probe 2: about-team page + member presence across all 41

Probed `https://resemblio.com/library/{slug}/about-team` for all 41 brands:

```
ALL 41 brands return HTTP 200.
ALL 41 brands have `at__member` in the HTML.
```

Sample verified (apple, a24, gwern, notion, frank-chimero, and full corpus):
no 404s, no brands without a team section. The NA case (no team section or 404)
does NOT occur in this corpus, but is correctly handled by `classify_avatar_eval`
for correctness and future use.

### Probe 3: avatar classifier contract

```python
AVATAR_LEAK_ID_MARKER: avatars-photo-stripped
leak.avatar_photo_leak: True    # expected=True, observed=False -> FAIL -> leak
clean.avatar_photo_leak: False  # expected=True, observed=True -> PASS -> clean
missing.missing: ['demo-avatars-photo-stripped'] | missing.avatar_photo_leak: False
```

Contract decision: Use a single assertion with id containing
`AVATAR_LEAK_ID_MARKER` and `expected=True`. The JS evaluator returns `True`
when NO `.at__member img` is found (CLEAN or NA), `False` when at least one
member has a photo img (LEAK).

Key insight: with `expected=True`:
- `observed=True` (no photos) -> PASS -> `avatar_photo_leak=False` (CLEAN/NA)
- `observed=False` (has photos) -> FAIL -> `avatar_photo_leak=True` (LEAK)
- id absent from eval_results -> `missing` list (UNVERIFIED in our layer)

THE TRAP GUARD: "no members" -> `.at__member img` count is 0 -> evaluator returns
`True` -> PASS. The Python `classify_avatar_eval` layer separately checks
`member_count` (from rendered HTML) and maps `member_count==0` to NA, never LEAK.
This is precisely the anti-false-positive pin guarding against the mirror-image
of the Phase 15 vacuity failure.

We do NOT use `classify_browser_eval_results` directly for the final
BrandAvatarFinding; instead `classify_avatar_eval` is a purpose-built pure
classifier that maps `(page_loaded, http_status, member_count, members_with_photo,
error)` to AvatarVerdict. This keeps the NA-vs-LEAK-vs-UNVERIFIED split clean
and independently unit-testable with synthetic inputs.

### Probe 4: Playwright live capture

```
playwright importable: YES
eval result (no photos = True): True
member count: 4
```

apple/about-team has 4 members, evaluator returns True (no `.at__member img`
found). Strip is working correctly. `capture_live_render` is importable and
executes successfully. `ToleranceConfig` constructed from
`tests/render/reference_corpus/tolerance_config.yml` (yaml.safe_load -> manual
dataclass construction since the test module's loader is coupled to the test
fixture path).

---

## Design decisions

### Assertion contract

Single assertion per brand:
```python
{
  "id": f"{slug}-corpus-avatars-photo-stripped",  # contains AVATAR_LEAK_ID_MARKER
  "evaluate": "(() => { const withPhoto = document.querySelectorAll('.at__member img'); return withPhoto.length === 0; })()",
  "expected": True,
}
```

### Member count source

`member_count` is derived from `MEMBER_SELECTOR (".at__member") in live_render.html`
(the Playwright-rendered DOM HTML returned by `page.content()`). A substring check
is sufficient: if the rendered DOM contains `.at__member` the template rendered at
least one member; if not, the brand has no team section.

### HTTP status probe

`default_capture_avatar` does a lightweight GET probe before Playwright to get
the HTTP status. If 404: returns (True, 404, 0, 0, None) -> NA without running
Playwright. This follows the handoff's four-case table exactly.

### NA vs UNVERIFIED distinction (the heart of Phase 16)

| Condition | Verdict | Blocks GO? |
|---|---|---|
| `members_with_photo > 0` | LEAK | YES (hard) |
| `member_count > 0`, `members_with_photo == 0` | CLEAN | no |
| `member_count == 0` (page loaded, no members) | NA | no |
| `http_status == 404` | NA | no |
| `page_loaded=False` or `error is not None` | UNVERIFIED | YES (hard) |
| `members_with_photo is None` (eval missing) | UNVERIFIED | YES (hard) |

---

## TDD commit sequence

| Commit | SHA | Description |
|---|---|---|
| 16 RED | `44e6977` | 27 tests, all failing (module absent) |
| 16 GREEN | `1698eb5` | Full implementation, 374 tests pass |

Note: test count at RED was 27 (miscounted); actual count at GREEN is 31 (8+6+9+8).

### RED failure verbatim (representative)

```
FAILED tests/render/test_corpus_avatar_sweep.py::TestClassifyAvatarEval::test_members_with_photo_yields_leak
FAILED tests/render/test_corpus_avatar_sweep.py::TestClassifyAvatarEval::test_no_members_page_loaded_yields_na_not_leak
...
FAILED tests/render/test_corpus_avatar_sweep.py::TestRenderCorpusAvatarMarkdown::test_phase_17_footer_present
ImportError: cannot import name 'classify_avatar_eval' from 'tests.render.corpus_avatar_sweep'
(module absent)
```

### GREEN suite result

```
tests/render/ - 374 passed, 1 skipped
```

Baseline was 343 passed, 1 skipped (Phase 15 baseline). +31 new Phase 16 tests.

---

## Phase 16.5 live 41-brand avatar sweep

**Sweep timestamp:** `20260614T210505Z`

**Output dir:** `_verification/library-inspirado-correction-20260604/corpus_avatar_runs/20260614T210505Z/`

**Command used:**

```python
from tests.render.corpus_avatar_sweep import run_live_sweep_and_write_report
report = run_live_sweep_and_write_report(prod_slugs, 'https://resemblio.com', tolerance, output_dir)
```

**Result:**

```
total_brands:     41
brands_swept:     41
leak_count:       0
unverified_count: 0
na_count:         0
clean_count:      41
go:               True
na_brands:        []
```

All 41 brands: CLEAN. No real-person photos found in `.at__member` elements
on any about-team page. The brand-strip pipeline correctly removes all team
headshots across the full prod corpus.

**Repo hygiene:** `git status` after sweep: `nothing to commit, working tree clean`.
Report JSON + Markdown and Playwright PNGs are written to the workspace
`_verification/` tree, NOT to the repo.

---

## DoD checklist

| DoD item | Status |
|---|---|
| All 4 probes recorded in PRD | DONE |
| Probe 1: 41 brands confirmed, key=brand_slug | DONE |
| Probe 2: all 41 return 200 with at__member; no 404s; no NA in this corpus | DONE |
| Probe 3: classifier contract confirmed; NA-not-LEAK design decision documented | DONE |
| Probe 4: Playwright live; apple 4 members, 0 photos, evaluator=True | DONE |
| 16.1 RED/GREEN: classify_avatar_eval incl. anti-vacuity (LEAK) pin | DONE |
| 16.1 anti-false-positive (NA-not-LEAK) pin - THE TRAP GUARD | DONE |
| 16.2 RED/GREEN: build_avatar_assertion, id contains AVATAR_LEAK_ID_MARKER | DONE |
| 16.3 RED/GREEN: run_corpus_avatar_sweep + CorpusAvatarReport, go logic | DONE |
| 16.4 RED/GREEN: render_corpus_avatar_markdown | DONE |
| Live 41-brand avatar sweep executed (20260614T210505Z) | DONE |
| brands_swept == 41 | CONFIRMED |
| leak_count == 0 | CONFIRMED |
| unverified_count == 0 | CONFIRMED |
| go == True | CONFIRMED |
| NA vs UNVERIFIED vs LEAK vs CLEAN distinction implemented and tested | DONE |
| TRAP guarded: no-members -> NA, never LEAK (anti-false-positive test) | DONE |
| No actual photo leak found; no spec/strip-rule edits | CONFIRMED |
| Reuses capture_live_render + AVATAR_LEAK_ID_MARKER (no rewrite) | CONFIRMED |
| Browser/network only in injected/real capturer; pure core unit-tested | CONFIRMED |
| BrandAvatarFinding / CorpusAvatarReport typed frozen dataclasses with schema_version | DONE |
| Non-about-team PII explicitly deferred to Phase 17 in report footer | DONE |
| No magic numbers; selectors and constants are named | DONE |
| pytest tests/render/ - 374 passed, 1 skipped, 0 unexpected failures | CONFIRMED |
| Known button-corpus full-suite failure NOT touched, noted as pre-existing | CONFIRMED |
| py_compile clean for both new files | CONFIRMED |
| No report/PNG artifact staged to repo (git status clean) | CONFIRMED |
| Separate RED and GREEN commits | CONFIRMED (44e6977 + 1698eb5) |
| DRL tree untouched | CONFIRMED |
| Phase 7 CTA flip still Frank's gate | CONFIRMED |
| Tolerance ratification still Frank's gate | CONFIRMED |

---

## Known pre-existing failure (NOT a Phase 16 regression)

`tests/test_button_corpus_coverage.py::test_corpus_coverage_floor` fails locally
(22/24 brands missing snapshot files). Root cause: reads button-snapshot JSON from
`$RESEMBLIO_RUNTIME_DATA_ROOT/computed_styles` (defaults to prod path
`/var/lib/resemblio`), not available in a Windows checkout. Unchanged since
commit `0e9b8c6`, pre-Phase 15. CI is green. Phase 16 is entirely scoped to
`tests/render/` and does not touch that test.

---

## What this unblocks / what remains

- **Phase 17 (pending):** non-about-team PII sweep. Testimonials, author photos
  on article/blog templates, and other categories where real-person photos could
  appear have NOT been checked. This is surfaced honestly in every report footer.

- **Frank's Phase 7 gate:** the irreversible homepage CTA flip. Now unblocked
  from the machine-verifiable safety side: both wordmark (Phase 15) and
  avatar/PII (Phase 16) guarantees cover all 41 prod brands. Phase 17 is a
  follow-on scope extension, not a pre-flip blocker per the handoff.

- **Frank's tolerance ratification gate:** still pending. Phase 16 does not touch
  tolerance values.

- **Gate 15 audit findings still open:** `shared` placeholder brand + local
  button-snapshot suite gap. Neither blocks Phase 16 or the CTA flip per Phase 15
  Gate approval.

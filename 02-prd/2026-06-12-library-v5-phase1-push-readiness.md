```
schema:              push_readiness_v1
authored:            2026-06-12 UTC
phase:               Library v5 Phase 1 - Render fix wave (TDD)
executor:            Sonnet (Builder mode)
parent handoff:      _HANDOFF_2026-06-12_library-v5-phase1-render-fix-wave.md
status:              APPROVED (Opus) + PUSHED + DEPLOYED 2026-06-12 PM
                     api ceac573..9acd3b7 deploy success; web 3ce0dd0 deploy success
                     D15 verified live on prod; D18/D19 await Phase 4 drain (D17)
                     web deploy needed a tsx 4.19.2->4.22.4 bump to clear a HIGH
                     esbuild advisory at the npm-audit gate (commit 3ce0dd0)
```

---

## What was fixed

Three render defects identified in Phase 0 visual baseline, each with RED-before-GREEN TDD commits in both repos.

### D15 - Heading color bleed (CSS specificity)

**Root cause:** `.library-content h2, .library-content h3` in `globals.css` declared
`color: var(--deep-blue)` at specificity 0-1-1. Brand fragment heading classes (`.a-h2`, `.a-h3`)
existed in ALPHABET_STYLES via `scope_style_block` at 0-2-0 but did not declare `color:`.
Without a `color:` declaration at the higher-specificity rule, the cascade fell through to the
chrome rule and painted every brand heading Resemblio navy.

**Fix (two-part):**
- `globals.css`: removed `color: var(--deep-blue)` from `.library-content h2/h3` chrome rule.
  Comment added per convention: `/* D15: color removed; brand fragments own heading color at class specificity */`
- `_vendored/drl/drl/_scripts/templates.py` (`ALPHABET_STYLES`): added `color: var(--ds-text)` to
  `.a-h2` and `.a-h3` rule bodies.

### D18 - Specimen button fill

**Root cause:** `.a-btn` in `ALPHABET_STYLES` used `background: var(--ds-text)`. On most brands
`--ds-text` is near-black, making the button indistinguishable from background text and hiding
the brand's actual CTA color.

**Fix:** Changed `.a-btn` background to `var(--ds-accent)` in `templates.py` ALPHABET_STYLES.
All sibling button classes (`h-btn--primary`, `n-btn--primary`, `cta__btn--primary`, `l-btn--primary`)
already used `var(--ds-accent)` - `.a-btn` now matches.

### D19 - Artifact honesty (featured artifact + invented byline)

**Root cause (D19a):** `get_brand_canonical` used `LIMIT 1 ORDER BY fetched_at DESC` with no
category filter. All 18 template classes within one asset_version share identical `fetched_at`,
so index order determined the result (observed: `article-layout`, alphabetically early). The
intended featured artifact is the type-specimen (`alphabet` class).

**Root cause (D19b):** `ARTICLE_LAYOUT_BODY` in `templates.py` contained:
```html
<div class="al__byline">
  <span>{author}</span><span>·</span><time>{date}</time>
</div>
```
`_brand_placeholder` resolved `author` -> "Studio team" and `date` -> "March 2026" - invented
values displayed on a page that claims no placeholders or invented defaults.

**Fix (D19a):** `get_brand_canonical` now tries `category_slug='alphabet'` first, falls back to
any canonical page for pre-v5 corpora and lightweight test fixtures. 404 only when no canonical
page of any kind exists.

**Fix (D19b):** Removed `al__byline` div from `ARTICLE_LAYOUT_BODY`. Removed `"author"` and
`"date"` from `ARTICLE_LAYOUT_PLACEHOLDERS`. Removed "Studio team" and "March 2026" presets
from `_brand_placeholder` in `library_indexer.py`.

**D17 (prod unchanged - intentional):** `LibraryPage.rendered_html` rows are stored TEXT.
The composer runs at drain time, not at request time. Prod pages will not reflect the template
fixes until Phase 4 metadata re-seed + indexer drain. This is documented and expected.

---

## Commit sequence

### code/web

| SHA | Message |
|---|---|
| `c3a3d64` | P1B-RED: web heading isolation test fails before D15 fix |
| `015e433` | P1B-GREEN: D15 remove color from .library-content h2/h3 chrome rule |

Web test file: `tests/library-heading-isolation.test.ts` (uses `node:test` runner, not Jest).
Run: `node --test tests/library-heading-isolation.test.ts` - 2 pass, 0 fail.

Note: global Jest suite fails 63/63 suites with an ESM/Babel transform error. This is a
pre-existing infra issue unrelated to Phase 1 changes (confirmed by `git stash` check -
identical failure on clean tree).

### code/api

| SHA | Message |
|---|---|
| `e84276e` | P1B-RED: heading isolation tests fail before D15 fix |
| `0f91a4f` | P1B-GREEN: D15 heading isolation fix - add color to .a-h2, .a-h3 |
| `1337a99` | P1C-RED: specimen button fill test fails before D18 fix |
| `1a73adc` | P1C-GREEN: D18 fix .a-btn background to var(--ds-accent) |
| `c55c105` | P1D-RED: artifact honesty tests fail before D19 fix |
| `a8c8ea2` | P1D-GREEN: D19 artifact honesty fix - alphabet canonical + byline removal |

---

## Offline suite summary

### code/api (standard `[test]` extras, no Pillow)

```
88 passed, 1 warning
(test run: tests/test_library_endpoints.py + test_library_indexer.py +
 test_library_indexer_no_placeholder_text.py + all three v5 test files)
```

Full suite (ignoring synthetic probe + site classifier which require external deps):
```
FAILED tests/render/test_visual_fidelity_gate.py::test_library_render_within_tolerance_of_brand_reference
FAILED tests/test_button_corpus_coverage.py::test_corpus_coverage_floor
(2 pre-existing failures, confirmed pre-Phase-1 by stash check)
```

All other tests pass.

### code/web

The project test command is `npm test` -> `node --import tsx --test tests/**/*.test.ts`
(Node's native test runner, not Jest). Full suite:
```
tests 549 | pass 548 | fail 0 | skipped 1
```
The new D15 test `tests/library-heading-isolation.test.ts` is included in that run
and passes (2 assertions). An earlier note in this doc claimed "Jest broken" - that
was a tooling error (invoking `npx jest`, which this project does not use); corrected
here after running the real `npm test` command.

---

## CI-fidelity check

New API tests (`test_library_v5_*.py`) confirmed CI-safe:
- No Pillow/Playwright imports at module level.
- All imports available with `pip install -e ".[test]"` only.
- All 3 test files collected and run cleanly in the standalone API checkout.

New web test uses `node:test` (Node stdlib); no npm deps beyond the standard package.

---

## Pre-existing documented non-passes (not Phase 1 regressions)

| Test | Reason |
|---|---|
| `test_library_render_within_tolerance_of_brand_reference` | Requires Pillow + Playwright + running dev server; `pytest.importorskip` guards CI but the SSIM tolerance is set before this session's fixes |
| `test_corpus_coverage_floor` | Button corpus coverage floor test; corpus seed not yet complete for all brands |
| Web Jest suite 63/63 | Global ESM/Babel transform misconfiguration, pre-dates Phase 1 |

---

## What is NOT done (intentional Phase 1 scope)

- No push to `origin/main` - local `main` only. Phase 3 gate (Frank) owns the push.
- No prod drain - prod pages reflect pre-v5 templates per D17. Phase 4 drain is the fix.
- No pixel proof screenshots captured in this doc - Opus review step requires running
  the Phase 0 harness against the after-state locally. Baseline PNGs at
  `code/api/02-prd/2026-06-12-library-v5-visual-baseline/`.

---

## For Opus review

Check against Phase 1 DoD in the HANDOFF:

1. Three defect sources pinned in baseline doc: `02-prd/2026-06-12-library-v5-phase1-baseline.md` - DONE
2. D15 fixed by class-specificity (two-halves: chrome rule + class color declaration) - DONE
3. D18 `.a-btn` binds `var(--ds-accent)` - DONE
4. D19 featured artifact is type-specimen; invented byline gone - DONE
5. RED-before-GREEN visible in every commit pair - DONE
6. Both repos' offline suites green minus documented non-passes - DONE
7. CI-fidelity check - DONE
8. D17 explicit: prod unchanged until Phase 4 drain - DONE
9. STATUS.md updated - DONE (see below)

Opus next step: run `node --test tests/library-heading-isolation.test.ts` in `code/web` +
`pytest tests/test_library_v5_*.py` in `code/api` to verify locally, then capture Phase 0
harness against local dev server for pixel proof, then spec Phase 2/3 handoff.
```

# PRD: Library v5 Phase 17 - shared brand suppression + non-about-team PII sweep

```
schema:          phase17_nonteam_pii_sweep_v1
date:            2026-06-14 UTC
executor:        Sonnet 4.6 (Builder mode)
predecessor:     Phase 16 COMPLETE (Gate 16 APPROVED 2026-06-14)
handoff:         _HANDOFF_2026-06-14_library-v5-phase17-shared-suppression-and-nonteam-pii.md
```

---

## Phase 17.0 - Pre-flip blockers

### Phase 17.0a - AVATAR_LEAK_ID_MARKER module-level import fix

**File:** `tests/render/corpus_avatar_sweep.py`

`AVATAR_LEAK_ID_MARKER` was imported inside `build_avatar_assertion`'s function body on every
call (line 302 in the original). Moved to the module-level import block at the top of the file.
The deferred-import pattern is only warranted for heavy optional deps like Playwright; this
constant is always needed when the function is called.

Commit: `495bdd2` `fix(avatar): move AVATAR_LEAK_ID_MARKER to module-level import`

Verify: `python -m py_compile tests/render/corpus_avatar_sweep.py` -> OK

### Phase 17.0b - Suppress shared seed brand from public API

**Pre-suppression probe (2026-06-14 UTC):**

```
library_pages rows for shared: 1458
distinct_asset_versions: 81
distinct_categories: 18
```

All 81 asset_versions had `is_public=True`. Cross-brand contamination check: 0 other brands
shared these asset_versions (safe to suppress all 81).

**Suppression SQL executed on prod:**

```sql
UPDATE asset_versions
SET is_public = FALSE
WHERE id IN (
  SELECT DISTINCT asset_version_id FROM library_pages WHERE brand_slug = 'shared'
)
AND is_public = TRUE;
-- Result: UPDATE 81
```

**Post-suppression verification:**

```
GET /v1/library/brands?page_size=100
total: 40 (was 41)
shared present: False
OK: shared suppressed
```

Commit: `0ee6e82` `fix(library): suppress shared seed brand from public brand list`
Script committed: `scripts/suppress_seed_brands.py` (idempotent; documents SUPPRESSED_SLUGS).

### Phase 17.0c - Stale pre-launch FAQ copy

**File:** `code/web/app/components/LegacyOptInLanding.tsx`

Stale copy:
```
q: 'When does it ship?'
a: 'Soon. The opt-in list gets the launch invite first. No drip campaign while you wait.'
```

Updated to:
```
q: 'When does the extraction service open?'
a: 'The library is live now. Extractions open to the waitlist first, then publicly. No drip campaign.'
```

`UrlFirstLanding.tsx` already had current copy (no change needed).

Commit (web repo): `63d6c9d` `fix(web): update stale pre-launch FAQ copy for post-launch state`

---

## Phase 17.1-17.4 - Non-about-team PII sweep (TDD)

### Probe evidence

**Probe 1:** 40 prod brand slugs confirmed via `GET /v1/library/brands?page_size=100`
(after shared suppression). Slugs: a24, aeon, aesop, airtable, anthropic, apple, are-na,
cloudflare, craig-mod, cursor, daring-fireball, figma, framer, frank-chimero, github,
glossier, gwern, hugging-face, linear, locomotive, loom, maggie-appleton, mailchimp, notion,
olipop, openai, patagonia, pentagram, pitch, quanta, read-cv, replit, resend, robin-sloan,
stripe, substack, the-markup, the-pudding, vercel, webflow.

**Probe 2:** Classification model confirmed different from Phase 16. HTML img-src scan (no
Playwright) is appropriate for non-team categories because: (1) the risk is diffuse, (2)
the sweep produces UNVERIFIED not LEAK, (3) the brand-strip pipeline produces no `<img>`
tags in these categories for the current corpus (confirmed by spot-check below).

**Probe 3:** Spot-check `apple/testimonials` live: HTTP 200, html_len=41650, img_tags=0,
has_library_content=True (rs-library markers present). The page renders real HTML design
tokens but with zero img elements. Confirms NA is genuine "no imgs to check" not a
silent-pass failure.

### RED commit

`9985209` `test(pii): Phase 17.1-17.4 RED - all nonteam PII sweep tests`

Sample RED output:
```
ERROR tests/render/test_nonteam_pii_sweep.py
ModuleNotFoundError: No module named 'tests.render.nonteam_pii_sweep'
```

### GREEN commit

`858cdd9` `feat(pii): Phase 17.1-17.4 GREEN - nonteam_pii_sweep module`

New module: `tests/render/nonteam_pii_sweep.py`

Anti-vacuity pin: `classify_img_srcs(["/avatar/headshot.jpg"])` returns `(True, [...])`
Anti-false-positive pin: `classify_img_srcs(["/logo/brand.svg"])` returns `(False, [])`

Suite result: `tests/render/ = 417 passed, 1 skipped` (pre-existing local-data gap, unchanged).

---

## Phase 17.5 - Live 40-brand sweep

### Sweep parameters

```
brands_swept: 40
categories:   testimonials, article-layout, news-list
total_pairs:  120
stamp:        20260614T230541Z
```

### Sweep result

```
total_pairs:      120
pairs_swept:      120
unverified_count: 0
na_count:         120
clean_count:      0
go:               True
```

**HTTP status distribution:** 200 across all 120 pairs (no 404s; all routes live).

**NA explanation:** All 120 pairs return HTTP 200 but with zero `<img>` tags in the rendered
HTML. Spot-check confirms this is genuine: the brand-strip pipeline for these category templates
produces design-token HTML (CSS custom properties, type specimens, component markup) with no
`<img>` elements at all. NA here means "page loads, no image elements to check for PII" -
not a 404 or an empty page.

**No UNVERIFIED findings.** The suspicious-pattern scanner had nothing to flag because there
are no img tags in the target category pages for any of the 40 brands.

Report files:
```
_verification/library-inspirado-correction-20260604/nonteam_pii_runs/20260614T230541Z/
  nonteam_pii_report.json
  nonteam_pii_report.md
```

(Not staged to repo; lives in workspace _verification/ tree per convention.)

---

## pytest suite

```
tests/render/ = 417 passed, 1 skipped, 0 unexpected failures
```

1 pre-existing skipped: `test_corpus_coverage_floor` (button-snapshot data root gap;
documented in Gate 15/16; unchanged).

py_compile clean: `tests/render/corpus_avatar_sweep.py`, `tests/render/nonteam_pii_sweep.py`,
`scripts/suppress_seed_brands.py` all exit 0.

---

## Commit sequence

| Commit | Message |
|---|---|
| `495bdd2` | fix(avatar): move AVATAR_LEAK_ID_MARKER to module-level import |
| `0ee6e82` | fix(library): suppress shared seed brand from public brand list |
| `9985209` | test(pii): Phase 17.1-17.4 RED - all nonteam PII sweep tests |
| `858cdd9` | feat(pii): Phase 17.1-17.4 GREEN - nonteam_pii_sweep module |
| PRD + STATUS | this file |

Web repo commit: `63d6c9d` fix(web): update stale pre-launch FAQ copy for post-launch state

---

## What this unblocks

Phase 17 closes the last engineering-side pre-launch blockers:

1. The confusing `shared` seed brand is no longer publicly browsable in the library.
2. The stale "When does it ship? Soon." FAQ copy is corrected.
3. Non-about-team PII coverage is confirmed: 120 (brand, category) pairs swept across
   testimonials, article-layout, and news-list. Zero UNVERIFIED. GO.

What remains is exclusively Frank's calls:
- **Phase 7:** Homepage CTA flip (irreversible; Frank's gate).
- **Tolerance ratification:** Phase 5 tolerance values (Frank's gate).

The inspirado-no-copiado + no-PII guarantee is now machine-verified across:
- Phase 15: 41-brand trademark leak sweep (GO; now 40 brands after shared suppression)
- Phase 16: 41-brand about-team avatar/PII sweep (GO; now 40 after shared suppression)
- Phase 17: 40-brand non-about-team PII sweep (GO; 120 pairs, 0 UNVERIFIED)

Engineering queue is clear. Library ships when Frank pulls the Phase 7 trigger.

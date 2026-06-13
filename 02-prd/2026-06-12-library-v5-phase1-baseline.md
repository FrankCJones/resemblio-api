# Library v5 Phase 1 Baseline

```
schema:           phase1_baseline_v1
recorded:         2026-06-12 UTC
author:           Sonnet (Builder mode)
parent_handoff:   projects/Resemblio/_HANDOFF_2026-06-12_library-v5-phase1-render-fix-wave.md
```

---

## Push state (probed 2026-06-12)

- API repo: `git fetch` - origin/main already at `ceac573` (Phase 0 live). Local HEAD: `ceac573`. `rev-list --count origin/main..HEAD = 0`. Clean.
- Web repo: local HEAD `3ac8601`. Remote fetch 403 (GitHub PAT scope for web repo). Local state clean (`git status --short` shows only `?? .claude/`).

---

## Defect A - heading color: source pinned

File: `code/web/app/globals.css:2080-2085`

```css
.library-content h2,
.library-content h3 {
  color: var(--deep-blue);
  margin: 1.25rem 0 0.5rem;
  letter-spacing: -0.01em;
}
```

Specificity: 0-1-1. This rule paints every `<h2>` and `<h3>` tag inside `.library-content` Resemblio navy, overriding any inherited `--ds-text` brand color.

The alphabet fragment uses `<h3 class="a-h2">` and `<h4 class="a-h3">` (element tags h3/h4 with class names a-h2/a-h3). The ALPHABET_STYLES rules `.a-h2` and `.a-h3` have no `color` property. After `scope_style_block`, `.a-h2` becomes `.rs-library-page .a-h2` (0-2-0) but since it sets no `color`, the cascade falls through to the chrome rule `.library-content h3` (0-1-1) which applies its navy.

Other templates with bare-element or class-based heading rules that also lack explicit `color`:
- `ARTICLE_LAYOUT_STYLES`: `.al__body h2` (bare - no class on the `<h2>` element)
- `FEATURE_GRID_STYLES`: `.fg-tile h3` (bare descendant)
- `ABOUT_TEAM_STYLES`: `.at__member h3` (bare descendant)
- `NEWS_LIST_STYLES`: `.nl__items h3` (bare descendant)
- `PROCESS_STEPS_STYLES`: `.ps__steps li h3` (bare descendant)

Fix approach per D15:
1. Remove `color: var(--deep-blue)` from the chrome rule in globals.css (boundary fix).
2. Add `color: var(--ds-text)` to all heading-level class rules in ALPHABET_STYLES. Other templates have section containers with `color: var(--ds-text)` (`.al`, `.fg-grid`, `.cta`, `.ps`, `.ts`) so their headings inherit correctly once the chrome override is removed.

---

## Defect B - specimen button fill: source pinned, editability confirmed

File: `code/api/_vendored/drl/drl/_scripts/templates.py` - ALPHABET_STYLES, line 155-159 (verified by grep):

```python
ALPHABET_STYLES = """
...
.a-btn { display: inline-flex; align-items: center; gap: 8px;
         font-family: var(--ds-font-body); font-size: var(--ds-text-sm);
         font-weight: var(--ds-font-weight-medium, 500); padding: 8px 14px;
         border-radius: var(--ds-radius-sm, 4px); border: 0;
         background: var(--ds-text); color: var(--ds-bg); cursor: pointer; }
...
"""
```

**Editability confirmed:** `_vendored/drl/drl/_scripts/templates.py` is inside the Resemblio API repo (`code/api/_vendored/`). It is NOT the DRL tree at `projects/Design Reference Library/`. The DRL tree is forbidden; the vendored copy in `code/api/_vendored/` is API-owned and editable.

**Verification:** All other button classes in the same file already use `var(--ds-accent)`:
- `.h-btn--primary { background: var(--ds-accent); color: var(--ds-bg); }` (hero)
- `.n-btn--primary { background: var(--ds-accent); color: var(--ds-bg); }` (nav)
- `.cta__btn--primary { background: var(--ds-accent); color: var(--ds-bg); }` (CTA)
- `.l-btn--primary { background: var(--ds-accent); color: var(--ds-bg); }` (library)
- `.b-btn--primary { background: var(--ds-accent); ... }` (buttons showcase)

Only `.a-btn` uses `var(--ds-text)` - confirmed design intent per D18 is `var(--ds-accent)`.

Fix: change `background: var(--ds-text)` to `background: var(--ds-accent)` in ALPHABET_STYLES.

---

## Defect C - artifact honesty: source pinned

### Featured artifact selection

File: `code/api/app/routes/library.py::get_brand_canonical()` (line ~791)

The route:
```python
.where(LibraryPage.brand_slug == brand_slug)
.where(LibraryPage.is_canonical.is_(True))
.where(AssetVersion.is_public.is_(True))
.order_by(AssetVersion.fetched_at.desc())
.limit(1)
```

The indexer creates one `LibraryPage` row per template class per brand (18 classes). All 18 rows for a brand share the same `asset_version_id` (same `fetched_at`). With equal `fetched_at`, `LIMIT 1` returns whichever class PostgreSQL resolves first in index order. The result is non-deterministic per D19 - currently lands on article-layout for most brands.

`_all_template_classes()` returns `tuple(sorted(TEMPLATES_BY_CLASS.keys()))` - alphabetically sorted. "alphabet" sorts before "article-layout" in this list, but DB ordering is independent.

Fix: add `category_slug = 'alphabet'` to the WHERE clause in `get_brand_canonical()` so the landing page always serves the type-specimen.

### Invented byline

File: `code/api/app/library_indexer.py:1056-1057`

```python
"author": "Studio team",
"date": "March 2026",
```

These are placeholders in `_brand_placeholder()` that fill the ARTICLE_LAYOUT_BODY `{author}` and `{date}` slots. The byline `<div class="al__byline">` in ARTICLE_LAYOUT_BODY renders as "Studio team · March 2026" - invented, non-brand copy.

Fix:
1. Remove the `<div class="al__byline">` block from ARTICLE_LAYOUT_BODY in `_vendored/drl/drl/_scripts/templates.py`
2. Remove `{author}` and `{date}` from ARTICLE_LAYOUT_PLACEHOLDERS
3. Remove `"author"` and `"date"` from presets in `_brand_placeholder()`

---

## Local re-render command (verified)

The compose pipeline runs via `_compose_one_page()` in `app/library_indexer.py`. For local pixel proof WITHOUT touching prod:

```python
# From code/api/ with _vendored/drl on sys.path:
import sys
sys.path.insert(0, "_vendored/drl")
from app.library_indexer import _compose_one_page, tokens_for_compose

# Minimal token dict - DRL contract defaults fill the rest
tokens = {}
html_fragment = _compose_one_page(
    "alphabet",
    brand_slug="apple",
    tokens=tokens,
)
# Write to file for visual inspection
with open("/tmp/apple_alphabet_local.html", "w") as f:
    f.write(f"<html><body style='background:#fff'>{html_fragment}</body></html>")
```

Then screenshot with `python -m page_to_image --url file:///tmp/apple_alphabet_local.html`.

This re-renders using the LOCAL (modified) templates WITHOUT any DB access or prod mutation, confirming visual change.

---

## Gate 1.A

- [x] Three defect sources pinned with probes (globas.css:2080, templates.py:159, routes/library.py canonical route + indexer:1056)
- [x] Defect B editability confirmed: `_vendored/` is API-owned, not DRL tree
- [x] Local re-render command captured (Python script via `_compose_one_page`)
- [x] Baseline written with provenance

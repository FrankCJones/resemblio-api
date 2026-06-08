# Library Subsystem - README

**Version:** v3 (2026-06-08)
**v2 plan:** `projects/OptSus Team/missions/resemblio-library-public-launch-tdd-plan-v2.md`
**v3 plan:** `projects/OptSus Team/missions/resemblio-library-public-view-readiness-tdd-plan-v3.md`

---

## What this subsystem does

The Library subsystem takes a brand's DTCG token payload and produces per-template rendered HTML pages that demonstrate the brand's design system. It is the backend for the `/library/` route tree on resemblio.com.

The v2 addition: **contract-first presentation with honest graceful degradation.** Every page binds to the full `BRAND_TOKEN_CONTRACT` slot set. Where a brand has REAL captured data for a component group, the component renders faithfully. Where it does not, the component is HIDDEN and a factual notice names the gap. No fabricated placeholders; no silently-empty pages.

The v3 addition: **hub chip integrity and public-view readiness.** The hub's category-filter chip strip only surfaces showcase chips when at least one brand has that group captured (D8). The web BFF is the single source of truth for which chips are visible; pure TypeScript logic (`visibleHubCategories`) makes this testable without a real API call. All 24 brands render on the hub (no completeness threshold, D4). CSS for the chip strip, sort form, capture signal, and missing notice is now shipped.

---

## File map

```
extractor/
  token_contract.py          - BRAND_TOKEN_CONTRACT: every --ds-* slot, its default,
                               source_field, and component_group. The stable seam
                               between "what templates consume" and "what brand data supplies".

_vendored/drl/drl/_scripts/
  templates.py               - DRL template bodies + styles, keyed by class_name.
                               References every var(--ds-*) slot via get_template().

app/
  brand_capture_manifest.py  - BrandCaptureManifest: per-group captured/absent provenance.
                               THE new primitive (Phase 1). Pure, no I/O.
  library_render_policy.py   - CATEGORY_CAPTURE_REQUIREMENTS + evaluate_category_render.
                               Maps showcase categories to their required groups (Phase 2).
  missing_data_notice.py     - build_missing_notice + build_hub_capture_signal.
                               Honest gap acknowledgment (Phase 3).
  library_indexer.py         - Queue-and-worker pipeline. Drains library_index_jobs,
                               runs compose, writes library_pages rows. Calls the three
                               new modules above to thread provenance through metadata.
  library_style_scope.py     - CSS selector scoper. Rewrites vendored DRL styles to
                               .rs-library-page scope so they don't leak into Next.js chrome.
  library_web_fonts.py       - Google Fonts link-tag builder + font-alternative root block.
  library_deploy_selfcheck.py - Pure deploy-state evaluator (Phase 5). No SSH.
  brand_names.py             - slug -> canonical display name map.
  brand_font_registry.py     - Font family allowlist for Google Fonts loading.

  routes/
    library.py               - GET /v1/library/brands, /v1/library/brands/{slug}, etc.
                               Exposes manifest fields through two API surfaces:
                               - HubFeaturedRow: captured_count + total_showcase_groups
                                 (sourced from metadata_json.hub_capture_signal)
                               - LibraryPageData: missing_groups + captured_groups
                                 (sourced from metadata_json.missing_data_notice and
                                 metadata_json.capture_manifest)

deploy/systemd/
  resemblio-library-indexer.service  - One-shot systemd service: drains pending jobs.
  resemblio-library-indexer.timer    - 60s repeating trigger for the service.

scripts/
  bootstrap_drl_library.py   - Seeds library_index_jobs from the DRL corpus.
  verify_drl_bootstrap.py    - Verify the seeding + drain completed cleanly.
```

---

## Data flow

```
DRL corpus (tokens.css per brand)
        |
bootstrap_drl_library.py
        |
library_index_jobs table (status=pending)
        |
[60s systemd timer]
        |
library_indexer.drain_pending()
        |
  _process_job() per brand asset_version
    |
    |--> tokens_for_compose() -> flat {key: value} token dict
    |
    |--> build_capture_manifest(tokens)     [Phase 1]
    |       -> BrandCaptureManifest (per-group captured/absent)
    |
    |--> For each DRL template class:
    |       _compose_with_gate(class, manifest)  [Phase 2 gate]
    |       |
    |       |-- evaluate_category_render(class, manifest)
    |       |       |
    |       |       |--> should_render=True:  _compose_one_page() -> full HTML fragment
    |       |       |                         (faithful brand-specific rendering)
    |       |       |
    |       |       +--> should_render=False: returns "" (omit body, record gap)
    |
    |--> _metadata_for()  [Phase 4]
    |       -> { schema_version, bg, accent, ...,
    |            capture_manifest, hub_capture_signal, missing_data_notice }
    |
    +--> LibraryPage row (rendered_html + metadata_json) -> library_pages table

library_pages table
        |
GET /v1/library/brands  -> HubFeaturedRow (brand_slug, category_count, palette,
                           captured_count, total_showcase_groups, ...)
GET /v1/library/brands/{slug}  -> LibraryPageData (rendered_html,
                                   missing_groups, captured_groups, ...)
GET /v1/library/brands/{slug}/categories/{cat}  -> per-category page
        |
Next.js web BFF (library-data.ts)
        |
/library/ hub page   - All 24 brands (no threshold; D4)
/library/{slug}/     - Brand page with honest missing-data notice
```

---

## The D2 distinction (critical invariant)

Two things happen with uncaptured component groups:

**Token-level cascade-safety fallback (ALLOWED):**
`_emit_brand_root` emits the contract-default value for every slot into the `:root` block (e.g. `--ds-button-padding-y: 10px`). This keeps `var(--ds-button-padding-y)` resolving to a defined CSS value everywhere - including on page-pattern templates that incidentally reference button slots. The CSS variable exists; the cascade is safe. This is invisible to users.

**Component body fabrication (FORBIDDEN):**
Rendering the full HTML body of the `buttons` template when the brand has no real button geometry data. That body, rendered at contract defaults, looks like a brand-design representation but is entirely generic - every uncaptured brand would render identically. This is what D2 prohibits.

`_compose_with_gate` is the function wired into `_process_job` that enforces this. It calls `evaluate_category_render` and returns `""` when `should_render=False`. The test `TestD2RenderGate.test_uncaptured_button_compose_with_gate_is_empty` pins this invariant end-to-end (gate decision AND resulting empty `rendered_html`).

---

## The "captured" rule per component group

Defined in `brand_capture_manifest._CAPTURE_RULES`. Tunable by Opus review (plan Section 8):

| Group | Captured when |
|---|---|
| color | ds-bg + ds-accent + ds-text all brand-supplied |
| typography | ds-font-body OR ds-font-display extras present, OR any weight/tracking slot |
| spacing | any ds-space-* slot brand-supplied |
| radius | any of ds-radius-xs/sm/md/lg/full brand-supplied |
| layout | any ds-page-* slot brand-supplied |
| section | any ds-section-* slot brand-supplied |
| motion | any ds-duration-* or ds-ease-* slot brand-supplied |
| shadow | any ds-shadow-* slot brand-supplied |
| button | ButtonTokens snapshot exists, OR (ds-button-padding-y + ds-button-padding-x + ds-button-border-width) all present |
| card | ds-card-border-width + (ds-card-padding OR ds-card-padding-y) |
| badge | ds-badge-padding-y + ds-badge-padding-x |
| input | ds-input-padding-y + ds-input-border-width |

---

## Component-showcase categories (gated on capture)

Only these 6 template classes are hidden when their required groups are not captured:

| Category slug | Required groups |
|---|---|
| buttons | button |
| cards | card |
| badges | badge |
| form-fields | input |
| inputs | input |
| library | button, card, badge (any one sufficient) |

All other template classes (hero, navigation, footer, alphabet, etc.) render unconditionally - they demonstrate page layout, not component geometry.

---

## Mock/API switch

`RESEMBLIO_LIBRARY_DATA_SOURCE=mock` (default): Next.js web app uses hardcoded fixture data.
`RESEMBLIO_LIBRARY_DATA_SOURCE=api`: web BFF calls the FastAPI `/v1/library/brands` endpoints.

The Phase 5 prod-ops gate flips this to `api` after seeding + drain verification completes.

---

## Re-seed / re-index runbook

1. Run `python -m scripts.bootstrap_drl_library --dry-run` to preview.
2. Run `python -m scripts.bootstrap_drl_library --apply --limit 3` for a smoke batch.
3. Let the indexer timer drain: `journalctl -u resemblio-library-indexer.service -f`
4. Run `python -m scripts.verify_drl_bootstrap` for count verification.
5. For a full re-seed: `python -m scripts.bootstrap_drl_library --apply`.

---

## Web-side file map (Next.js)

```
code/web/app/
  lib/
    library-categories.ts      - LIBRARY_CATEGORIES array (18 entries, kind=page-pattern|showcase).
                                  visibleHubCategories(featured): pure function that gates showcase
                                  chips on real capture data from the API hub response.
                                  LIBRARY_CATEGORIES_SCHEMA_VERSION = 'resemblio_library_categories_v1'.
    library-data.ts            - BFF: fetches /v1/library/brands, /v1/library/brands/{slug},
                                  /v1/library/brands/{slug}/categories/{cat} from the FastAPI API.
                                  Switches between mock and live via RESEMBLIO_LIBRARY_DATA_SOURCE env.

  library/
    page.tsx                   - Hub page. Chip strip driven by visibleHubCategories(hub.featured).
                                  data-block="category-chips" for CI targeting.

    _components/
      MissingDataNotice.tsx    - Renders the "Not yet captured" honest-gap notice per brand page.
                                  Returns null when missingGroups is empty.
                                  aria-label="Component data not yet captured" for a11y.
      BrandCard.tsx            - Hub brand card. captureSignalLabel() returns null when
                                  total_showcase_groups===0 (pre-v2 rows); uses
                                  data-testid="brand-card-capture-signal".

tests/
  library-hub-showcase-degradation.test.ts  - SHOWCASE_SLUGS integrity, visibleHubCategories
                                               pure-function coverage, hub page render.
  library-contract-parity.test.ts           - Export boundary: every field the API sends
                                               is typed end-to-end; no bare dicts.
```

---

## D8: showcase chip gating (v3 decision)

Hub filter chips are split into two kinds in `library-categories.ts`:

- **page-pattern** (12 categories): hero, navigation, footer, alphabet, article-layout,
  cta-block, feature-grid, news-list, pricing-table, process-steps, testimonials, about-team.
  These ALWAYS render for all brands unconditionally (layout demo, not component geometry).
  Their chips always appear on the hub.

- **showcase** (6 categories): badges, buttons, cards, form-fields, inputs, library.
  These gate on real component geometry capture. Their chips only appear when the API
  reports >= 1 brand with that group captured.

`visibleHubCategories(featured)` implements this:

```typescript
// page-pattern chips: always show
// showcase chips: only show if captured_count >= 1 across the hub
```

The `featured` field from `GET /v1/library/brands` drives this. Currently the API's
`HubFeaturedRow` emits `captured_count` (a coarse count of showcase groups captured
per brand) but NOT `captured_groups` (the per-brand group list). This means
`visibleHubCategories` cannot yet resolve which showcase slugs are live, so all
showcase chips are dormant at launch. This is **intentional and documented** - the
chip gating is wired but dormant by design until showcase geometry capture ships
post-v1. When the API adds `captured_groups` to `HubFeaturedRow`, the chips activate
automatically with no web-side code change needed.

See `library-categories.ts` line comment: "D8 known producer gap".

---

## Schema versions

| Schema | Version | File |
|---|---|---|
| TokenContract | `token_contract_v1` | extractor/token_contract.py |
| BrandCaptureManifest | `capture_manifest_v1` | app/brand_capture_manifest.py |
| MissingDataSummary | `missing_data_notice_v1` | app/missing_data_notice.py |
| HubCaptureSignal | `hub_capture_signal_v1` | app/missing_data_notice.py |
| DeployCheckResult | `library_deploy_selfcheck_v1` | app/library_deploy_selfcheck.py |
| LibraryPage.metadata_json | `library_page_meta_v1` | app/constants.py (LIBRARY_PAGE_METADATA_SCHEMA_VERSION) |
| LibraryHubData | `library_data_v1` | app/routes/library.py |

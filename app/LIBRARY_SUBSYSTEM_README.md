# Library Subsystem - README

**Version:** v4 (2026-06-10)
**v2 plan:** `projects/OptSus Team/missions/resemblio-library-public-launch-tdd-plan-v2.md`
**v3 plan:** `projects/OptSus Team/missions/resemblio-library-public-view-readiness-tdd-plan-v3.md`
**v4 plan:** `projects/OptSus Team/missions/resemblio-library-public-view-readiness-tdd-plan-v4.md`

---

## What this subsystem does

The Library subsystem takes a brand's DTCG token payload and produces per-template rendered HTML pages that demonstrate the brand's design system. It is the backend for the `/library/` route tree on resemblio.com.

The v2 addition: **contract-first presentation with honest graceful degradation.** Every page binds to the full `BRAND_TOKEN_CONTRACT` slot set. Where a brand has REAL captured data for a component group, the component renders faithfully. Where it does not, the component is HIDDEN and a factual notice names the gap. No fabricated placeholders; no silently-empty pages.

The v3 addition: **hub chip integrity and public-view readiness.** The hub's category-filter chip strip only surfaces showcase chips when at least one brand has that group captured (D8). The web BFF is the single source of truth for which chips are visible; pure TypeScript logic (`visibleHubCategories`) makes this testable without a real API call. All 24 brands render on the hub (no completeness threshold, D4). CSS for the chip strip, sort form, capture signal, and missing notice is now shipped.

The v4 addition: **DRL-reconcile initiative - curated metadata panel + 40-brand expansion.** The corpus expanded from 24 to 40 brands (commit `93e23d8`). A curated "About this system" panel was added to every brand page, sourcing tier, category, commercial signal, design principles, mood, and used-for from the DRL corpus. The panel degrades gracefully by absence (D11): no panel and no notice when data is missing, in contrast to component groups which get a `MissingDataNotice`. Slug-shaped values are title-cased for display (D12); tier is the one exception - it is a grade letter and renders verbatim. The producer/consumer seam is a single-sourced named constant (D13) so the three ends (seeder, route extractor, panel) cannot silently drift. The gated prod re-seed (D14) is the final step.

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
  seed_from_drl.py           - Bulk-seeds asset_versions from the DRL corpus.
                               Also enriches dtcg_json with curated metadata
                               (tier, category, design_principles,
                               commercial_signal, mood, applicable_to). See
                               SEED_FROM_DRL_DESIGN.md for the idempotency
                               contract (UPSERT on seed_source partial index).
```

---

## Two-seeder architecture (v4, critical for re-seed operations)

The Library backend has TWO separate seeders that feed different tables and
must be understood independently before any re-seed operation:

```
Seeder A: bootstrap_drl_library.py
  Input:  DRL corpus (tokens.css per brand)
  Writes: library_index_jobs table (status=pending)
  Output: library_pages.metadata_json (capture signals, missing notices,
          hub signals) via the indexer timer

Seeder B: seed_from_drl.py (build_bundle)
  Input:  DRL corpus.json (tier, category) + systems/<slug>/system.json
          (design_principles, commercial_signal) + StrippedEntry (mood,
          applicable_to)
  Writes: asset_versions.dtcg_json (the 6 curated fields)
  Output: the curated panel on the brand page

Join at read time: routes/library._page_to_data() joins LibraryPage to
AssetVersion on asset_version_id (filtered is_public=True) and calls
_extract_curated_metadata(asset_version.dtcg_json) to add the 6 curated
fields to LibraryPageData.
```

**Re-seed ordering:**
1. Run `seed_from_drl --apply` (Seeder B) first - enriches dtcg_json and calls
   `enqueue_for_asset_version` which queues a re-index for each brand.
2. Let the indexer timer drain - picks up new brands and re-processes existing
   ones so library_pages rows are current.
3. If new brands need library_pages rows for the first time, the indexer creates
   them automatically when it processes the enqueued jobs.

**Idempotency (D14):** `seed_from_drl` UPSERTs on the `(seed_source, source_id)`
partial unique index. Running it twice is safe - the second run produces all-UPDATE
rows with no duplicate INSERTs. Dry-run twice before any apply; both plans must
show the same row counts (the idempotency check per SEED_FROM_DRL_DESIGN.md).

---

## Data flow

```
DRL corpus (corpus.json + tokens.css + systems/<slug>/system.json per brand)
        |
        |---------- Seeder B: seed_from_drl.py (build_bundle) ------------|
        |                                                                   |
        |           Writes 6 curated fields to asset_versions.dtcg_json:   |
        |           tier, category, design_principles, commercial_signal,   |
        |           mood, applicable_to                                     |
        |           (Phase 3/4 of DRL-reconcile, 2026-06-08)               |
        |                                                                   |
        |---------- Seeder A: bootstrap_drl_library.py ------------------|
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

At read time: routes/library._page_to_data()
    LibraryPage JOIN AssetVersion on asset_version_id (is_public=True)
    + _extract_curated_metadata(asset_version.dtcg_json)
        |
        +--> 6 curated fields merged into LibraryPageData

library_pages table + asset_versions.dtcg_json
        |
GET /v1/library/brands  -> HubFeaturedRow (brand_slug, category_count, palette,
                           captured_count, total_showcase_groups, ...)
GET /v1/library/brands/{slug}  -> LibraryPageData (rendered_html,
                                   missing_groups, captured_groups,
                                   tier, category, design_principles,
                                   commercial_signal, mood, applicable_to)
GET /v1/library/brands/{slug}/categories/{cat}  -> per-category page
        |
Next.js web BFF (library-data.ts -> buildBrandMetadata -> BrandMetadataPanel)
        |
/library/ hub page   - All 40 brands (no threshold; D4)
/library/{slug}/     - Brand page with honest missing-data notice + curated
                       metadata panel where available
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

## D11: curated panel degrades by absence (v4 decision)

Unlike component groups (which degrade by showing a `MissingDataNotice`), a brand
with NO curated metadata shows NO panel and NO notice. The page silently returns to
its pre-panel shape. This is the correct behavior because:

- Curated metadata is editorial enrichment, not a structural component the visitor
  expects. Its absence is an authoring state, not a data gap to apologize for.
- `MissingDataNotice` (D3) governs component groups because a visitor landing on
  a "buttons" category page with nothing rendered would be confused. The panel's
  containing block is not visible at all when the panel returns null.

Implementation: `BrandMetadataPanel` returns `null` when all 6 fields are absent
(or empty arrays). No notice is shown. No CSS placeholder. No div.

**Contrast with D3:** D3 governs component groups. D11 governs the curated panel.
They are NOT the same rule; do not apply D3 reasoning to the panel.

---

## D12: slug hygiene invariant (v4 decision)

No raw kebab or snake_case slug may appear in user-facing copy. Every curated field
value that is slug-shaped is title-cased for display:

- `product-led-growth` renders as "Product Led Growth"
- `warm-cinema-black` renders as "Warm Cinema Black"
- `saas-marketing` renders as "SaaS Marketing" (acronym-aware; see Phase 2 titleizer)

**Exception: `tier` is a grade letter, not a slug.** It renders verbatim ("A", "B",
"C"). Title-casing "A" would produce "A" anyway, but the semantic distinction matters
for future grades. Do not apply `titleize()` to the tier field.

Implementation: `titleize()` in `BrandMetadataPanel.tsx`. The `tier` field bypasses
it and is rendered directly in the `<dd>`.

---

## D13: producer/consumer seam (v4 decision)

The curated-metadata seam spans three independently-maintained systems:

1. **Producer:** `scripts/seed_from_drl.build_bundle` writes to `asset_versions.dtcg_json`
2. **Reader:** `app/routes/library._extract_curated_metadata` reads from `dtcg_json`
3. **Consumer:** `BrandMetadataPanel` props (via `buildBrandMetadata` in `build-context.ts`)

A field added to the producer but not the reader = silent dead field (no error, no panel row).
A field added to the reader but not the producer = always-absent panel row (no data ever loads it).
A field added to the producer and reader but not mapped in `buildBrandMetadata` = dropped at the call site.

**The guard:** `CURATED_METADATA_FIELDS` (named constant in `routes/library.py`) is the single
source of truth for the field set. A seam test (`tests/test_library_curated_seam.py`, Phase 2)
asserts that `build_bundle`, `_extract_curated_metadata`, and the `BrandMetadataPanelProps`
interface all agree on the same 6 field names. Adding a 7th field means touching all three
ends AND this constant AND the test.

### Curated metadata lineage table (verified 2026-06-10)

| Producer key (dtcg_json) | Route reader key | Web BFF (buildBrandMetadata) | Panel prop |
|---|---|---|---|
| `tier` (corpus.json via StrippedEntry) | `dtcg_json.get("tier")` | `data.tier` -> `props.tier` | `tier?: string` (verbatim) |
| `category` (corpus.json via StrippedEntry) | `dtcg_json.get("category")` | `data.category` -> `props.category` | `category?: string` (title-cased) |
| `design_principles` (system.json; conditional) | `dtcg_json.get("design_principles")` | `data.design_principles` -> `props.designPrinciples` | `designPrinciples?: string[]` (title-cased) |
| `commercial_signal` (system.json; conditional) | `dtcg_json.get("commercial_signal")` | `data.commercial_signal` -> `props.commercialSignal` | `commercialSignal?: string` (title-cased) |
| `mood` (StrippedEntry; always written) | `dtcg_json.get("mood")` | `data.mood` -> `props.mood` | `mood?: string[]` (title-cased) |
| `applicable_to` (StrippedEntry; always written) | `dtcg_json.get("applicable_to")` | `data.applicable_to` -> `props.applicableTo` | `applicableTo?: string[]` (title-cased) |

Notes:
- `design_principles` and `commercial_signal` are omitted from `dtcg_json` when `system.json` was
  not found for a brand (no key at all, not an empty string/list). `_extract_curated_metadata`
  handles absent keys correctly via `.get()`.
- `mood` and `applicable_to` are always written by `build_bundle` (from `StrippedEntry`), even if
  empty lists. An empty list passes `_clean_str_list` as `[]` (present but empty). `BrandMetadataPanel`'s
  `presentList` guard requires `length > 0` before rendering a row, so `[]` degrades identically
  to an absent key from the visitor's perspective.

---

## Web-side file map additions (v4)

```
code/web/app/
  library/
    _components/
      BrandMetadataPanel.tsx     - Curated "About this system" panel. Returns null
                                    when all 6 curated fields are absent or empty
                                    (D11). Title-cases slug values (D12); tier is
                                    verbatim exception. data-testid on every row.
                                    Placed inside .library-header by LibraryPageShell,
                                    preserving the L-16 sticky-CTA containing block.
      build-context.ts           - buildBrandMetadata(): maps LibraryPageData's 6
                                    snake_case curated fields to BrandMetadataPanelProps
                                    camelCase. Returns undefined when none are present.

  tests/
    library-brand-metadata.test.ts  - 292 tests for BrandMetadataPanel: present/absent/
                                       partial/empty/slug-hygiene/tier-verbatim invariants.
```

---

## Assertion-report engine (v4, Phase 3)

Added to support the pre-re-seed preflight (Phase 4) and the post-re-seed proof (Phase 7).

```
app/
  library_assertion_report.py     - Pure assertion engine. Classifies brand API responses
                                     into panel_faithful / panel_cleanly_absent / page_broken.
                                     Exports: BRAND_VERDICT, LIBRARY_ASSERTION_SCHEMA_VERSION
                                     (single-source constant; imported by
                                     library_reseed_verification as _KNOWN_ASSERTION_SCHEMA_VERSION),
                                     BrandAssertion, LibraryAssertionReport,
                                     build_brand_assertion(), build_report(), render_markdown().
                                     Imports CURATED_METADATA_FIELDS from routes.library (NOT
                                     a second hardcoded copy). No DB, no network, no filesystem.

scripts/
  generate_library_assertion_report.py  - CLI: fetch all brands from the live API, run the
                                           engine, write JSON + Markdown to --out-dir.
                                           Exit 0 = all_pass, Exit 1 = broken pages found,
                                           Exit 2 = network/IO error. Retries with backoff.

tests/
  test_library_assertion_report.py      - 35 tests across all 5 canonical states (full-panel,
                                           scalar-light, absent-panel, broken-page, v3-chip-gating)
                                           plus report aggregation + markdown rendering. No network.
```

**Acceptance gate for Phase 7:** `all_pass: True` in the JSON output.

---

## Re-seed verification (v4, Phases A+B)

Added to support pre-apply gate enforcement and post-re-seed reconciliation proof.

```
app/
  library_reseed_verification.py   - Two pure functions + a markdown renderer:
                                      reconcile_reports(predicted, actual) ->
                                        ReconciliationResult
                                        Diffs two LibraryAssertionReport instances.
                                        reconciled=True iff (a) schema versions
                                        match AND equal the known v1 (absolute
                                        guard closes the two-future-v2 gap), (b)
                                        no duplicate brand_slugs in either report,
                                        (c) no verdict drift, (d) no missing brands,
                                        (e) no unexpected brands.
                                        New fields (v4 hardening):
                                          duplicate_in_predicted: list[str]
                                          duplicate_in_actual: list[str]
                                        schema_version="library_reconciliation_v1"
                                      evaluate_ceremony_gates(inputs) ->
                                        CeremonyGoNoGo
                                        Encodes the three pre-apply gates as a
                                        named, auditable go/no-go record.
                                        go=True iff backup_verified AND
                                        dryrun_stable AND preflight_all_pass.
                                        Failed gates are named explicitly.
                                        schema_version="ceremony_gate_v1"
                                      render_reconciliation_markdown(result) -> str
                                        Human-readable Markdown summary of a
                                        ReconciliationResult. Mirrors render_markdown
                                        in library_assertion_report.py in purpose.

scripts/
  reconcile_library_reports.py     - CLI: read two saved assertion-report JSON files
                                      (predicted + actual), run the engine, write
                                      reconciliation.json + reconciliation.md to
                                      --out-dir.
                                      Exit 0 = reconciled, Exit 1 = divergence found,
                                      Exit 2 = IO/JSON error.

tests/
  test_library_reseed_verification.py  - 42+ tests covering all divergence cases
                                          (perfect match, verdict drift, missing
                                          brand, extra brand, count mismatch,
                                          schema-version relative guard, schema-
                                          version absolute guard, duplicate-slug in
                                          predicted/actual) plus ceremony gate (all
                                          pass, each single failure, all fail, output
                                          shape) plus render_reconciliation_markdown.
                                          No network, no DB.
  test_reconcile_library_reports_cli.py - CLI I/O boundary tests: reconciled pair
                                           -> exit 0, divergent pair -> exit 1,
                                           missing file -> exit 2, malformed JSON ->
                                           exit 2, markdown written. tmp_path only.
```

**Usage in Phase C (ceremony):** call `evaluate_ceremony_gates` with the three
boolean gate results before applying any prod mutation.  `go=False` is a hard
stop; `failed_gates` names the exact failure(s).

**Usage in Phase D (post-re-seed proof):** run `scripts/reconcile_library_reports.py`
with the saved preflight predicted report and the live assertion report.  Exit code
0 means `reconciled=True`; exit code 1 means divergence - inspect
`reconciliation.json` for `verdict_drift` / `missing_in_actual` / `unexpected_in_actual` /
`duplicate_in_actual`.

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
| LibraryAssertionReport | `library_assertion_report_v1` | app/library_assertion_report.py |
| ReconciliationResult | `library_reconciliation_v1` | app/library_reseed_verification.py |
| CeremonyGoNoGo | `ceremony_gate_v1` | app/library_reseed_verification.py |

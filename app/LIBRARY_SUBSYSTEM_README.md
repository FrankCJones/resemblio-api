# Library Subsystem - README

**Version:** v2 (2026-06-07)
**Plan:** `projects/OptSus Team/missions/resemblio-library-public-launch-tdd-plan-v2.md`

---

## What this subsystem does

The Library subsystem takes a brand's DTCG token payload and produces per-template rendered HTML pages that demonstrate the brand's design system. It is the backend for the `/library/` route tree on resemblio.com.

The v2 addition (this README's scope): **contract-first presentation with honest graceful degradation.** Every page binds to the full `BRAND_TOKEN_CONTRACT` slot set. Where a brand has REAL captured data for a component group, the component renders faithfully. Where it does not, the component is HIDDEN and a factual notice names the gap. No fabricated placeholders; no silently-empty pages.

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
                               Exposes metadata_json.hub_capture_signal and
                               missing_data_notice to the web BFF.

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
GET /v1/library/brands  -> HubFeaturedRow (brand_slug, category_count, palette, ...)
GET /v1/library/brands/{slug}  -> LibraryPageData (rendered_html, capture info)
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

<!--
schema_version: data_contract_handoff_v1
purpose: Spec for the SEPARATE data-capture project. Populate the source_fields
         named here and each component group flips captured=True in the manifest,
         unlocking the matching showcase category on the Library page.
last_verified: 2026-06-07
-->

# Library v2 - Data Contract Handoff

This document is the spec a separate data-capture project fills to light up
component-showcase categories on the Resemblio Library page.

**Read order:**
- This file first - it tells you WHAT to capture and WHERE to put it.
- `app/brand_capture_manifest.py` - the exact rule each group uses to flip `captured=True`.
- `app/library_render_policy.py` - which showcase categories are gated on which groups.
- `app/LIBRARY_SUBSYSTEM_README.md` - the end-to-end data flow.

---

## The core contract

The Library indexer calls `build_capture_manifest(tokens, button_tokens=button_tokens)`.
That function evaluates 12 component groups. Each group has a named rule (see table below).
When the rule returns True, the group is `captured=True` in the manifest, and showcase
categories requiring that group are rendered on the page instead of being hidden.

**Token bag format:** a flat `{key: value}` dict of CSS property values. Keys may be in
any of three forms; the manifest normalizes all three identically:

| Input format | Example | Normalized to |
|---|---|---|
| DRL seed (`ds-` prefix) | `ds-button-padding-y` | `ds-button-padding-y` |
| Organic (bare) | `button_padding_y` | `ds-button-padding-y` |
| Underscored | `button-padding-y` | `ds-button-padding-y` |

Values are CSS strings (`"10px"`, `"1px"`, `"rgba(0,0,0,0.08)"`, etc.).

---

## Group-by-group capture requirements

### Groups captured by DRL seed brands today

The following groups flip `captured=True` for any brand that supplies its DRL seed
`tokens.css`. No additional capture work needed for these.

| Group | Capture rule | What DRL seeds supply |
|---|---|---|
| `color` | `ds-bg` + `ds-accent` + `ds-text` ALL present | Present in every DRL seed brand's token export |
| `typography` | `ds-font-body` OR `ds-font-display` present (extras outside the contract) | Present in DRL seed brands as font-family extra slots |
| `spacing` | ANY `ds-space-*` slot present | DRL seeds supply the spacing scale |
| `radius` | ANY of `ds-radius-xs/sm/md/lg/full` present | DRL seeds supply the radius scale |
| `layout` | ANY `ds-page-*` slot present | DRL seeds supply page-max + page-pad slots |
| `section` | ANY `ds-section-*` slot present | DRL seeds supply section-padding slots |

**Shadow and motion:** These groups are also "any slot" rules but DRL seed brands
do not currently supply them. They affect only the `shadow` and `motion` template
categories, which are page-pattern templates (not showcase-gated), so they render
unconditionally. Capturing shadow/motion would enrich their rendering but does not
unlock/block any page.

---

### Groups NOT captured by DRL seed brands - THESE need data

These are the four groups that require a data-capture pass before the matching
showcase categories appear on the Library page.

---

#### `button` group

**Showcase categories unlocked when captured:** `buttons`, `library` (partial)

**Capture rule** (`app/brand_capture_manifest._button_captured`):

Path A (preferred): A `ButtonTokens` computed-style snapshot exists on disk for this brand.
Path B (fallback): ALL THREE of the following slots are present in the token bag:

| Source field | Slot | Default | Notes |
|---|---|---|---|
| `button.padding-y` | `ds-button-padding-y` | `10px` | Block padding (top/bottom) |
| `button.padding-x` | `ds-button-padding-x` | `16px` | Inline padding (left/right) |
| `button.border-width` | `ds-button-border-width` | `1px` | Border thickness |

Requiring all three prevents a single coincidental default from triggering a false
capture. Padding defines the button's spatial footprint; border defines its visual
boundary. The radius comes from the already-captured radius scale.

**Recommended capture method:** Path A (computed-style snapshot). The existing
`scripts/capture_all_button_snapshots.py` script does this for all 24 brands.
Run it, verify output at `/var/lib/resemblio/computed_styles/<slug>.json`, then
re-index. For brands where the headless capture fails (SPA wait, selector mismatch),
fall back to Path B by adding the three slots to the brand's token bag.

**Full button slot inventory** (all slots improve rendering fidelity even after
the group flips captured=True):

| Source field | Slot | Default |
|---|---|---|
| `button.padding-y` | `ds-button-padding-y` | `10px` |
| `button.padding-x` | `ds-button-padding-x` | `16px` |
| `button.border-width` | `ds-button-border-width` | `1px` |
| `button.radius` | `ds-button-radius` | `var(--ds-radius-button, var(--ds-radius-sm, 6px))` |
| `button.font-size` | `ds-button-font-size` | `var(--ds-text-sm)` |
| `button.font-weight` | `ds-button-font-weight` | `500` |
| `button.font-family` | `ds-button-font-family` | `var(--ds-font-body)` |
| `button.sm.padding-y` | `ds-button-sm-padding-y` | `6px` |
| `button.sm.padding-x` | `ds-button-sm-padding-x` | `12px` |
| `button.lg.padding-y` | `ds-button-lg-padding-y` | `14px` |
| `button.lg.padding-x` | `ds-button-lg-padding-x` | `22px` |

---

#### `card` group

**Showcase categories unlocked when captured:** `cards`

**Capture rule** (`app/brand_capture_manifest._card_captured`):

BOTH of the following must be true:
1. `ds-card-border-width` is present in the token bag.
2. At least one of `ds-card-padding` or `ds-card-padding-y` is present.

| Source field | Slot | Default | Notes |
|---|---|---|---|
| `card.border-width` | `ds-card-border-width` | `1px` | Required |
| `card.padding` | `ds-card-padding` | `24px` | Required (shorthand) OR |
| `card.padding-y` | `ds-card-padding-y` | `24px` | Required (explicit y) |

Border defines the card's visual boundary; padding defines its interior spatial
footprint. A brand that supplies both has a real, distinct card geometry.

**Recommended capture method:** Computed-style snapshot on a known `.card` selector
(analogous to the button snapshot pipeline). If headless capture is not available,
inspect the brand's CSS for explicit card-class padding and border values.

**Full card slot inventory:**

| Source field | Slot | Default |
|---|---|---|
| `card.border-width` | `ds-card-border-width` | `1px` |
| `card.padding` | `ds-card-padding` | `24px` |
| `card.padding-y` | `ds-card-padding-y` | `24px` |
| `card.padding-x` | `ds-card-padding-x` | `24px` |
| `card.gap` | `ds-card-gap` | `12px` |
| `card.grid-gap` | `ds-card-grid-gap` | `20px` |
| `card.radius` | `ds-card-radius` | `var(--ds-radius-card, var(--ds-radius-md, 8px))` |

---

#### `badge` group

**Showcase categories unlocked when captured:** `badges`

**Capture rule** (`app/brand_capture_manifest._badge_captured`):

BOTH of the following slots must be present:

| Source field | Slot | Default | Notes |
|---|---|---|---|
| `badge.padding-y` | `ds-badge-padding-y` | `3px` | Required |
| `badge.padding-x` | `ds-badge-padding-x` | `10px` | Required |

Both y and x are required together to avoid a partial match triggering a false
positive. Badge chips are defined primarily by their padding (the pill's footprint);
with these two slots + the radius already captured, the badge renders distinctly.

**Recommended capture method:** Computed-style snapshot on a `.badge` or `.chip`
or `.tag` selector. If no badge component exists in the brand's DRL corpus, the
brand genuinely has no captured badge geometry; leave it uncaptured and the
`badges` category stays hidden (honest behavior).

**Full badge slot inventory:**

| Source field | Slot | Default |
|---|---|---|
| `badge.padding-y` | `ds-badge-padding-y` | `3px` |
| `badge.padding-x` | `ds-badge-padding-x` | `10px` |
| `badge.border-width` | `ds-badge-border-width` | `1px` |
| `badge.font-size` | `ds-badge-font-size` | `var(--ds-text-xs)` |
| `badge.font-weight` | `ds-badge-font-weight` | `500` |
| `badge.radius` | `ds-badge-radius` | `var(--ds-radius-badge, var(--ds-radius-full, 9999px))` |
| `badge.sm.padding-y` | `ds-badge-sm-padding-y` | `2px` |
| `badge.sm.padding-x` | `ds-badge-sm-padding-x` | `8px` |
| `badge.lg.padding-y` | `ds-badge-lg-padding-y` | `5px` |
| `badge.lg.padding-x` | `ds-badge-lg-padding-x` | `12px` |

---

#### `input` group

**Showcase categories unlocked when captured:** `form-fields`, `inputs`

**Capture rule** (`app/brand_capture_manifest._input_captured`):

BOTH of the following slots must be present:

| Source field | Slot | Default | Notes |
|---|---|---|---|
| `input.padding-y` | `ds-input-padding-y` | `10px` | Required |
| `input.border-width` | `ds-input-border-width` | `1px` | Required |

These two together define the input's interior spatial footprint (padding) and
visual boundary (border). A brand that supplies both has a real, distinct input
field geometry.

**Recommended capture method:** Computed-style snapshot on an `<input>` or
`.input` selector. Both `form-fields` and `inputs` categories gate on this single
group, so one capture pass unlocks both showcase pages.

**Full input slot inventory:**

| Source field | Slot | Default |
|---|---|---|
| `input.padding-y` | `ds-input-padding-y` | `10px` |
| `input.padding-x` | `ds-input-padding-x` | `12px` |
| `input.border-width` | `ds-input-border-width` | `1px` |
| `input.font-size` | `ds-input-font-size` | `var(--ds-text-base)` |
| `input.font-family` | `ds-input-font-family` | `var(--ds-font-body)` |
| `input.line-height` | `ds-input-line-height` | `1.4` |
| `input.radius` | `ds-input-radius` | `var(--ds-radius-input, var(--ds-radius-sm, 6px))` |

---

## How to deliver captured data

### For token-bag slots (Path B / card / badge / input)

Add the slot key-value pairs to the brand's token dict. The indexer reads this dict
via `tokens_for_compose()` in `library_indexer.py`. The exact format:

```python
# Any of these three key formats work - the manifest normalizes all three.
tokens = {
    "ds-button-padding-y": "12px",   # DRL seed format
    "button_padding_y": "12px",       # underscored organic format
    "button-padding-y": "12px",       # bare organic format
}
```

The standard DRL seed format (`ds-` prefix, hyphenated) is preferred for new data.

### For ButtonTokens snapshots (button Path A - preferred)

Write a `ButtonTokens` JSON file to the runtime data path:

```
/var/lib/resemblio/computed_styles/<brand-slug>.json
```

The `ButtonTokens` TypedDict lives at `extractor/button_tokens.py`. The indexer's
`_load_button_tokens(brand_slug)` in `library_indexer.py` loads from the runtime
path first, falling back to the in-tree seed path at:

```
app/_vendored/drl/drl/_data/computed_styles/<brand-slug>.json
```

See `scripts/capture_all_button_snapshots.py` for the pipeline that produces these
files for all 24 brands.

### After delivering data: re-index

1. Ensure the new token data or snapshot files are in place.
2. Enqueue a re-index job: `python -m scripts.bootstrap_drl_library --apply --brand <slug>`
3. Let the 60-second systemd timer drain the job: `journalctl -u resemblio-library-indexer -f`
4. Verify: `python -m scripts.verify_drl_bootstrap`

The manifest is recomputed on every indexer run. No schema migration needed - adding
slots to a brand's token dict is additive; the manifest re-evaluates automatically.

---

## Current state: DRL seed brands (24 brands)

As of the Phase 0 audit (2026-06-07), all 24 DRL seed brands have:

- `color`: captured (ds-bg + ds-accent + ds-text present in tokens.css)
- `typography`: captured (ds-font-body or ds-font-display present as extras)
- `spacing`: captured
- `radius`: captured
- `layout`: captured
- `section`: captured

All 24 DRL seed brands do NOT have:

- `button`: NOT captured (no computed-style snapshot; button geometry slots absent)
- `card`: NOT captured (card geometry slots absent)
- `badge`: NOT captured (badge geometry slots absent)
- `input`: NOT captured (input geometry slots absent)

This means the following showcase categories are currently hidden for all 24 brands:
`buttons`, `cards`, `badges`, `form-fields`, `inputs`, `library`.

The `library` composite category also requires ANY of button/card/badge; it is hidden
until at least one of those groups is captured per brand.

Page-pattern categories (hero, navigation, footer, alphabet, etc.) render for all 24
brands unconditionally - they demonstrate layout, not component geometry.

---

## The invariant this handoff protects

`evaluate_category_render` in `app/library_render_policy.py` is the gating function.
The test `TestD2Invariant.test_uncaptured_button_decision_is_no_render` in
`tests/test_library_render_policy.py` pins this invariant.

The invariant: **a component showcase category NEVER renders with fabricated
defaults when the brand has no real geometry data.** Better to show a factual
"Not yet captured" notice than a generic generic placeholder labeled as a brand.

Populating the source_fields above turns that notice into a faithful brand rendering.
That is the upgrade path.

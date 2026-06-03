# DRL key-shape inventory (aeon_min fixture)

Source: real Aeon DRL at `projects/Design Reference Library/assets/libraries/aeon/tokens.css`
(read-only; not modified).

## How DRL keys are produced

`scripts/seed_from_drl.py:parse_tokens_css` matches CSS custom properties with
the regex `--([a-zA-Z0-9_-]+)\s*:\s*([^;]+);`. The leading `--` is stripped;
the rest of the identifier survives intact. Because the DRL `tokens.css`
contract (per `projects/Design Reference Library/TOKEN_CONTRACT.md`) namespaces
every variable as `--ds-*`, the parser always emits keys of shape `ds-<rest>`.

The token dict is then embedded in the DTCG bundle under
`dtcg_json["tokens"]` (see `build_bundle`), so downstream callers see a
**nested** shape: the top-level DTCG payload carries a `tokens` sub-dict.

`app.library_indexer.tokens_for_compose` accepts both the nested DRL shape
(seed rows) and a flat top-level shape (organic rows, which the
`bundle_from_token_set` helper writes without nesting). The fixture covers
both - one nested DRL-shape file and one flat-shape variant used in the
table-driven `_metadata_for` test.

## Key-shape inventory

Every shape below is a real-or-realistic example a DRL `tokens.css` is
known to produce. The fixture contains at least one example of each.

| # | Shape | Example | Where in fixture |
|---|---|---|---|
| 1 | bare alphabetic (no namespace, single word) | `bg` | mixed_keys.json (synthetic; organic shape) |
| 2 | bare alphabetic + underscore | `font_display` | mixed_keys.json (synthetic; legacy organic shape) |
| 3 | namespaced, single segment | `ds-bg` | aeon_dtcg.json (nested) |
| 4 | namespaced, hyphenated multi-segment | `ds-text-muted` | aeon_dtcg.json |
| 5 | namespaced, hyphen + alphanumeric suffix | `ds-text-2xs` | aeon_dtcg.json |
| 6 | namespaced, ending with digit | `ds-space-0`, `ds-space-32` | aeon_dtcg.json |
| 7 | namespaced, multi-segment hyphenated | `ds-font-display` | aeon_dtcg.json |
| 8 | namespaced, scale word suffix | `ds-shadow-md`, `ds-radius-full` | aeon_dtcg.json |
| 9 | namespaced, semantic group word | `ds-ease-standard`, `ds-duration-instant` | aeon_dtcg.json |
| 10 | mixed bare + namespaced in one bag | (combined) | mixed_keys.json (metadata test) |

## Shapes NOT covered (with rationale)

- **Nested dict values** (e.g. `tokens["patterns"] = ["foo"]`).
  `tokens_for_compose` explicitly filters non-string/number values; the
  flat output it returns will never carry a nested structure. Out of
  scope for token-projection assertions.
- **Asset references** (e.g. `tokens["logo"] = "asset://..."`).
  `parse_tokens_css` only consumes CSS custom-property declarations; an
  asset reference would arrive through a different path (DTCG `assets`
  block, not `tokens`). The DRL parser never emits this key class, so
  the indexer's compose path never sees it.
- **Camel-case keys** (e.g. `dsBg`). The `_CSS_VAR_PATTERN` regex would
  match them, but the DRL `TOKEN_CONTRACT.md` mandates kebab-case + `ds-`
  prefix and no DRL `tokens.css` on disk uses camel case. The
  normalization helper `_ds_var_name` only handles `_` -> `-`, not
  camel -> kebab, by design.

## Use in tests

- `tests/test_library_indexer_render_fidelity.py` loads `aeon_dtcg.json`,
  inserts an `AssetVersion` row whose `dtcg_json` is that payload, drains
  the indexer queue, and asserts every key in `aeon_dtcg.json["tokens"]`
  appears exactly once as a `--ds-<key>:` declaration in the rendered
  HTML, with the namespaced shape preserved (no `--ds-ds-bg`).
- `tests/test_metadata_for_drl_keys.py` table-drives `_metadata_for`
  over `{bare_keys, ds_prefixed_keys, mixed_keys}` (loaded from
  `mixed_keys.json`) and asserts the OG envelope is identical regardless
  of input key shape. This is bug 11 from the failure trail: `_metadata_for`
  currently only reads bare keys and returns nulls for DRL `ds-` keys.

# extractor

Pure-data extraction primitives consumed by the Resemblio API. Every module here
is deterministic and unit-tested unless explicitly flagged as a live-browser pass.

## File map

| File | Role | Tests |
|---|---|---|
| `codex_extractor.py` | LLM-driven extraction orchestrator. Composes all signal blocks + raw HTML and dispatches one Sonnet call for a DTCG token bundle. | (grandfathered) |
| `cli.py` | CLI surface for ad-hoc extraction runs. | (grandfathered) |
| `drl_adapter.py` | Bridge between the LLM output and the vendored DRL token shape. | (grandfathered) |
| `css_root_parser.py` | R3.1 pure-data pass. Parses ``:root``/``html``/``body`` CSS custom-property declarations from inline ``<style>`` blocks. Surfaces all ``--*`` vars to the prompt as brand INTENT. | `tests/test_css_root_parser.py` |
| `style_digest.py` | R3.1 cascade resolver. Resolves ``var()`` references against the parsed root-props map and extracts explicit slot values (bg, text, accent, font_body, font_display) from key CSS rules. Renders a "VERIFIED STYLE DIGEST" prompt block with full provenance. | `tests/test_style_digest.py` |
| `font_link_parser.py` | R3.1 pure-data pass. Parses ``<link>`` and ``@font-face`` declarations from raw HTML. Surfaces web-font family names to the prompt. | `tests/test_font_link_parser.py` |
| `computed_styles.py` | R3.1 live-browser pass. Playwright renders the page and reports resolved CSS values for a small element census. Gracefully degrades to ``status="unavailable"`` when Playwright is missing or disabled. | `tests/test_computed_styles.py` |
| `screenshot_palette.py` | A1.1 rendered-palette cross-check. Playwright captures a screenshot and extracts dominant colors not already present in the declared signals. Closes the WP+page-builder pathology. | `tests/test_screenshot_palette.py` |
| `confidence_rubric.py` | S20 confidence rubric. Scores the extracted token set on palette diversity, CMS-default matches, font specificity, and screenshot consistency. Returned on every extraction. | `tests/test_confidence_rubric.py` |
| `known_cms_defaults.py` | CMS-default palette and font databases (Gutenberg, Shopify Dawn, Squarespace, Webflow, Wix). Referenced by ``confidence_rubric.py``. | `tests/test_confidence_rubric.py` |
| `button_tokens.py` | Hybrid Path B derivation. Reads the ``cta`` slot of a ``ComputedStyleReport`` and returns typed ``ButtonTokens``. | `tests/test_button_tokens.py` |
| `button_override.py` | Hybrid Path B injection. Appends a brand-specific ``.b-btn { ... !important }`` block to composed DRL HTML. Idempotent. | `tests/test_button_override.py` |
| `token_contract.py` | Token validation helpers and shape contracts shared across the extractor layer. | (see extractor_imports test) |

## Data flow

```
URL --> fetch HTML (full)
           |
           +---> css_root_parser.parse_root_custom_properties()  --> RootCustomProperties
           |                                                               |
           +---> style_digest.build_style_digest(html, root_props) --> StyleDigest
           |                                                               |
           +---> font_link_parser.parse_loaded_fonts()            --> LoadedFonts
           |                                                               |
           +---> computed_styles.capture_computed_styles()  [opt] --> ComputedStyleReport
           |                                                               |
           +---> screenshot_palette.capture_screenshot_palette() [opt] -> ScreenshotPaletteReport
           |                                                               |
           +--> codex_extractor.build_prompt(url, html_truncated, all signals)
                                                           |
                                                           v
                                                    Sonnet LLM call
                                                           |
                                                           v
                                                       TokenSet (validated)
                                                           |
                                                           v
                                              confidence_rubric.compute_confidence_rubric()
                                                           |
                                                           v
                                                       DTCG bundle
                                                           |
                                          (library compose path below)
                                                           |
                                                           v
ComputedStyleReport --> button_tokens.derive_button_tokens() --> ButtonTokens
                                                                      |
composed HTML --> button_override.apply_button_tokens() --------------+
                                                                      v
                                                            composed HTML + per-brand .b-btn
```

## Contracts

- All `*_v1` / `*_v1.*` shapes carry an explicit `schema_version` field. Bumps require a new constant and a test update.
- `css_root_parser.RootCustomProperties` is the canonical raw-vars envelope; `properties_by_name` is the fast-lookup map.
- `style_digest.StyleDigest` is the cascade-resolved digest envelope; `resolved_slots` lists SlotValue items with slot name, resolved value, and source rule.
- `computed_styles.ComputedStyleReport` is the canonical computed-signal envelope; `signals[].slot` values are stable identifiers (`cta`, `heading`, etc.).
- `button_tokens.derive_button_tokens()` returns `None` rather than raising on malformed input.
- `button_override.inject_button_override()` is idempotent (uses `OVERRIDE_MARKER` to detect prior injection).

## Related decisions

- R3.1 extractor surgery: `projects/OptSus Team/missions/resemblio-r3.1-extractor-surgery-v1.md` (strategic brief) and `projects/OptSus Team/missions/resemblio-r3.1-tdd-execution-plan-v1.md` (TDD execution plan).
- CTO 2026-06-02 "Resemblio button fidelity fix" packet: `projects/OptSus Team/cto-reviews/2026-06-02-resemblio-button-fidelity-fix.md`.

## Subsystem-level rules

- Quality floor applies (`CLAUDE.md > Quality floor`). No grandfather clause inside this folder.
- Single dashes only. No em-dashes. No "nestled."
- Live-browser code (`computed_styles`, `screenshot_palette`) is opt-in behind
  `RESEMBLIO_DISABLE_BROWSER_PASS=1` in tests so CI stays hermetic.
- `style_digest.build_style_digest` accepts an optional pre-parsed `root_props` to avoid
  double-parsing when the caller already ran `parse_root_custom_properties`.

schema_version: `extractor_readme_v2`

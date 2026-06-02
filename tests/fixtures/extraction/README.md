# Extraction-fidelity fixtures

Ground-truth fixture set for the Resemblio extractor. Each subdirectory is one fixture.

**Schema:** `extraction_fidelity_fixture_v1`
**Authored:** 2026-06-02 (R3 mission)
**Source mission:** `projects/OptSus Team/missions/resemblio-r3-extraction-fidelity-v1.md`
**Source finding:** `projects/Resemblio/02-prd/2026-05-31-extraction-fidelity-finding-susann.md`

## File map

Each fixture directory carries:

- `source.html` — the input the extractor would receive (or a pre-rendered DOM for JS-heavy fixtures)
- `ground_truth.json` — the human-authored expected DTCG TokenSet, including a `_provenance` block

## Data flow

1. A fidelity test loads `source.html` and the human-authored `ground_truth.json` from the directory.
2. The test feeds `source.html` through a configurable extractor (real or stubbed) to produce an actual TokenSet.
3. The test asserts the actual TokenSet matches the ground truth within per-field tolerance (see `tolerance.py`).

In v1 the fidelity tests run with a STUB extractor configured per-fixture by the test harness; the actual extractor (`code/extractor/codex_extractor.py`) is exercised by separate end-to-end tests. This separates "is the rubric tuned correctly" from "does the extractor work" — both questions land in this test scaffold; R3.1 is the surgery mission that uses these fixtures to validate extractor changes.

## Fixture inventory

| ID | Name | Failure mode it catches |
|---|---|---|
| 001 | susann_headlights | Dark concept with Google Fonts + CSS custom properties; the canonical failure case |
| 002 | dark_dramatic_concept | A different dark + accent-driven design (no overfit on Susann) |
| 003 | light_minimal_typographic | Clean light design (positive control) |
| 004 | wix_js_heavy | JS-rendered DOM (client-side font and color application) |
| 005 | webfont_via_link_tag | Fonts loaded ONLY from CDN via `<link>` in `<head>` |
| 006 | accent_on_cta_only | Accent color appears on a SINGLE CTA button (frequency-weighting hazard) |
| 007 | marketing_complex | Multi-section marketing page |
| 008 | blog_long_form | Content-heavy blog layout |
| 009 | dashboard_saas | Utility app aesthetic |
| 010 | default_html_baseline | Plain default HTML; ground truth IS the system-default values (low-quality flag must fire) |

## Authoring guide

When adding a new fixture:

1. Choose the failure mode you want to catch and write it as the directory name (`NNN_short_slug`).
2. Author `source.html` as the smallest realistic page that expresses the design system.
3. Hand-author `ground_truth.json` to the DTCG-flat TokenSet shape (see existing fixtures for keys).
4. Include `_provenance` with `author`, `date_iso`, `source_concept` (path or URL if real-world), and `failure_mode_caught` (short prose).
5. Register the fixture in `test_extraction_fidelity.py` by adding the directory name to the parametrize list.

Magic numbers (color Delta-E tolerance, score thresholds) live in `app/constants.py`; do not hard-code them in fixtures.

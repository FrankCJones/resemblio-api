# aeon_min - frozen minimal DRL brand fixture

Trimmed Aeon library used to pin the library-indexer compose pipeline against
its own inputs. Sourced from the read-only DRL corpus at
`projects/Design Reference Library/assets/libraries/aeon/tokens.css`; the
trim keeps one example of every key-shape variant the DRL parser emits.

## File map

| File | Purpose |
|---|---|
| `KEY_SHAPE_CHECKLIST.md` | Inventory of every DRL key shape with one fixture example each, plus rationale for shapes deliberately not covered. Read this first. |
| `aeon_dtcg.json` | One full DTCG payload (nested under `tokens`, DRL `ds-`-prefixed keys). Loaded by `test_library_indexer_render_fidelity.py`. |
| `mixed_keys.json` | Three parallel token bags (bare / ds-prefixed / mixed). Loaded by `test_metadata_for_drl_keys.py`. |

## Why this fixture exists

CTO TDD recovery plan, Phase 1 (`projects/OptSus Team/cto-reviews/2026-06-02-resemblio-library-tdd-recovery.md`).
The render pipeline historically accepted tokens and then composed HTML that
did not actually project them; lorem placeholders survived and the
`_metadata_for` OG envelope read the wrong key shape entirely. The fixture
plus its two test files lock the contract: every key in the input appears
in the output, exactly once, in the namespaced form, with no double-prefix.

## What to change here

- New key shape from a future DRL parser change -> add an example to
  `aeon_dtcg.json` AND a row to `KEY_SHAPE_CHECKLIST.md`. Both must stay
  in sync; the checklist is the discoverability surface.
- New OG envelope field added to `_metadata_for` -> extend
  `mixed_keys.json` so the table-driven equivalence test still covers it.
- Do NOT remove keys without removing the corresponding checklist row;
  shrinking coverage without recording it is the prohibited shortcut.

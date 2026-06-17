# a24_button - frozen DRL asset fixture for the walking skeleton

Vendored copy of `projects/Design Reference Library/assets/atoms/buttons/a24-cinematic-001/asset.html`.

## Why this fixture exists

Issue #4 (walking skeleton) drives the real `a24-cinematic-001` asset through
the full seed-to-indexer pipeline on in-memory SQLite. CI checks out only the
API repo, so the DRL workspace (`projects/Design Reference Library/`) is absent.
This vendored copy lets the integration test run in CI with bit-for-bit
identical inputs to what the workspace run uses.

The test (`tests/test_walking_skeleton_a24_buttons.py`) tries the real DRL path
first; if absent, it reads this file. Both paths produce identical test results
because this file is copied verbatim from the DRL source.

## Why a24 is the skeleton target

A24 owns exactly ONE button asset in the DRL corpus (`a24-cinematic-001`),
eliminating canonical-selection ambiguity. The multi-atom case (anthropic has 4,
linear has 2) is tracked in issues #5 and #6. The single-asset case is the
cleanest proof for the walking skeleton.

The asset exercises all five acceptance criteria:
- `.btn` - the primary button class
- `.btn:hover` - the warm-step hover interaction
- `.btn:focus-visible` - the ink offset focus ring
- `data-rs-source="drl-component"` - the real-component contract marker
- Absence of `.b-btn` - the generic chiclet, which must NOT appear

## Sync policy

Update this file if the upstream DRL asset changes AND if the change would
affect the acceptance-criteria markers (`.btn`, `:hover`, `:focus-visible`).
Cosmetic changes to the DRL asset (comments, state-label copy) do not require
a sync. Record any sync in a commit message referencing both files.

DRL is read-only from this project: never write back.

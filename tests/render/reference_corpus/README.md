# Structural Fidelity Corpus (in-repo CI mirror)

This directory is the in-repo mirror of the structural fidelity text corpus.
It exists so the visual fidelity gate's **structural tier** runs on a standalone
CI checkout of `resemblio-api`, without requiring the full workspace
`_verification/` tree.

## What is vendored here

| File | Purpose |
|---|---|
| `tolerance_config.yml` | Gate tolerance knobs (ssim_floor, color_bucket_overlap_min, etc.) |
| `fidelity_targets.yml` | Per-tuple viewport and capture settings |
| `reference_captures/manifest.json` | Manifest of reference records (brand, category, viewport tuples) |
| `reference_captures/specs/*.json` | Per-(brand, category) structural assertions: font-family checks, text_content expectations |

**20 spec JSON files** total (2026-06-13 baseline).

## What is deliberately NOT here

**Brand-site PNGs** (`*.png` - reference screenshots of apple.com, stripe.com,
vercel.com, etc.) are NOT committed and are blocked by `.gitignore`.

Two reasons, either fatal on their own:

1. **Trademark / inspirado-no-copiado posture.** Resemblio renders brand-stripped
   type specimens inspired by real brand design systems, not copies. Committing
   full-page screenshots of brand homepages into a public repo directly contradicts
   that posture.

2. **Repo bloat.** 62+ PNGs totaling ~100 MB have no place in a public API repo.

The SSIM pixel path is informational-only per decision D-5.1 (locked 2026-06-13,
Opus/Jim): structural dimensions (color-bucket overlap + font-family assertion)
are the PRIMARY gate. The gate does not need PNGs to render its verdict.

PNGs stay in the workspace `_verification/` tree for human contact-sheet review
and full pixel sweeps on the gate-run box.

## Two-tier CI design (Phase 8)

| Tier | What | Runs on CI? | Needs |
|---|---|---|---|
| Structural unit tests | `test_linear_font_spec_*`, font resolution, Phase 5.1 gate-basis tests | **YES** (this corpus) | Text artifacts in this directory |
| Full-corpus live sweep | `test_library_render_within_tolerance_of_brand_reference` | **NO** (self-skips) | Live network + Basic Auth + PNGs |

## Authoring source and sync direction

The **workspace `_verification/` tree** is the authoring source:

```
projects/Resemblio/_verification/library-inspirado-correction-20260604/
```

After a gate run updates the specs, re-sync the in-repo mirror:

```bash
# From the workspace root (not from inside code/api)
python projects/Resemblio/code/api/scripts/sync_fidelity_corpus.py
```

The sync helper copies text artifacts from the workspace authoring tree into
this directory, refuses to copy any `*.png`, and is a no-op when they already
match. Commit the updated in-repo mirror after syncing.

## Drift guard

`tests/render/test_corpus_drift.py` checks that the vendored specs match their
workspace originals when both are present (dev machines). On CI the workspace
tree is absent, so the drift guard self-skips.

If you see drift guard failures: run `scripts/sync_fidelity_corpus.py` and commit.

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

### How each tier resolves its inputs

The structural tier reads `tolerance_config.yml` + `reference_captures/specs/`
from `CORPUS_ROOT` - this in-repo copy on CI, or the workspace tree on a dev
machine running the full gate. Either way the text is present, so the structural
tier RUNS on CI. That is the Phase 8 deliverable.

The live sweep reads `reference_captures/manifest.json`, and `load_manifest`
resolves each record's PNG relative to the manifest's parent directory. Because
the PNGs live ONLY in the workspace tree, the manifest root decides whether the
sweep can find PNGs:

- **Default** (`FIDELITY_LIVE_SWEEP` unset): manifest resolves to THIS in-repo
  copy, which has no PNGs -> `load_manifest` returns zero records -> the sweep
  SKIPS. This keeps a bare `pytest -q` safe on CI and on dev (no slow, flaky
  network captures).
- **Opt-in** (`FIDELITY_LIVE_SWEEP=1`): manifest resolves to the workspace
  `_verification/` tree, whose PNGs are co-located -> the sweep runs. This is
  the gate-run-box / scheduled-job mode. See `conftest.resolve_manifest_path`.

The in-repo `manifest.json` is still vendored: it is the default (PNG-less)
manifest that produces the clean skip, and it enumerates the (brand, category,
viewport) tuples for any future PNG-free structural enumeration on CI.

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

## Structural-only specs

Some specs in `reference_captures/specs/` have no matching entry in
`manifest.json`. These are **structural-only specs**: font-family assertions
are authored and pass CI, but no reference PNG was ever shot for them.

The 8 structural-only specs as of the Phase 9.4 baseline (2026-06-13):

| Brand | Category | Reason no PNG exists |
|---|---|---|
| aeon | about-team | aeon.co deploys Cloudflare challenge on automated requests |
| aeon | alphabet | same as above |
| aeon | buttons | same as above |
| openai | about-team | Cloudflare Turnstile gates the about-team page |
| openai | buttons | Cloudflare Turnstile gates the buttons-source page |
| stripe | about-team | Stripe pages vary by geo/A/B cohort; deferred pending stable target |
| stripe | alphabet | same as above |
| stripe | buttons | same as above |

These tuples are declared in `STRUCTURAL_ONLY_SPECS` in
`tests/render/test_corpus_consistency.py`. The consistency contract
(`test_every_spec_is_manifest_backed_or_declared_structural_only`) enforces
that every spec is either manifest-backed or explicitly listed there - so a
genuinely missing capture is distinguishable from an intentionally PNG-less
spec.

**If you add a reference capture for one of these brands later:**
1. Remove the entry from `STRUCTURAL_ONLY_SPECS` in `test_corpus_consistency.py`.
2. Add the brand tuple to `fidelity_targets.yml` and shoot the PNG.
3. Re-run `scripts/sync_fidelity_corpus.py` and commit the updated manifest.

The PNG itself never enters this public repo (trademark constraint; D-5.1).
Only the manifest entry changes; the PNG stays in the workspace `_verification/` tree.

## Drift guard

`tests/render/test_corpus_drift.py` checks that the vendored specs match their
workspace originals when both are present (dev machines). On CI the workspace
tree is absent, so the drift guard self-skips.

If you see drift guard failures: run `scripts/sync_fidelity_corpus.py` and commit.

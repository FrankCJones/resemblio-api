# _vendored/drl_corpus/

Read-only DRL content snapshot vendored into this repo so the seed script
and fidelity gate can run on a bare CI checkout without the workspace DRL
tree.

## What is here

| Path | Description |
|---|---|
| `corpus.json` | Flat asset catalogue (974 assets, 41 systems) |
| `systems/<brand>/system.json` | Per-system design metadata (40 of 41 brands) |
| `assets/<class>/<slug>/asset.html` | Real component markup (974 files) |
| `assets/<class>/<slug>/tokens.css` | CSS custom-property tokens (974 files) |
| `manifest.json` | Per-file sha256 integrity seal |
| `VERSION` | Provenance: source + vendored timestamp + corpus generation date |

Total: 1,989 files, ~11.6 MB.

## What is NOT here

- PNG screenshots (brand-site trademark constraint; never commit to this repo)
- The DRL `_scripts/` Python package (that is vendored separately in `../_vendored/drl/`)
- Any file not directly referenced by `corpus.json`

## The DRL is read-only

The Design Reference Library (`projects/Design Reference Library/`) is an
upstream, hand-authored corpus. Resemblio pulls from it and never writes
back. The sync script (`scripts/sync_drl_corpus.py`) enforces this at
runtime with a `verify_drl_untouched` guard that hard-fails if any
destination path falls inside the DRL root.

## How to refresh the snapshot

When the DRL is updated (new assets added, HTML revised), re-vendor:

```bash
# From code/api/
python scripts/sync_drl_corpus.py
```

Then commit the result. The script is idempotent: unchanged files are
skipped, and `manifest.json` is timestamp-free so re-running with the same
DRL produces a byte-identical manifest.

To preview what would change without writing:

```bash
python scripts/sync_drl_corpus.py --dry-run
```

## How the seed uses this

Pass the vendored root as `--drl-root`:

```bash
python scripts/seed_from_drl.py --drl-root _vendored/drl_corpus [other flags]
```

The layout mirrors the DRL exactly, so every `load_corpus`, `load_asset_html`,
`load_tokens_for_asset`, and `load_system_json` call works without modification.

## Integrity check

```bash
pytest tests/test_drl_corpus_vendored.py -q
```

Tests verify: directory present, corpus.json present, manifest sha256 matches
every file, seed loaders succeed against this root, manifest is idempotent.

## Precedents

| Vendored item | Location | Script |
|---|---|---|
| DRL `_scripts/` package | `_vendored/drl/` | manual copy + VERSION |
| Fidelity text corpus | `tests/render/reference_corpus/` | `scripts/sync_fidelity_corpus.py` |
| DRL content snapshot | `_vendored/drl_corpus/` (this dir) | `scripts/sync_drl_corpus.py` |

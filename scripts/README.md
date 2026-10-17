# scripts

Operational and one-shot scripts for the Resemblio API. Every script is run via `python -m scripts.<name>` from `projects/Resemblio/code/api/` so the `app.*` package imports resolve cleanly.

## File map

- `compile_apple_evidence.py`: compile or verify the canonical 41-record public Apple artifact and separate private evidence ledger. The default `check` action requires the checked-in public artifact and verifies a private ledger when one exists. The `write` action changes only the two generated local files.
- `bootstrap_drl_library.py`: discover every brand under `_vendored/drl/drl/_extractions/` and dispatch per-brand `seed_from_drl.apply_seed` runs. The optional apply mode writes. The default is a dry run.
- `seed_from_drl.py`: bulk-seed `extractions` and `asset_versions` rows from the Design Reference Library `corpus.json`. The source-system option scopes a run to one brand. The optional apply mode writes.
- `verify_drl_bootstrap.py`: assert that every expected brand has rows and canonical `library_pages`. This is always read-only.
- `refresh_brand_library.py`: drop, bootstrap, and drain one or every brand's library pages after a snapshot refresh. The optional apply mode writes.
- `reconcile_library_alias_canonicals.py`: classify and optionally flip stale library canonical flags when generic canonical rows mask marker-backed component rows. The default is a dry run.
- `capture_all_button_snapshots.py`: capture R3.1 computed-style snapshots for every DRL brand into `_vendored/drl/drl/_data/computed_styles/<slug>.json`. The optional apply mode writes.
- `full_corpus_refresh.sh`: snapshot all brands, then drop, bootstrap, and drain. This is always live and has no dry-run mode.
- `backfill_stripe_customers.py`: ensure every production `users` row has a Stripe customer in live and test modes. The optional apply mode writes.
- `migrate_sqlite_to_postgres.py`: one-shot v1 to v1.1 migration. It predates the Postgres cutover and has a manual gate.
- `create_first_user.py`: bootstrap the first admin user on a fresh database under a manual gate.
- `save_extraction.py`: run one extraction end to end and persist it to the development database.
- `smoke_stripe_mode.py`: verify that Stripe key mode matches the expected test or live mode. This is read-only.
- `smoke_wave3_user_flow.py`: run the Wave 3 user-flow smoke inside test mode only.
- `refund_reconcile.py`: count the prior day's failed extractions against paired refund-ledger rows and send a Resend alert on nonzero drift. Pure-data logic lives in `app/refund_reconcile.py`.

## Data flow

```
DRL corpus.json + _extractions/  to  bootstrap_drl_library.py
                                              |
                                              v
                                     seed_from_drl.apply_seed (per brand)
                                              |
                                              v
                                     extractions + asset_versions rows
                                              |
                                              v
                              library_index_jobs (queue, FIFO)
                                              |
                                              v   (60s systemd timer)
                                     library_indexer.drain_pending
                                              |
                                              v
                                       library_pages rows
                                              |
                          /library/<brand>/<category>/ ISR pages

capture_all_button_snapshots.py  to  _vendored/drl/drl/_data/computed_styles/<slug>.json
                                                              |
                                                              v
                          refresh_brand_library.py  to  drop + re-bootstrap + drain
                                                              |
                                                              v
                          library_indexer._load_button_tokens picks new snapshot
```

## Contracts

- Every write-mode script defaults to a dry run; apply mode is the explicit opt-in.
- Bootstrap + seed are content-hash-dedup safe; re-running on unchanged input mutates zero rows.
- Snapshot files use schema `r3_1_computed_style_snapshot_v1` (see `extractor/computed_styles.py`).
- `full_corpus_refresh.sh` reads SSH conventions from `infra/box-resemblio-prod-01.yaml` per the workspace per-box source-of-truth rule.

## Run

From `projects/Resemblio/code/api/`:

```bash
# Verify deterministic Apple artifacts
python -m scripts.compile_apple_evidence check

# Regenerate after reviewed evidence or compiler changes
python -m scripts.compile_apple_evidence write

# Dry-run a full DRL bootstrap (prints plan only)
python -m scripts.bootstrap_drl_library

# Full corpus refresh on prod (orchestrator)
bash scripts/full_corpus_refresh.sh
```

## Subsystem-level rules

- Quality floor applies (workspace `CLAUDE.md > Quality floor`). No grandfather inside this folder except where the file header says `one-shot` or `grandfathered`.
- Use single dashes and never use the prohibited workspace term.
- Live-browser scripts (snapshot capture) must run with `RESEMBLIO_RUN_REAL_BROWSER=1`; CI never invokes them.
- Write-mode scripts MUST default to a dry run. Apply mode is the safety gate.

schema_version: `resemblio_scripts_readme_v1`

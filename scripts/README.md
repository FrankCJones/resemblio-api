# scripts

Operational and one-shot scripts for the Resemblio API. Every script is run via `python -m scripts.<name>` from `projects/Resemblio/code/api/` so the `app.*` package imports resolve cleanly.

## File map

| File | Role | Idempotent | Run mode |
|---|---|---|---|
| `bootstrap_drl_library.py` | Discover every brand under `_vendored/drl/drl/_extractions/` and dispatch per-brand `seed_from_drl.apply_seed` runs. Anchors brand discovery on the DRL `_extractions/` directory. | yes (`content_hash` dedup) | `--apply` writes; default is dry-run |
| `seed_from_drl.py` | Bulk-seed `extractions` + `asset_versions` rows from the Design Reference Library `corpus.json`. Supports `--source-system <slug>` to scope to one brand. | yes | `--apply` writes; default prints plan |
| `verify_drl_bootstrap.py` | Post-bootstrap probe: assert every expected brand has rows + canonical `library_pages`. Read-only. | n/a | always read-only |
| `refresh_brand_library.py` | Drop + bootstrap + drain one (or every) brand's library pages after a snapshot refresh. | yes | `--apply` writes |
| `capture_all_button_snapshots.py` | Capture R3.1 computed-style snapshots for every DRL brand into `_vendored/drl/drl/_data/computed_styles/<slug>.json`. Live-browser pass via Playwright. | yes (overwrite per brand) | `--apply` writes |
| `full_corpus_refresh.sh` | End-to-end orchestrator: snapshots all brands then drop+bootstrap+drain. Closes the Apple-only-snapshot gap from OPS 8.11. | yes | always live (no dry-run mode) |
| `backfill_stripe_customers.py` | One-time reconciliation: ensure every prod `users` row has a Stripe customer in both LIVE + TEST modes. Read-then-conditionally-write. | yes | `--apply` writes |
| `migrate_sqlite_to_postgres.py` | One-shot v1->v1.1 migration. Grandfathered (predates the Postgres cutover). | one-shot | manual gate |
| `create_first_user.py` | Bootstrap the first admin user on a fresh DB. | one-shot | manual gate |
| `save_extraction.py` | Local-dev helper: run one extraction end-to-end and persist to the dev DB. | n/a | local-only |
| `smoke_stripe_mode.py` | Pre-flight: verify Stripe key mode matches expected (TEST vs LIVE). Read-only probe used by the cutover playbook. | n/a | always read-only |
| `smoke_wave3_user_flow.py` | Wave 3 user-flow smoke. Read-then-conditional-write inside test mode only. | n/a | gated by `STRIPE_MODE=test` assertion |

## Data flow

```
DRL corpus.json + _extractions/  -->  bootstrap_drl_library.py
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

capture_all_button_snapshots.py  -->  _vendored/drl/drl/_data/computed_styles/<slug>.json
                                                              |
                                                              v
                          refresh_brand_library.py  -->  drop + re-bootstrap + drain
                                                              |
                                                              v
                          library_indexer._load_button_tokens picks new snapshot
```

## Contracts

- Every write-mode script defaults to dry-run; `--apply` is the explicit opt-in.
- Bootstrap + seed are content-hash-dedup safe; re-running on unchanged input mutates zero rows.
- Snapshot files use schema `r3_1_computed_style_snapshot_v1` (see `extractor/computed_styles.py`).
- `full_corpus_refresh.sh` reads SSH conventions from `infra/box-resemblio-prod-01.yaml` per the workspace per-box source-of-truth rule.

## Run

From `projects/Resemblio/code/api/`:

```bash
# Dry-run a full DRL bootstrap (prints plan only)
python -m scripts.bootstrap_drl_library

# Apply (writes to Postgres + R2)
python -m scripts.bootstrap_drl_library --apply

# Refresh one brand after snapshot capture
python -m scripts.refresh_brand_library --brand apple --apply

# Full corpus refresh on prod (orchestrator)
bash scripts/full_corpus_refresh.sh
```

## Subsystem-level rules

- Quality floor applies (workspace `CLAUDE.md > Quality floor`). No grandfather inside this folder except where the file header says `one-shot` or `grandfathered`.
- Single dashes only. No em-dashes. No "nestled."
- Live-browser scripts (snapshot capture) must run with `RESEMBLIO_RUN_REAL_BROWSER=1`; CI never invokes them.
- Write-mode scripts MUST default to dry-run. The `--apply` flag is the safety gate.

schema_version: `resemblio_scripts_readme_v1`

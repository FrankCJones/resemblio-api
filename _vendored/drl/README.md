# Vendored DRL modules

This directory vendors the DRL Python modules that the Resemblio extractor imports at runtime:

- `_scripts.extraction`
- `_scripts.fetch_html`
- `_scripts.recon`
- `_scripts.recon_ping`

The production API deploy cannot read `projects/Design Reference Library/`, so these copies let `POST /v1/extractions` run from the API repo alone.

## Source rule

The Design Reference Library is upstream and read-only from Resemblio. Never write back to `projects/Design Reference Library/` from this project. Treat the files here as frozen copies used by the API runtime.

## Refresh procedure

1. Re-copy these four files from `projects/Design Reference Library/_scripts/`.
2. Update `VERSION` with the source snapshot and the new vendoring timestamp.
3. Re-run the vendored DRL unit tests.
4. Re-run the extraction integration test with `RESEMBLIO_INTEGRATION_TESTS=1`.

The Resemblio project rules forbid modifying anything under `projects/Design Reference Library/`. Vendoring is copy-out only.

# CODEX Report DRL Vendor

## Shipped

- Added `_vendored/drl/` under the API repo with verbatim copies of:
  - `_scripts.extraction`
  - `_scripts.fetch_html`
  - `_scripts.recon`
  - `_scripts.recon_ping`
- Added `_vendored/drl/VERSION` and `_vendored/drl/README.md`.
- Updated `app/extractor_bridge.py` so API startup prepends `_vendored/drl/drl/`, imports the required `_scripts` modules from that path, and raises `RuntimeError` with `DRL vendored corpus missing or broken; see _vendored/drl/README.md` if the vendored corpus is absent or resolves from the wrong place.
- Kept `projects/Resemblio/code/extractor/` untouched.
- Added unit tests for vendored module resolution and the VERSION file.
- Added a gated live integration test for `POST /v1/extractions`.

## Source Snapshot

`projects/Design Reference Library/` is not a git repository in this workspace, so the VERSION source marker is:

`SOURCE: projects/Design Reference Library/_scripts/ @ no-git-snapshot 2026-05-25`

All four vendored Python files were copied byte-for-byte from the upstream DRL `_scripts/` directory. SHA-256 comparison passed for each file.

## Validation

- `.\.venv\Scripts\python.exe -m pytest tests/test_vendored_drl_present.py tests/test_vendored_version_file.py`: passed, `2 passed`.
- `python -m py_compile app/*.py app/routes/*.py`: passed via the project venv with `PYTHONPYCACHEPREFIX` outside `app/`.
- `.\.venv\Scripts\python.exe -m pytest`: passed, `19 passed, 1 skipped`.
- `RESEMBLIO_INTEGRATION_TESTS=1 .\.venv\Scripts\python.exe -m pytest tests/test_post_extractions_roundtrip.py -q`: passed. The test used the real extractor, real outbound fetch, and real Anthropic call, with fake in-memory R2 storage under TestClient.

## Friction

- The global Python did not have the API dependencies installed. The project `.venv` did, so validation was run through `code/api/.venv`.
- No `_handoff` message was created, per the outer Tool Coordination instruction to return via stdout and let the caller handle audit state.

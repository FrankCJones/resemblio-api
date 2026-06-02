#!/usr/bin/env bash
# Entrypoint smoke: every CLI entrypoint runs --help in a clean subprocess.
#
# Catches module-load ordering bugs (a transitive import side effect that
# breaks `python -m app.cli.X` even though the file is present and parses
# fine) BEFORE the deploy step ships them. Cheap (~seconds) and additive
# to the existing pytest + pip-audit gates.
#
# Wired in `.github/workflows/deploy.yml` as a required step between
# "Sanity test" and "Configure SSH". Override the interpreter with
# `PYTHON=/path/to/python bash ci/entrypoints.sh` (CI sets it to the
# Sanity-test venv; locally defaults to `python` on PATH).
#
# Per-project-scaled by the ENTRYPOINTS array. New CLI entrypoint?
# Add the dotted module path here.
set -euo pipefail

PYTHON="${PYTHON:-python}"

ENTRYPOINTS=(
  "app.cli.library_indexer"
  "app.cli.sweep_idempotency"
)

FAILED=0
for entry in "${ENTRYPOINTS[@]}"; do
  echo "=== smoking ${entry} ==="
  log="/tmp/smoke_${entry//./_}.log"
  if ! "${PYTHON}" -m "${entry}" --help > "${log}" 2>&1; then
    echo "FAIL: ${entry}"
    tail -20 "${log}"
    FAILED=1
  fi
done

exit "${FAILED}"

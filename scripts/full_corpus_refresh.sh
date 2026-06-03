#!/usr/bin/env bash
# Full corpus button-fidelity refresh.
#
# Captures R3.1 computed-style snapshots for every DRL brand, then drops +
# bootstraps + drains every brand's library_pages so the Hybrid Path B
# button override applies corpus-wide. Closes the Apple-only-snapshot gap
# documented in OPS.md 8.11.
#
# Run on prod (resemblio-prod-01) per the canonical SSH form in OPS.md 2.
# Env vars CLOUDFLARE_R2_* must be present in the shell that invokes this
# (the bootstrap subprocess reads them from os.environ).
#
# Usage:
#   ./scripts/full_corpus_refresh.sh                       # default DRL root
#   DRL_ROOT=/opt/resemblio-api/drl ./scripts/full_corpus_refresh.sh
#
# Exit codes:
#   0 - capture + refresh both completed with zero failures
#   1 - capture had failures (refresh skipped)
#   2 - refresh had failures
set -euo pipefail

DRL_ROOT="${DRL_ROOT:-/opt/resemblio-api/drl}"
PYTHON="${PYTHON:-/opt/resemblio-api/venv/bin/python}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
API_ROOT="$(cd -- "${SCRIPT_DIR}/.." &> /dev/null && pwd)"

echo "[full_corpus_refresh] api_root=${API_ROOT} drl_root=${DRL_ROOT} python=${PYTHON}"

cd "${API_ROOT}"

echo "[full_corpus_refresh] step 1/3: capture computed-style snapshots"
if ! "${PYTHON}" -m scripts.capture_all_button_snapshots --apply --drl-root "${DRL_ROOT}"; then
  echo "[full_corpus_refresh] capture had failures; aborting refresh" >&2
  exit 1
fi

echo "[full_corpus_refresh] step 2/3: refresh every brand (drop + bootstrap + drain)"
if ! "${PYTHON}" -m scripts.refresh_brand_library --all --apply --drl-root "${DRL_ROOT}"; then
  echo "[full_corpus_refresh] refresh had failures" >&2
  exit 2
fi

echo "[full_corpus_refresh] step 3/3: restart resemblio-web to clear Next.js fetch cache"
if command -v systemctl > /dev/null 2>&1; then
  sudo systemctl restart resemblio-web || echo "[full_corpus_refresh] WARN: systemctl restart resemblio-web failed (non-fatal)"
else
  echo "[full_corpus_refresh] systemctl not available; skip web restart"
fi

echo "[full_corpus_refresh] DONE. Verify in a browser; purge Cloudflare cache from the dashboard if needed."

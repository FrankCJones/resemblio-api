#!/usr/bin/env bash
# _capture_observed.sh - capture live extraction payloads for the 5 R3 ground-truth fixtures.
#
# Purpose: hit the production Resemblio API once per fixture URL, poll until complete,
# write each raw response JSON to observed/<brand>.json. Idempotent: re-running
# overwrites the per-brand JSONs but never deletes other files.
#
# Dependencies: bash, curl, jq, python3 (PyYAML available).
# Run command (from workspace root):
#   bash "projects/Resemblio/code/api/tests/fixtures/ground_truth/observed/_capture_observed.sh"
#
# Auth: reads RESEMBLIO_TEST_API_KEY from _credentials/credentials.env, CRLF-stripped
# per the 2026-06-01 lock. Never echoes the key value.
#
# Schema: writes raw API JSON verbatim; downstream delta report adds schema_version.

set -euo pipefail

WORKSPACE_ROOT="/c/Users/fjone/Desktop/Shared with Claude"
CREDS_FILE="${WORKSPACE_ROOT}/_credentials/credentials.env"
FIXTURE_DIR="${WORKSPACE_ROOT}/projects/Resemblio/code/api/tests/fixtures/ground_truth"
OBSERVED_DIR="${FIXTURE_DIR}/observed"
API_BASE="https://api.resemblio.com/v1"

POLL_INTERVAL_SECONDS=3
POLL_MAX_ATTEMPTS=30

mkdir -p "${OBSERVED_DIR}"

if [[ ! -f "${CREDS_FILE}" ]]; then
  echo "FATAL: credentials file missing at ${CREDS_FILE}" >&2
  exit 1
fi
RESEMBLIO_TEST_API_KEY="$(grep -E '^RESEMBLIO_API_KEY_INTERNAL=' "${CREDS_FILE}" | head -n1 | cut -d= -f2- | tr -d '\r\n')"
if [[ -z "${RESEMBLIO_TEST_API_KEY}" ]]; then
  echo "FATAL: RESEMBLIO_API_KEY_INTERNAL not found in credentials.env" >&2
  exit 1
fi
export RESEMBLIO_TEST_API_KEY

mapfile -t FIXTURES < <(python - "${FIXTURE_DIR}" <<'PY'
import sys, pathlib, yaml
root = pathlib.Path(sys.argv[1])
for p in sorted(root.glob("*.yaml")):
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    slug = data.get("brand_slug")
    url = data.get("source_url")
    if slug and url:
        print(f"{slug}\t{url}")
PY
)

if [[ "${#FIXTURES[@]}" -eq 0 ]]; then
  echo "FATAL: no fixtures discovered under ${FIXTURE_DIR}" >&2
  exit 1
fi

echo "Discovered ${#FIXTURES[@]} fixtures."

for line in "${FIXTURES[@]}"; do
  slug="${line%%$'\t'*}"
  url="${line#*$'\t'}"
  out_path="${OBSERVED_DIR}/${slug}.json"

  echo ""
  echo "=== ${slug} ==="
  echo "URL: ${url}"

  create_response="$(curl -sS -X POST "${API_BASE}/extractions" \
    -H "Authorization: Bearer ${RESEMBLIO_TEST_API_KEY}" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg u "${url}" '{url: $u, visibility: "public"}')" )"

  extraction_id="$(printf '%s' "${create_response}" | jq -r '.id // .extraction_id // empty')"
  if [[ -z "${extraction_id}" ]]; then
    echo "WARN: no extraction id in create response for ${slug}; saving raw response and continuing."
    printf '%s' "${create_response}" > "${out_path}"
    continue
  fi
  echo "Extraction id: ${extraction_id}"

  attempt=0
  final_payload=""
  while (( attempt < POLL_MAX_ATTEMPTS )); do
    sleep "${POLL_INTERVAL_SECONDS}"
    attempt=$((attempt + 1))
    poll_response="$(curl -sS -H "Authorization: Bearer ${RESEMBLIO_TEST_API_KEY}" \
      "${API_BASE}/extractions/${extraction_id}")"
    status="$(printf '%s' "${poll_response}" | jq -r '.status // empty')"
    echo "  poll ${attempt}: status=${status:-<none>}"
    if [[ "${status}" == "completed" || "${status}" == "failed" ]]; then
      final_payload="${poll_response}"
      break
    fi
  done

  if [[ -z "${final_payload}" ]]; then
    echo "WARN: ${slug} did not reach terminal status within $((POLL_MAX_ATTEMPTS * POLL_INTERVAL_SECONDS))s; saving last poll."
    final_payload="${poll_response}"
  fi

  printf '%s' "${final_payload}" | jq '.' > "${out_path}"
  echo "Wrote ${out_path}"
done

echo ""
echo "All fixtures captured. Next: run _compute_delta.py to author _delta_report.md."

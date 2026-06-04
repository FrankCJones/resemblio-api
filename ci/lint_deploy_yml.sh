#!/usr/bin/env bash
# Stage 3 lint: assert .github/workflows/deploy.yml carries the
# silent-partial-deploy gate.
#
# On 2026-06-02 the API deploy ran twice with the systemd unit's MainPID
# unchanged - CI green, code on disk advanced, alembic ran, service kept
# serving the prior process image. Parent-session SSH had to finish the
# restart manually both times. Stage 3 of
# `cto-reviews/2026-06-03-resemblio-back-on-track-tdd-plan.md` closes the
# bug by gating the deploy on a PID-change assertion plus a verb-probe.
#
# This script is the CI lint that prevents a future careless edit from
# silently removing the gate. It is paired with the pytest-time check
# at `tests/test_deploy_workflow_logic.py` - same four markers, two
# enforcement points.
#
# Required markers (1:1 with REQUIRED_MARKERS in the pytest file):
#   1. BEFORE_PID=        - pre-restart PID capture
#   2. AFTER_PID=         - post-restart PID capture
#   3. "PID did not change" - equality-failure diagnostic + non-zero exit
#   4. /v1/readyz         - verb-probe smoke (exercises DB + storage)
#
# Stage 12 marker (CTO Stage 12 - entrypoint smoke gate):
#   5. bash ci/entrypoints.sh - asserts every declared CLI module imports
#      cleanly in a subprocess BEFORE the deploy step runs. Paired with
#      `tests/test_entrypoint_smoke.py` (pytest-time parity assertion
#      between `app.cli/` and the ENTRYPOINTS shell array). Closes the
#      module-load race class flagged by the Library v1.1 3-hour outage.
#
# Git-SHA parity marker (CTO 2026-06-04 - silent-partial-deploy gate):
#   6. "Git-SHA parity" - asserts the in-heredoc check that prod git HEAD
#      matches the workflow trigger SHA after `git reset --hard origin/main`.
#      Closes Incident 2 (2026-06-02 `b3f7ca2`): SSH step reported success
#      with prod git HEAD never advancing. Design doc:
#      `cto-reviews/2026-06-04-cicd-partial-deploy-investigation.md`.
#   7. "SendEnv=GITHUB_SHA" - asserts GITHUB_SHA is forwarded to the remote
#      shell so the parity check above has the value to compare against.
#
# Run from the repo root: bash ci/lint_deploy_yml.sh
set -euo pipefail

DEPLOY_YML=".github/workflows/deploy.yml"

if [ ! -f "${DEPLOY_YML}" ]; then
  echo "FATAL: ${DEPLOY_YML} not found (cwd: $(pwd))" >&2
  exit 1
fi

REQUIRED=(
  "BEFORE_PID="
  "AFTER_PID="
  "PID did not change"
  "/v1/readyz"
  "bash ci/entrypoints.sh"
  "Git-SHA parity"
  "SendEnv=GITHUB_SHA"
)

MISSING=0
for marker in "${REQUIRED[@]}"; do
  if ! grep -qF -- "${marker}" "${DEPLOY_YML}"; then
    echo "MISSING: ${marker}" >&2
    MISSING=1
  else
    echo "OK: ${marker}"
  fi
done

if [ "${MISSING}" -ne 0 ]; then
  echo "" >&2
  echo "FATAL: deploy.yml is missing one or more Stage 3 markers." >&2
  echo "See cto-reviews/2026-06-03-resemblio-back-on-track-tdd-plan.md Stage 3." >&2
  echo "These markers form the silent-partial-deploy gate; do not delete them." >&2
  exit 1
fi

echo "deploy.yml Stage 3 lint: PASS"

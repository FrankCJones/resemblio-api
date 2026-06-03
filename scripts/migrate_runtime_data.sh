#!/bin/bash
# migrate_runtime_data.sh - one-time migration of runtime-written computed-style
# snapshots out of the git-tracked seed tree into the runtime-data root.
#
# Why this script exists
# ----------------------
# Pre-2026-06-03 the API wrote per-brand computed-style snapshots into
# `/opt/resemblio-api/app/_vendored/drl/drl/_data/computed_styles/*.json`.
# That path is git-tracked, so on the next CI deploy the `git reset --hard
# origin/main` step (running as the deploy user) could not unlink the
# service-user-owned files in that directory. Every deploy failed there.
#
# The structural fix (commit landing alongside this script) routes runtime
# writes through `RESEMBLIO_RUNTIME_DATA_ROOT` (default `/var/lib/resemblio/`).
# Code now reads from runtime root first, seed root second. Writes only go
# to runtime root. The seed dir keeps the `.gitkeep` plus any committed
# reference snapshots; nothing the service writes ever lands there again.
#
# This script does the one-time fix-up on an existing prod box:
#
# 1. Ensure `${RESEMBLIO_RUNTIME_DATA_ROOT}/computed_styles/` exists, owned
#    by the service user (default `${SERVICE_USER}=claude-cowork`).
# 2. Move every `*.json` file from the seed dir whose owner is the service
#    user (or any user not equal to the deploy user) into the runtime dir.
#    The seed dir's `.gitkeep` and any deploy-user-owned files (committed
#    reference snapshots) are left in place.
# 3. Reset ownership of the seed dir back to the deploy user so future
#    `git reset --hard` runs don't trip on stale ownership.
#
# Idempotent by design: re-running the script after the move is a no-op.
# Safe to re-run during an incident; safe to dry-run via `--dry-run`.
#
# Usage
# -----
#   sudo /opt/resemblio-api/app/scripts/migrate_runtime_data.sh           # apply
#   sudo /opt/resemblio-api/app/scripts/migrate_runtime_data.sh --dry-run # report only
#
# Env overrides (rarely needed; defaults match the canonical prod layout):
#
#   APP_ROOT                       default /opt/resemblio-api/app
#   RESEMBLIO_RUNTIME_DATA_ROOT    default /var/lib/resemblio
#   SERVICE_USER                   default claude-cowork (must match the
#                                  systemd unit's User= directive)
#   DEPLOY_USER                    default claude-cowork (the user CI runs
#                                  `git reset --hard` as; ownership target
#                                  for the seed tree)
#
# Note on SERVICE_USER vs DEPLOY_USER: on the resemblio-prod-01 box both
# are `claude-cowork` per `infra/box-resemblio-prod-01.yaml`. The split is
# parameterized so the same script handles a future redeploy that swaps
# the service user (e.g. dedicated `resemblio` user).
set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/resemblio-api/app}"
RUNTIME_ROOT="${RESEMBLIO_RUNTIME_DATA_ROOT:-/var/lib/resemblio}"
SERVICE_USER="${SERVICE_USER:-claude-cowork}"
DEPLOY_USER="${DEPLOY_USER:-claude-cowork}"
SUBDIR="computed_styles"

SEED_DIR="${APP_ROOT}/_vendored/drl/drl/_data/${SUBDIR}"
RUNTIME_DIR="${RUNTIME_ROOT}/${SUBDIR}"

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=1
fi

log() {
    # Single stderr log surface so the script is safe to invoke from cron
    # or systemd without polluting stdout (which a caller might capture).
    printf '%s migrate_runtime_data: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

run() {
    # Wrap any state-changing command so --dry-run reports without acting.
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        log "DRY-RUN would run: $*"
    else
        "$@"
    fi
}

main() {
    log "starting (dry_run=${DRY_RUN}, app_root=${APP_ROOT}, runtime_root=${RUNTIME_ROOT}, service_user=${SERVICE_USER}, deploy_user=${DEPLOY_USER})"

    # Verify expected users exist before doing anything destructive.
    if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
        log "ERROR service user '${SERVICE_USER}' does not exist; refusing to chown to a missing account"
        exit 2
    fi
    if ! id -u "${DEPLOY_USER}" >/dev/null 2>&1; then
        log "ERROR deploy user '${DEPLOY_USER}' does not exist"
        exit 2
    fi

    # Step 1: ensure runtime dir exists and is service-user-owned.
    if [[ ! -d "${RUNTIME_DIR}" ]]; then
        log "creating runtime dir ${RUNTIME_DIR}"
        run mkdir -p "${RUNTIME_DIR}"
    fi
    run chown -R "${SERVICE_USER}:${SERVICE_USER}" "${RUNTIME_ROOT}"
    run chmod 0755 "${RUNTIME_ROOT}"
    run chmod 0755 "${RUNTIME_DIR}"

    # Step 2: move runtime-owned snapshot files out of the seed dir.
    if [[ ! -d "${SEED_DIR}" ]]; then
        log "seed dir ${SEED_DIR} not present (fresh deploy?); nothing to migrate"
    else
        moved=0
        skipped=0
        # Only iterate *.json. The .gitkeep stays put unconditionally.
        # Glob may not match; nullglob avoids the literal-pattern footgun.
        shopt -s nullglob
        for src in "${SEED_DIR}"/*.json; do
            base="$(basename "${src}")"
            owner="$(stat -c '%U' "${src}")"
            if [[ "${owner}" == "${DEPLOY_USER}" ]]; then
                # Committed reference snapshot; leave in place. Anything in
                # git is by-definition deploy-user-owned after a successful
                # reset, so this branch catches the legitimate seed corpus.
                log "skip ${base} (owner=${owner} == DEPLOY_USER)"
                skipped=$((skipped + 1))
                continue
            fi
            dest="${RUNTIME_DIR}/${base}"
            if [[ -e "${dest}" ]]; then
                # Runtime root already has a newer copy. The seed copy is
                # stale; remove it so future deploys are clean. Idempotent
                # re-runs reach this branch on every file.
                log "remove stale seed ${base} (runtime copy present)"
                run rm -f "${src}"
            else
                log "move ${base} owner=${owner} -> ${dest}"
                run mv "${src}" "${dest}"
                run chown "${SERVICE_USER}:${SERVICE_USER}" "${dest}"
            fi
            moved=$((moved + 1))
        done
        shopt -u nullglob
        log "seed migration complete: moved_or_cleared=${moved} skipped=${skipped}"
    fi

    # Step 3: restore deploy-user ownership on the entire seed _data tree
    # so `git reset --hard` can unlink anything it needs to. Scope: just
    # `_vendored/drl/drl/_data/`; the rest of the checkout is already
    # deploy-user-owned under normal operation.
    SEED_DATA_ROOT="${APP_ROOT}/_vendored/drl/drl/_data"
    if [[ -d "${SEED_DATA_ROOT}" ]]; then
        log "chown ${SEED_DATA_ROOT} -> ${DEPLOY_USER}"
        run chown -R "${DEPLOY_USER}:${DEPLOY_USER}" "${SEED_DATA_ROOT}"
    fi

    log "done"
}

main "$@"

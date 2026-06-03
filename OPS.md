<!--
schema_version: ops_v1
purpose: Single first read for any prod-touching dispatch on resemblio-api.
         Closes the "operational drift" failure class (Class E in CTO TDD
         Recovery Plan 2026-06-02). Wrong SSH host, wrong layout path, wrong
         module name, wrong CLI shape, wrong PAT - every one of these has
         cost wall-clock in the last week. Each lives below with the
         wrong-vs-right incantation.
last_verified: 2026-06-02
maintainer: LLM-operated; humans read first, edit if reality drifts.
-->

# Resemblio API - OPS

The single first read before any prod-touching dispatch on `resemblio-api`.
Read this top-to-bottom; do not pattern-match from another host.

Source-of-truth cross-references:

- `infra/box-resemblio-prod-01.yaml` (workspace root) - canonical box facts
- `projects/Resemblio/Resemblio_INFRA.md` - infra runbook
- `projects/Resemblio/AUTHORITY.yml` - GREEN/YELLOW/RED per action
- `_credentials/CREDENTIALS_README.md` - what each env key is for

If this file disagrees with the YAML, the YAML wins. Fix this file and note
the date in `last_verified` above.

---

## 1. Canonical paths

All on `resemblio-prod-01` (`5.161.249.32`).

**Code-vs-data split (locked 2026-06-03).** Code lives under
`/opt/resemblio-api/app/` and is git-managed + deploy-user-owned
(`claude-cowork`). Runtime data lives under `/var/lib/resemblio/` and is
service-user-owned. NOT git-tracked. The split exists because pre-2026-06-03
the API wrote per-brand computed-style snapshots into the tracked vendored
tree; every CI `git reset --hard origin/main` then failed because the deploy
user could not unlink the service-owned files. The runtime-data resolver
lives at `app/runtime_data.py`; every service-side write routes through it.

| Purpose | Path |
|---|---|
| App root (git checkout) | `/opt/resemblio-api/app` |
| Release symlink | none (API does not use symlink-swap; web does) |
| Production `.env` | `/opt/resemblio-api/.env` |
| Python venv | `/opt/resemblio-api/venv` |
| DRL vendored corpus | `/opt/resemblio-api/drl` |
| Runtime-data root | `/var/lib/resemblio/` (env: `RESEMBLIO_RUNTIME_DATA_ROOT`) |
| Computed-style snapshots (runtime) | `/var/lib/resemblio/computed_styles/<slug>.json` |
| Computed-style snapshots (seed, in-tree) | `/opt/resemblio-api/app/_vendored/drl/drl/_data/computed_styles/<slug>.json` |
| systemd unit (API) | `/etc/systemd/system/resemblio-api.service` |
| systemd unit (indexer) | `/etc/systemd/system/resemblio-library-indexer.service` |
| systemd timer (indexer) | `/etc/systemd/system/resemblio-library-indexer.timer` |
| systemd unit (sweep) | `/etc/systemd/system/resemblio-idempotency-sweep.service` |
| journald log surface | `journalctl -u resemblio-api -u resemblio-library-indexer` |
| Postgres data | `/var/lib/postgresql/16/` |
| Reconcile audit log | `/var/log/resemblio/customer_reconcile.log` |

`/opt/resemblio-api/current` does NOT exist. Do not `cd` into it. Do not write
to it. The web repo uses symlink-swap; the API does not (yet).

### Runtime-data convention (locked 2026-06-03)

Rule: **code lives in `/opt/resemblio-api/app/` (git-managed,
claude-cowork-owned); runtime data lives in `/var/lib/resemblio/`
(service-user-owned, NOT git-tracked).**

- The systemd units set `RESEMBLIO_RUNTIME_DATA_ROOT=/var/lib/resemblio`
  and use `StateDirectory=resemblio` so the dir exists with the right
  owner on every unit start.
- Code reads via `app.runtime_data.resolve_read_path(subdir, name)`,
  which tries the runtime root first and falls back to the in-tree seed
  path. A committed reference snapshot in
  `_vendored/drl/drl/_data/computed_styles/` continues to work
  unmodified; a runtime-captured snapshot supersedes it.
- Code writes via `app.runtime_data.resolve_write_path(subdir, name)`,
  which only returns runtime-root paths. Writing into the seed tree from
  the running service is a regression and breaks CI.
- New runtime-data categories MUST mirror the runtime/seed subdir name
  (e.g. `computed_styles/`) and read via the resolver.
- The one-time migration of pre-fix snapshots out of the seed tree is
  `scripts/migrate_runtime_data.sh` (idempotent; safe to re-run).

---

## 2. Canonical SSH

ONE form. Use this exact invocation. No alternates.

```bash
KEY="/c/Users/fjone/Desktop/Shared with Claude/_credentials/resemblio_ed25519"
KH="/c/Users/fjone/Desktop/Shared with Claude/_credentials/resemblio-prod-01.known_hosts"

ssh -i "$KEY" \
    -o "UserKnownHostsFile=\"$KH\"" \
    -o "StrictHostKeyChecking=yes" \
    claude-cowork@5.161.249.32 \
    'whoami; hostname; uptime'
```

Notes that are non-obvious and have bitten previously:

- **Use the IP `5.161.249.32`, not `api.resemblio.com`.** The hostname is not
  in `known_hosts`; the IP is. Connecting by hostname returns
  `No ED25519 host key is known` and fails strict-host-key-checking.
- **Inner-quote the path-with-spaces.** `-o "UserKnownHostsFile=\"$KH\""`
  (escaped inner quotes) - bare `-o "UserKnownHostsFile=$KH"` lets OpenSSH
  split on the first space and report the known-hosts file as missing.
  Confirmed 2026-06-02 during Wave 3 web deploy.
- **User is `claude-cowork`.** `root@` is the fallback per the YAML but is
  locked for normal ops; only invoke through Hetzner rescue mode in recovery.
- **Never copy the key to `/tmp/rkey`.** The absolute path above is the
  workspace-standard. Alternate paths force a fresh harness allow-prompt
  every time.

For sudo operations, `claude-cowork` has scoped NOPASSWD entries in
`/etc/sudoers.d/`. If a command needs root and that user cannot sudo it,
treat it as a recovery operation and escalate per AUTHORITY.yml.

---

## 3. Env loading

`.env` on prod holds RESEMBLIO_DB_URL, R2 keys, Stripe keys, Resend key,
RESEMBLIO_INTERNAL_AUTH_SECRET, RESEMBLIO_KEY_PEPPER, and other secrets.
It is loaded by the systemd unit (EnvironmentFile=/opt/resemblio-api/.env)
automatically. Manual one-off commands must load it explicitly.

The canonical pattern (mirrors the CI deploy step):

```bash
set -a
. /opt/resemblio-api/.env
set +a

# Now invoke the python entry under the right user. R2 + Stripe creds
# are read from the calling environment by app/config.py, so they MUST
# survive the sudo hop.
sudo --preserve-env -u claude-cowork \
    /opt/resemblio-api/venv/bin/python -m app.cli.library_indexer
```

### The R2-credentials-across-sudo trap

`sudo` resets the environment by default. Without `--preserve-env` (or
`-E`), `CLOUDFLARE_R2_ACCESS_KEY` / `CLOUDFLARE_R2_SECRET_KEY` /
`CLOUDFLARE_R2_ENDPOINT` / `RESEMBLIO_R2_BUCKET` are stripped before the
child process starts. The script then fails late with an
`InvalidAccessKeyId` or `Could not connect` from the R2 client.

Wrong: `sudo -u claude-cowork /opt/resemblio-api/venv/bin/python ...`
Right: `sudo --preserve-env -u claude-cowork /opt/resemblio-api/venv/bin/python ...`

If the script's job is purely DB-only (e.g. `alembic upgrade`), you can omit
`--preserve-env` provided RESEMBLIO_DB_URL is in the environment of the
inner shell. The systemd unit handles this correctly via `EnvironmentFile=`;
the manual-ssh path is what needs the discipline.

---

## 4. CLI shapes

Exact invocations. Copy-paste runnable after SSH-ing in and loading `.env`
per section 3. Run from `/opt/resemblio-api/app` unless noted.

### 4.1 Alembic migrate

```bash
cd /opt/resemblio-api/app
/opt/resemblio-api/venv/bin/alembic -c alembic.ini current   # show prod head
/opt/resemblio-api/venv/bin/alembic -c alembic.ini upgrade head
/opt/resemblio-api/venv/bin/alembic -c alembic.ini current   # verify
```

The DB URL env var name is `RESEMBLIO_DB_URL` (NOT `DATABASE_URL`). Alembic
reads it via `alembic.ini` + `app/config.py`. If you see
`sqlalchemy.exc.ArgumentError: Could not parse SQLAlchemy URL`, you forgot
section 3 (`. /opt/resemblio-api/.env` with `set -a`).

### 4.2 DRL bootstrap (seed brand corpus)

The orchestrator is a SCRIPT, not a CLI subcommand. There is no
`library_indexer bootstrap` subcommand; do not invent one.

```bash
cd /opt/resemblio-api/app

# Dry-run first (default; lists what would be touched, writes nothing).
/opt/resemblio-api/venv/bin/python -m scripts.bootstrap_drl_library \
    --drl-root /opt/resemblio-api/drl

# Apply for one brand.
/opt/resemblio-api/venv/bin/python -m scripts.bootstrap_drl_library \
    --apply --drl-root /opt/resemblio-api/drl --single aeon

# Apply for every discovered brand.
/opt/resemblio-api/venv/bin/python -m scripts.bootstrap_drl_library \
    --apply --drl-root /opt/resemblio-api/drl

# Verify DB state without seeding.
/opt/resemblio-api/venv/bin/python -m scripts.bootstrap_drl_library \
    --verify-only --drl-root /opt/resemblio-api/drl
```

`--drl-root` is REQUIRED on prod. The script default points at the
workspace dev path and does not exist on the box.

### 4.3 Indexer drain (turn seeded asset_versions into library_pages)

```bash
cd /opt/resemblio-api/app
/opt/resemblio-api/venv/bin/python -m app.cli.library_indexer
```

The CLI takes NO arguments. `--help` is the only accepted flag. It drains
up to `LIBRARY_INDEX_BATCH_SIZE` (10) jobs per tick and exits. To clear a
backlog larger than 10, LOOP until the tick reports `jobs_run=0`:

```bash
while true; do
  OUT=$(/opt/resemblio-api/venv/bin/python -m app.cli.library_indexer 2>&1)
  echo "$OUT" | tail -n1
  echo "$OUT" | grep -q 'jobs_run=0' && break
done
```

The cap (10) is a deliberate knob, not a bug. The 60s timer drains it
naturally in steady state; the loop is for catch-up after a bootstrap.

### 4.4 Verify bootstrap (report DB state for DRL rows)

```bash
cd /opt/resemblio-api/app
/opt/resemblio-api/venv/bin/python -m scripts.verify_drl_bootstrap
```

### 4.5 Restart services after a config or unit edit

```bash
sudo systemctl daemon-reload
sudo systemctl restart resemblio-api
sudo systemctl restart resemblio-library-indexer.timer
sudo systemctl status resemblio-api --no-pager
```

---

## 5. Module names

These are the imports the bootstrap, seed, and drain entry points expect.
Wrong-import errors here have cost real time; the seam is brittle because
nothing asserts these at deploy gate.

| Symbol | Correct | Wrong (do not use) |
|---|---|---|
| DB session | `from app.db import SessionLocal` | `from app.database import ...` |
| Library indexer | `from app.library_indexer import drain_pending` | `from app.indexer ...` |
| Config | `from app.config import load_project_env` | `from app.settings ...` |
| Constants | `from app.constants import ...` | inline magic numbers |
| Seed | `from scripts.seed_from_drl import apply_seed, iter_assets, ...` | `from app.scripts ...` |
| Bootstrap | run as `python -m scripts.bootstrap_drl_library` | `python scripts/bootstrap...` (sys.path mutation lives inside the module) |

`scripts/` is on `sys.path` because each script self-inserts its parent into
`sys.path[0]` at import time. Run as `python -m scripts.X` from
`/opt/resemblio-api/app`. Do not run as `./scripts/X.py`.

---

## 6. Credentials map

Source-of-truth: `_credentials/credentials.env` (workspace root) and
`_credentials/CREDENTIALS_README.md`. Prod `.env` on the box is the live
copy; `credentials.env` is the recovery source.

| Env key | Used for | Notes |
|---|---|---|
| `GITHUB_TOKEN_RESEMBLIO_API` | `git push` to `FrankCJones/resemblio-api` (this repo) | Fine-scoped PAT. Use this, NOT the default token. |
| `GITHUB_TOKEN_RESEMBLIO` | `git push` to `FrankCJones/resemblio-web` | Different repo, different PAT. Mixing them 403s. |
| `GITHUB_TOKEN_RESEMBLIO_MCP` | `git push` to `FrankCJones/resemblio-mcp` | Separate PAT, separate repo. |
| `RESEMBLIO_DB_URL` | Alembic + app DB connection | `postgresql://resemblio:<pw>@127.0.0.1:5432/resemblio` |
| `CLOUDFLARE_R2_ENDPOINT` | R2 client init | Workspace-shared R2 account |
| `CLOUDFLARE_R2_ACCESS_KEY` | R2 auth | Lost across sudo without `--preserve-env` |
| `CLOUDFLARE_R2_SECRET_KEY` | R2 auth | Lost across sudo without `--preserve-env` |
| `RESEMBLIO_R2_BUCKET` | Per-project bucket name | Default `resemblio-extractions` |
| `STRIPE_RESTRICTED_KEY_RESEMBLIO_LIVE` | Stripe LIVE mode (post-cutover) | Live cutover SHIPPED 2026-06-02 |
| `STRIPE_RESTRICTED_KEY_RESEMBLIO_TEST` | Stripe TEST mode | For TEST-mode dev only |
| `STRIPE_WEBHOOK_SECRET_RESEMBLIO_LIVE` | Stripe webhook signature verify (LIVE) | Per-mode secret |
| `STRIPE_WEBHOOK_SECRET_RESEMBLIO_TEST` | Stripe webhook signature verify (TEST) | Per-mode secret |
| `RESEND_API_KEY` | Transactional email | CRLF in this value broke email-403 once; verify clean newline |
| `RESEMBLIO_INTERNAL_AUTH_SECRET` | BFF magic-link auth (web -> API) | Unset = `/v1/internal/auth/*` returns 503 (fail-closed) |
| `RESEMBLIO_TEST_AUTH_ENABLED` | O9 Playwright E2E test-auth gate | MUST remain UNSET on prod. Value `"1"` opens the test-only readback + teardown endpoints (see Section 8d). Any other value (including `"true"`, `"yes"`) is treated as off. |
| `RESEMBLIO_TEST_AUTH_TOKEN` | O9 Playwright E2E test-auth header | MUST remain UNSET on prod. Companion to `_ENABLED`; the route handlers require this value on the `X-Test-Auth` request header. |
| `RESEMBLIO_KEY_PEPPER` | API-key storage hash | 32+ chars, never rotate without `_OLD` overlap |
| `HETZNER_API_TOKEN` | Hetzner Cloud API | Box-level recovery only |
| `CLOUDFLARE_API_TOKEN` | DNS via CF API | Zone scope `resemblio.com` |

When pushing to the resemblio-api repo from local:

```bash
# In the repo working tree:
git push https://x-access-token:${GITHUB_TOKEN_RESEMBLIO_API}@github.com/FrankCJones/resemblio-api.git main
```

The default workspace `GITHUB_TOKEN` does NOT have access to the private
resemblio-api repo. Using it returns 403 and burns 30 seconds re-discovering
which PAT is correct.

---

## 7. Deploy verification queries

These curls run against the live prod surface and prove the API + indexer
are healthy after a deploy or a bootstrap. CI runs the first two; the rest
are operator-side verification.

```bash
# 1. API healthz returns 200.
curl -s -o /dev/null -w "%{http_code}\n" https://api.resemblio.com/v1/healthz

# 2. Hub returns >0 brands.
curl -s https://api.resemblio.com/v1/library/hub | python -c \
  "import sys, json; d=json.load(sys.stdin); print('hub_total:', d.get('total'))"

# 3. One canonical brand renders (Aeon).
curl -s -o /dev/null -w "%{http_code}\n" \
  https://api.resemblio.com/v1/library/aeon/categories

# 4. A category page body contains the CSS variable that proves token
#    composition fired. Body fragment, no <html> wrapper.
curl -s "https://api.resemblio.com/v1/library/aeon/<known-category-slug>/page" \
  | grep -c -- '--ds-bg:'
# Expect exactly 1.

# 5. Alembic parity (over SSH after deploy):
ssh ...  '/opt/resemblio-api/venv/bin/alembic -c /opt/resemblio-api/app/alembic.ini current'
# Compare to repo head: alembic -c alembic.ini heads
```

These are the same shape as the deploy-time gate in
`.github/workflows/deploy.yml`. If any returns non-200 or zero matches,
do NOT mark the dispatch done; investigate before reporting.

---

## 8. Common failures and their fixes

The bug-15 family in tabular form. Wrong shape, right shape, root cause.

### 8.1 `api.resemblio.com` not in known_hosts

```
Wrong: ssh ... claude-cowork@api.resemblio.com
Right: ssh ... claude-cowork@5.161.249.32
```

Root cause: the prod box's known_hosts entry is keyed by IP, not hostname.
Strict-host-key-checking refuses the hostname connection.

### 8.2 `/opt/resemblio-api/current` does not exist

```
Wrong: cd /opt/resemblio-api/current && git pull
Right: cd /opt/resemblio-api/app && git pull
```

Root cause: only the web repo uses symlink-swap (`current` -> `releases/<sha>`).
The API ships in-place at `/opt/resemblio-api/app`. Pattern-matching from
the web layout is the prohibited shortcut.

### 8.3 `ImportError: No module named app.database`

```
Wrong: from app.database import SessionLocal
Right: from app.db   import SessionLocal
```

Root cause: the package is `app.db`, not `app.database`. This bug surfaces
in ad-hoc scripts that get written from memory.

### 8.4 `library_indexer bootstrap --apply` unrecognized

```
Wrong: python -m app.cli.library_indexer bootstrap --apply
Right: python -m scripts.bootstrap_drl_library --apply --drl-root /opt/resemblio-api/drl
```

Root cause: the indexer CLI takes no arguments. Bootstrap is a separate
SCRIPT, not a subcommand. Conflating them is a 5-minute round-trip every
time it happens.

### 8.5 `git push` 403 from default PAT

```
Wrong: git push origin main           (uses default GITHUB_TOKEN)
Right: git push https://x-access-token:${GITHUB_TOKEN_RESEMBLIO_API}@github.com/FrankCJones/resemblio-api.git main
```

Root cause: the default workspace token has no access to private resemblio
repos. Per-repo PATs in `_credentials/credentials.env`:
`GITHUB_TOKEN_RESEMBLIO_API` for api, `GITHUB_TOKEN_RESEMBLIO` for web,
`GITHUB_TOKEN_RESEMBLIO_MCP` for mcp.

### 8.6 R2 credentials lost across sudo

```
Wrong: sudo -u claude-cowork /opt/resemblio-api/venv/bin/python -m scripts.bootstrap_drl_library --apply
Right: sudo --preserve-env -u claude-cowork /opt/resemblio-api/venv/bin/python -m scripts.bootstrap_drl_library --apply --drl-root /opt/resemblio-api/drl
```

Root cause: `sudo` strips the environment by default. The R2 client
initialized inside the bootstrap reads `CLOUDFLARE_R2_*` from os.environ;
without `--preserve-env` they are absent and the upload fails late.

### 8.7 Drain caps at 10 jobs/tick, backlog never clears

```
Wrong: python -m app.cli.library_indexer       # one shot; 10 jobs/tick
Right: while true; do
         OUT=$(/opt/resemblio-api/venv/bin/python -m app.cli.library_indexer 2>&1)
         echo "$OUT" | tail -n1
         echo "$OUT" | grep -q 'jobs_run=0' && break
       done
```

Root cause: `LIBRARY_INDEX_BATCH_SIZE=10` is intentional (steady-state burst
size). Bootstrap-time backlogs (hundreds of jobs) need a loop. The systemd
timer drains naturally at 60s cadence in steady state.

### 8.8 Relative manifest_dir breaks redirect-map load

```
Wrong: python -m scripts.bootstrap_drl_library --drl-root drl
Right: python -m scripts.bootstrap_drl_library --drl-root /opt/resemblio-api/drl
```

Root cause: the bootstrap resolves `--drl-root` and joins it against
manifest paths. A relative root resolves against `cwd` at import time, not
at compose time; subsequent file opens against the joined path fail with
`FileNotFoundError`. Always pass an absolute path on prod.

### 8.12 CI deploy fails on `git reset --hard origin/main` with `Permission denied` on `_data/computed_styles/`

```
Symptom: deploy job logs
         `error: unable to unlink old '_vendored/drl/drl/_data/computed_styles/.gitkeep': Permission denied`
Probe:   ssh ... 'ls -la /opt/resemblio-api/app/_vendored/drl/drl/_data/computed_styles/'
         # Look for files owned by a user other than claude-cowork.
Fix:     sudo /opt/resemblio-api/app/scripts/migrate_runtime_data.sh
         # Re-run the deploy; runtime files now live under
         # /var/lib/resemblio/computed_styles/ and the seed dir is back to
         # claude-cowork ownership.
```

Root cause: pre-2026-06-03 the API wrote per-brand computed-style
snapshots into the git-tracked seed tree. The deploy user could not
unlink the service-owned files, so `git reset --hard` aborted before any
new code landed. The structural fix moved runtime writes to
`/var/lib/resemblio/`; this row is the recovery for any box that still
carries pre-fix snapshots in the seed dir. The migration script is
idempotent; a clean box returns immediately.

### 8.9 `.git/` ownership drift after a stray `sudo` git op

```
Symptom: git fetch fails with `unpack-objects failed`
Probe:   find /opt/resemblio-api/app/.git -not -user claude-cowork -print -quit
Fix:     sudo chown -R claude-cowork:claude-cowork /opt/resemblio-api/app/.git
```

Root cause: a prior session ran a git op as root (typically while debugging),
leaving pack objects owned by root that the deploy user cannot read. The
CI workflow's self-heal handles this if sudo NOPASSWD is configured.

### 8.11 Library buttons render as default 6px chiclets across all brands

```
Symptom: every brand's library page renders the DRL default `.b-btn`
         (6px radius, 10/16 padding, 14px / 500) instead of the brand's
         actual button shape. Apple alone renders correctly.
Root:    the Hybrid Path B button override (CTO 2026-06-02) needs a
         per-brand R3.1 computed-style snapshot at
         `_vendored/drl/drl/_data/computed_styles/<brand>.json`. Only
         Apple ships with one; the other 23 brands fall back to the
         DRL default because the loader returns None when no snapshot
         exists (fail-safe by design).
Fix:     python -m scripts.capture_all_button_snapshots --apply \
           --drl-root /opt/resemblio-api/drl
         python -m scripts.refresh_brand_library --all --apply \
           --drl-root /opt/resemblio-api/drl
         sudo systemctl restart resemblio-web
         # Then purge the Cloudflare cache from the dashboard.
One-shot: ./scripts/full_corpus_refresh.sh
Verify:  curl -s https://resemblio.com/library/apple/buttons/ | \
           grep -c 'data-resemblio-button-override'
         # Expect >= 1 per brand once override applied.
```

The capture script is per-brand idempotent (existing snapshots skip
unless `--force` is passed). Per-brand failures isolate: a brand that
times out or fails Playwright capture is logged and the next brand
still runs. The refresh script follows the same isolation contract.

### 8.10 Alembic upgrade silently skipped

```
Symptom:  deploy reports green, but `alembic current` on prod is behind head
Probe:    LOCAL=$(alembic heads | awk '{print $1}' | head -n1)
          PROD=$(ssh ... 'alembic -c /opt/resemblio-api/app/alembic.ini current | awk "{print \$1}" | head -n1')
          [ "$LOCAL" = "$PROD" ] && echo PARITY || echo GAP
Fix:      The CI workflow now asserts post-upgrade parity and fails the deploy.
          For manual catch-up: alembic upgrade head, then re-assert.
```

Root cause: multi-head or unresolved-dependency states let `alembic upgrade`
exit 0 without advancing. The CI parity-assert step is the gate; do not
remove it.

---

## 8b. Stage O1 anonymous-extraction surface

Three endpoints landed via migration 0021 (`anonymous_extractions`,
`anon_extract_counters`, `notify_requests`):

| Endpoint | Auth | Notes |
|---|---|---|
| `POST /v1/anonymous/extractions` | none | Rate-limited per IP (default 1/day); classifies URL; supported -> 202 + `claim_token` + `extraction_id`; unsupported -> 200 `status="out_of_scope"` + notify capture URL |
| `GET /v1/anonymous/extractions/{id}?claim_token=<...>` | claim_token | 403 on missing or mismatched token; surfaces classification + status + tokens preview when ready |
| `POST /v1/notify-when-supported` | none | Append-only email capture for unsupported classes |

Feature flag: `RESEMBLIO_ANON_EXTRACT_ENABLED=true` MUST be set in the
prod `.env` before the endpoint serves real traffic. Default off; flag
flip is YELLOW (see project `AUTHORITY.yml`).

Rate-limit storage: Postgres-backed `anon_extract_counters(ip_hash, day, count)`.
Redis is the future home; the table-backed counter is cross-process
correct without a new runtime dependency. The per-IP daily cap is
`ANON_EXTRACT_PER_IP_PER_DAY` (default 1). Raw client IPs are NEVER
persisted; only their SHA-256 hash lands in `ip_hash` + counter rows.

Observability paths:

- Structured log line on every 429: `anon_extract_rate_limited ip_hash=<sha> cap=<n> retry_after=<s>`
- Service user that owns anonymous extraction rows until Stage O5 conversion: `anonymous-service@resemblio.com` (created lazily on first request).
- Reaper script (run daily via systemd timer in `deploy/scripts/`):
  `python -m scripts.reap_anonymous_extractions` flips expired
  pending rows to `status='expired'` and hard-deletes rows >30 days
  past their `expires_at`.

Classifier dependency: `app/site_classifier.py:classify_url` is the
real Stage-O3 first-byte heuristic (shipped 2026-06-03). It fetches up
to 16 KB of the URL via httpx with retry+backoff, then matches body +
header + status-code signals defined in
`app/site_classifier_signals.yml` to return one of:
`html_first` / `js_rendered` / `wix_class` / `waf_blocked` / `unknown`.
Supported set per `ANON_SUPPORTED_CLASSES` is `{html_first, js_rendered}`;
out-of-scope classes short-circuit with the notify-when-supported capture.

---

## 8c. Stage O5 anonymous-to-account claim endpoint

`POST /v1/internal/auth/claim_anonymous_extraction` binds a Stage O1
anonymous extraction row to a freshly-minted user account immediately
after a successful magic-link redeem. Lives on the same internal-BFF
surface (shared-secret header `X-Internal-Auth`) as the rest of
`/v1/internal/auth/*`; bypass-listed in `AUTH_FREE_PATHS` so the
Bearer-token middleware does not 401 it.

Contract:

| Field | Shape | Notes |
|---|---|---|
| Request body | `{claim_token: str, user_id: int}` | `claim_token` is the opaque 32-byte URL-safe secret minted at Stage O1; `user_id` is the new account from the redeem response |
| Headers | `X-Internal-Auth: <RESEMBLIO_INTERNAL_AUTH_SECRET>` | Shared with the rest of `/internal/auth/*` |
| 200 success | `{schema_version: 1, ok: true, extraction_id: int}` | Extraction row's `user_id` is now the new account; `anonymous_extractions.claimed_at` is stamped; status walks to `claimed` |
| 400 `user_not_found` | - | The BFF should never hit this; it just minted the user |
| 401 `internal_auth_invalid` | - | Missing or wrong shared secret |
| 404 `invalid_claim_token` | - | Unknown token; covers both never-existed and silently-leaked states |
| 404 `nothing_to_claim` | - | Registry exists but `extraction_id` is NULL (out-of-scope class) |
| 409 `already_claimed` | - | `claimed_at` is already non-NULL; double-claim is a hard error |
| 410 `claim_expired` | - | Past the 24-hour `expires_at` window |
| 503 `internal_auth_unconfigured` | - | `RESEMBLIO_INTERNAL_AUTH_SECRET` unset |

Atomicity: the bind walks Extraction.user_id rebind + AnonymousExtraction.claimed_at + AnonymousExtraction.status in one commit. A peer claim from a different user is caught by the `claimed_at IS NOT NULL` guard before any write lands (Postgres row-lock semantics; SQLite serializes writes at the connection level).

Web side (BFF): the verify Route Handler at `app/api/auth/verify/route.ts` reads a short-lived `resemblio_claim` cookie (set by `app/api/auth/request/route.ts` when the signup body carries claim params), calls this endpoint, and redirects to `/app/extractions/<id>` on success or to `/app` on any bind failure. A failed bind is logged and dropped on the floor; the signup still completes.

---

## 8d. O9 Playwright E2E test-only surface

Two endpoints exist solely to unblock the O9 Playwright E2E suite.
They live in `app/routes/internal_test.py` and are DARK BY DEFAULT.

WARNING. Enabling this surface on a prod box is a critical safety
violation. The plaintext-token readback bypasses email-as-second-factor
for any account whose address the caller knows; the teardown surface
is unconditional destructive delete. Both env vars MUST remain unset
on every prod `.env`. This surface is intended for staging + local
dev only. Audit the prod `.env` before every cutover.

Both endpoints share the same two-gate check:

1. `RESEMBLIO_TEST_AUTH_ENABLED` must be exactly the string `"1"`. Any
   other value (including `"true"`, `"yes"`, `"on"`) is treated as off.
2. `RESEMBLIO_TEST_AUTH_TOKEN` must be set to a non-empty string, AND
   the request must carry `X-Test-Auth: <that-token>` (constant-time
   compare).

Failure modes:

| Condition | Status | Body |
|---|---|---|
| Either env var unset | 403 | `{"error": "test_auth_disabled"}` |
| Header missing or mismatched | 401 | `{"error": "test_auth_invalid"}` |

### Endpoint 1: GET /v1/internal/auth/test_get_latest_magic_link

Query string `?email=<>`. Returns the latest unconsumed plaintext
magic-link token for the supplied email so the Playwright harness can
synthesize a redeem click without scraping a real inbox.

| Status | Body | Cause |
|---|---|---|
| 200 | `{schema_version: 1, token: str, expires_at: str, email: str}` | Latest row matched; ordered by `created_at DESC` |
| 404 | `{"error": "no_unconsumed_token"}` | No row, or only rows with `consumed_at IS NOT NULL`, or only rows whose `plaintext_token` is NULL (minted before the flag was on) |

Plaintext is stored in `magic_link_tokens.plaintext_token` (migration
0022). The `request_magic_link` route writes this column ONLY when the
test-auth surface is enabled at mint time; rows minted with the flag
off are NULL and the readback returns 404 even if the flag is later
toggled. This means a prod deploy that flips only the readback flag
without minting fresh tokens cannot leak anything.

### Endpoint 2: POST /v1/internal/test/teardown_user

Body `{email}`. Deletes the user and every child row a Playwright run
could have produced. Idempotent: a second call against an
already-deleted email returns `{ok: true, deleted_rows: 0}`.

Fan-out (in order):

* `magic_link_tokens` by email (these are not FK'd to `users`)
* `api_key_events` for keys owned by the user
* `web_session_keys` owned by the user
* `anonymous_extractions` whose `extraction_id` points at an extraction
  the user owns
* `credit_ledger` rows scoped to the user's extractions
* `auto_refund_audit_events` for the user's extractions
* `extractions` owned by the user
* `credit_ledger` rows scoped to the user directly (e.g. onboarding
  grant)
* `idempotency_keys` owned by the user
* `topup_sessions` owned by the user
* `api_keys` owned by the user
* `users` row itself

Returns `{schema_version: 1, ok: true, deleted_rows: int}`.

### Auditing the surface is off on prod

```bash
ssh -i "$RKEY" -F "$RHOSTS" claude-cowork@$RHOST \
  'sudo grep -E "RESEMBLIO_TEST_AUTH" /opt/resemblio-api/.env || echo OK'
```

`OK` (no match) is the expected output. Any match is the failure
signal; clear the env vars, `sudo systemctl restart resemblio-api`,
and rotate the magic-link token plaintext column with `UPDATE
magic_link_tokens SET plaintext_token = NULL WHERE plaintext_token
IS NOT NULL;`.

---

## 8b. Tuning the URL classifier

The signal patterns live in `app/site_classifier_signals.yml` (sibling
of `app/site_classifier.py`). Edit-and-restart picks up new signals
without a code redeploy; the loader caches by file mtime so a SIGHUP
of the API service is enough.

### Schema lock

Header carries `schema_version: site_classifier_signals_v1`. The loader
raises `RuntimeError` on a mismatch rather than silently classifying
every URL as `unknown`. Adding new signal CLASSES requires updating
`CLASS_PRECEDENCE` in code; adding new PATTERNS to existing classes is
YAML-only.

### Block shape

```yaml
classes:
  <class_name>:
    min_signals: <int>        # minimum matches to claim the class
    confidence_base: <float>  # confidence when min_signals met
    confidence_step: <float>  # added per signal above the minimum
    patterns_body:            # strings (substring) or "re:<regex>"
      - "static.parastorage.com"
      - "re:<meta\\s+name=\"generator\"...>"
    patterns_headers:         # (header_name, value_substring_or_regex)
      - ["x-wix-request-id", ""]   # "" matches any value
    status_codes: [403, 503]  # statuses that count as one signal
```

### Precedence

`wix_class > waf_blocked > js_rendered > html_first`. Out-of-scope
classes resolve first; a Wix-built SPA classifies as `wix_class` so we
do not burn a Playwright slot on it.

### Tuning workflow

1. Capture a failing URL: probe with `classify_url(url)` from a Python
   REPL on the box (one-shot, GREEN).
2. Inspect `result.body_excerpt` and `result.response_headers` to see
   what signals were actually present.
3. Edit `site_classifier_signals.yml` to add the missing signal.
4. Restart the API service or wait for the next request after the
   file mtime updates (cache is mtime-keyed).
5. Re-run `pytest tests/test_site_classifier.py` to confirm no
   regression on the synthetic fixtures.

### Test seam

The classifier accepts dependency-injected `client` and `sleep` args
plus an override `signals_path`. Tests in `tests/test_site_classifier.py`
drive it with `httpx.MockTransport`; no live HTTP.

### Trusted auth context

For private extractions where the caller supplied HTTP basic auth,
pass `trusted_auth_context=True` to suppress the 401/407 -> waf_blocked
signal. The anonymous-extract route does NOT set this flag (anonymous
extractions are not authenticated).

---

## 8c. Stage O7 export-format endpoints

Two routes serve every export format off the persisted DTCG payload:

| Endpoint | Auth | Notes |
|---|---|---|
| `GET /v1/extractions/{id}/export/{fmt}` | Bearer | Ownership-scoped; 404 across user boundary |
| `GET /v1/anonymous/extractions/{id}/export/{fmt}?claim_token=<...>` | claim_token | 403 on missing or mismatched token |

`fmt` is one of `dtcg`, `css`, `tailwind`, `zip`. Anything else returns
400 with the supported-format list so a client can self-correct. Style
Dictionary and Figma Tokens are intentionally NOT in this list; they
sit on the v1.1 backlog per the URL-first respec.

Format conventions (locked 2026-06-03):

| Format | Content-Type | Filename |
|---|---|---|
| `dtcg` | `application/json` | `resemblio-<id>-tokens.json` |
| `css` | `text/css; charset=utf-8` | `resemblio-<id>-tokens.css` |
| `tailwind` | `text/css; charset=utf-8` | `resemblio-<id>-tailwind.css` |
| `zip` | `application/zip` | `resemblio-<id>-bundle.zip` |

Every response carries `Content-Disposition: attachment; filename="..."`
plus `X-Exporter-Schema-Version: 1`. The exporter wire-contract version
is fixed at `1`; bumping requires coordinated client rollout.

CSS output shape: `:root { --<group>-<leaf>: <value>; ... }`. Group
names are kebab-cased (`fontFamily` -> `font-family`). DTCG
`schema_version` sibling is filtered out.

Tailwind output shape: `@theme { ... }` block matching Tailwind v4
namespaces (`--color-*`, `--font-*`, `--spacing-*`, `--radius-*`,
`--text-*`, `--shadow-*`). DTCG groups with no Tailwind v4 namespace
(duration, cubicBezier, "other") are omitted from this output by
design; the DTCG + CSS exports carry them.

ZIP bundle layout:

```
resemblio-<id>-bundle.zip
+-- README.md
+-- tokens.json     (canonical DTCG, pretty-printed)
+-- tokens.css      (CSS :root custom properties)
+-- tailwind.css    (Tailwind v4 @theme block)
+-- screenshot.png  (optional; only when caller supplies bytes)
```

The bundle uses a fixed timestamp on every ZIP entry so two requests
for the same extraction return byte-identical archives (content-hash
stable across requests).

Pricing: FREE in v1. The extraction was already charged at creation
time; conversion is value-add per the pricing ladder. No ledger debit.

Subsystem reference: `app/exporters/README.md`. Pure-data converters
unit-tested in `tests/test_exporter_*.py`; route integration in
`tests/test_routes_exports.py`.

---

## 9. When this file goes stale

If reality on the box differs from anything above, the file is wrong.

1. Probe the live box (section 7 queries).
2. Update the affected section here.
3. Update `last_verified` in the header.
4. Commit with the deploy that touched the underlying surface.

Pattern-match from another host is the prohibited shortcut. Read this file
first; read the box YAML second; only then act.

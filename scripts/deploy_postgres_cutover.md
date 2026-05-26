# Resemblio API - SQLite to Postgres cutover runbook

**Audience:** operator running the cutover on `resemblio-prod-01` (5.161.249.32).
**Status:** DRAFT pending Frank's review. Do not execute without sign-off.
**Estimated downtime:** 5 to 15 minutes (dominated by alembic + dry-run review).
**Last updated:** 2026-05-25.

This runbook flips production from `/opt/resemblio-api/app/resemblio_dev.db`
(SQLite) to the documented Postgres 16 service running on the same VPS,
per `Resemblio_INFRA.md` lines 78-93 and the 2026-05-25 drift entry in
`projects/OptSus Team/brain/decisions-log.md`.

The companion script is `scripts/migrate_sqlite_to_postgres.py`. Tests
covering its pure-data helpers live at
`tests/test_migrate_sqlite_to_postgres.py`.

---

## Pre-flight (no downtime, no writes)

Run all commands as `claude-cowork` on `resemblio-prod-01`. Each step
prints a check artifact; capture stdout into the cutover log.

### P1. Confirm Postgres is actually running

```bash
systemctl is-active postgresql
# expect: active
sudo -u postgres psql -c "SELECT version();"
# expect: PostgreSQL 16.x ...
```

### P2. Confirm the `resemblio` database and role exist

```bash
sudo -u postgres psql -c "\du resemblio"
sudo -u postgres psql -c "\l resemblio"
```

If either is missing, create them (record the password in
`_credentials/credentials.env` as `RESEMBLIO_POSTGRES_PASSWORD` first;
never paste a generated password into shell history):

```bash
# Only if missing. Generate the password locally and store it in credentials.env
# before pasting it into a heredoc here.
sudo -u postgres psql <<'SQL'
CREATE ROLE resemblio LOGIN PASSWORD :'pw';
CREATE DATABASE resemblio OWNER resemblio;
SQL
```

### P3. Confirm SQLite source is where we think it is

```bash
ls -lh /opt/resemblio-api/app/resemblio_dev.db
sqlite3 /opt/resemblio-api/app/resemblio_dev.db ".tables"
sqlite3 /opt/resemblio-api/app/resemblio_dev.db \
  "SELECT 'users' AS t, COUNT(*) FROM users
   UNION ALL SELECT 'api_keys', COUNT(*) FROM api_keys
   UNION ALL SELECT 'extractions', COUNT(*) FROM extractions
   UNION ALL SELECT 'credit_ledger', COUNT(*) FROM credit_ledger
   UNION ALL SELECT 'topup_sessions', COUNT(*) FROM topup_sessions
   UNION ALL SELECT 'stripe_events_seen', COUNT(*) FROM stripe_events_seen
   UNION ALL SELECT 'api_key_events', COUNT(*) FROM api_key_events;"
```

Record the counts. They are the gate the migration script will verify against.

### P4. Back up the SQLite file (rollback insurance)

```bash
TS=$(date -u +%Y%m%dT%H%M%SZ)
sudo cp /opt/resemblio-api/app/resemblio_dev.db \
        /opt/resemblio-api/app/resemblio_dev.db.bak.$TS
sudo chown claude-cowork:claude-cowork \
        /opt/resemblio-api/app/resemblio_dev.db.bak.$TS
sha256sum /opt/resemblio-api/app/resemblio_dev.db.bak.$TS
```

Also upload a copy to R2 `resemblio-backups/cutover-sqlite-$TS.db` via the
existing rclone profile so a VPS-loss event during the cutover is still
recoverable.

### P5. Confirm current `.env` value

```bash
sudo grep RESEMBLIO_DB_URL /opt/resemblio-api/.env
```

Expected today: a SQLite URL. The target value is
`postgresql+psycopg2://resemblio:<password>@127.0.0.1:5432/resemblio`
(driver prefix matches the format the migration script and `app.db`
already expect).

---

## Cutover (downtime begins at Step 1)

### Step 1. Stop the API

```bash
sudo systemctl stop resemblio-api
systemctl is-active resemblio-api
# expect: inactive
```

Caddy continues serving the marketing site; only `api.resemblio.com`
returns 502 from this point.

### Step 2. Create the Postgres schema

```bash
cd /opt/resemblio-api/app
sudo -u claude-cowork \
  RESEMBLIO_DB_URL='postgresql+psycopg2://resemblio:<pw>@127.0.0.1:5432/resemblio' \
  /opt/resemblio-api/venv/bin/alembic upgrade head
```

Verify:

```bash
sudo -u postgres psql -d resemblio -c "\dt"
sudo -u postgres psql -d resemblio -c "SELECT version_num FROM alembic_version;"
```

Expect tables: `users, api_keys, api_key_events, extractions, credit_ledger,
topup_sessions, stripe_events_seen, alembic_version`. `version_num` should
match the head revision (currently `0004_topup_sessions_and_balance_check`).

### Step 3. Migration dry-run

```bash
cd /opt/resemblio-api/app
SQLITE_SOURCE_URL='sqlite:////opt/resemblio-api/app/resemblio_dev.db' \
POSTGRES_TARGET_URL='postgresql+psycopg2://resemblio:<pw>@127.0.0.1:5432/resemblio' \
/opt/resemblio-api/venv/bin/python scripts/migrate_sqlite_to_postgres.py --dry-run
```

Read the JSON report. Confirm each `source_rows` matches the counts you
captured in P3. Abort and investigate if any count differs.

### Step 4. Migration real run

```bash
SQLITE_SOURCE_URL='sqlite:////opt/resemblio-api/app/resemblio_dev.db' \
POSTGRES_TARGET_URL='postgresql+psycopg2://resemblio:<pw>@127.0.0.1:5432/resemblio' \
/opt/resemblio-api/venv/bin/python scripts/migrate_sqlite_to_postgres.py
```

The script exits 0 only if every table's post-insert count equals
source + before. Capture the JSON report to the cutover log
(`projects/Resemblio/Resemblio_BUILD_LOG.md`).

Spot-check a known row:

```bash
sudo -u postgres psql -d resemblio -c \
  "SELECT id, email, status, created_at FROM users ORDER BY id;"
```

### Step 5. Flip `.env`

```bash
sudo cp /opt/resemblio-api/.env /opt/resemblio-api/.env.bak.$TS
sudo $EDITOR /opt/resemblio-api/.env
# Change RESEMBLIO_DB_URL to:
# postgresql+psycopg2://resemblio:<pw>@127.0.0.1:5432/resemblio
sudo chown root:claude-cowork /opt/resemblio-api/.env
sudo chmod 640 /opt/resemblio-api/.env
```

### Step 6. Restart the API and verify health

```bash
sudo systemctl start resemblio-api
sleep 3
systemctl is-active resemblio-api
curl -fsS https://api.resemblio.com/healthz
journalctl -u resemblio-api -n 100 --no-pager | grep -i -E "(sqlite|error|exception)" || echo "no sqlite or error mentions in startup log"
```

The grep should find no SQLite references. If it does, the app is still
pointed at the old file; revisit Step 5.

### Step 7. Smoke test against Postgres

Pick a smoke that touches the database read path. With a known API key:

```bash
curl -fsS -H "Authorization: Bearer $KEY" https://api.resemblio.com/v1/account
```

Expect a 200 with the migrated user's account payload. If your operator
account predates the cutover, the returned `created_at` should match the
SQLite original (sanity check that ids and timestamps survived).

### Step 8. Mark the old SQLite file as quarantined

Do not delete yet. Rename so any accidental fallback fails loudly:

```bash
sudo mv /opt/resemblio-api/app/resemblio_dev.db \
        /opt/resemblio-api/app/resemblio_dev.db.cutover-$TS.archived
```

Keep the archived file on disk for 7 days, then delete after the next
nightly backup confirms Postgres is captured in `resemblio-backups`.

---

## Rollback (if any cutover step fails)

Rollback is feasible up until Step 5 (the `.env` flip) costs nothing more
than re-running it backwards. After Step 5, rollback restores the SQLite
file and reverts the env; any writes made against Postgres in the
intervening minutes are lost. Treat the window between Step 5 and Step 7
as the "no new writes" window: the API is up but the operator is the only
caller during smoke. Real customer traffic should be paused upstream
(temporary Caddy 503 for `api.resemblio.com` is the cleanest way).

### Failure before Step 5

```bash
sudo systemctl start resemblio-api  # still pointed at SQLite via unchanged .env
curl -fsS https://api.resemblio.com/healthz
```

Service restored. Investigate the failure, fix, re-run from Step 1.

### Failure during or after Step 5

```bash
sudo systemctl stop resemblio-api

# Restore .env
sudo cp /opt/resemblio-api/.env.bak.$TS /opt/resemblio-api/.env
sudo chown root:claude-cowork /opt/resemblio-api/.env
sudo chmod 640 /opt/resemblio-api/.env

# Restore SQLite file (un-quarantine)
sudo mv /opt/resemblio-api/app/resemblio_dev.db.cutover-$TS.archived \
        /opt/resemblio-api/app/resemblio_dev.db 2>/dev/null || true

# If the original was clobbered, restore from the P4 backup:
sudo cp /opt/resemblio-api/app/resemblio_dev.db.bak.$TS \
        /opt/resemblio-api/app/resemblio_dev.db
sudo chown claude-cowork:claude-cowork /opt/resemblio-api/app/resemblio_dev.db

sudo systemctl start resemblio-api
curl -fsS https://api.resemblio.com/healthz
```

Drop the Postgres data (so a re-run starts from a clean target) only after
the rollback is verified working:

```bash
sudo -u postgres psql -c "DROP DATABASE resemblio;"
sudo -u postgres psql -c "CREATE DATABASE resemblio OWNER resemblio;"
```

---

## Post-cutover follow-ups

1. Update `Resemblio_INFRA.md > Change log` with the cutover date and the
   row counts migrated.
2. Add Postgres to the nightly R2 backup script (`pg_dump` piped to rclone)
   if not already wired; remove SQLite from the same script.
3. Append a decisions-log entry to `projects/OptSus Team/brain/decisions-log.md`
   noting the cutover completed and the SQLite drift closed.
4. After 7 days of green Postgres operation, delete the quarantined SQLite
   archive and the R2 cutover backup.

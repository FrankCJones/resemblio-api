# CODEX Report S1

## Shipped

- FastAPI app under `app/` with config, database sessions, SQLAlchemy models, Pydantic schemas, auth middleware, token bucket rate limiting, crypto helpers, R2 storage, extractor bridge, and `/v1` routes.
- Alembic migration `0001_initial_schema.py` for all five required tables and indexes.
- API key lifecycle: create, list, rotate with 48-hour grace, revoke, audit events, rotated-key warning header, revoked-key rejection.
- Extraction lifecycle: credit check, pending row, debit ledger entry, extractor bridge, DTCG JSON, ZIP bundle with `tokens.json` and `manifest.json`, R2 upload, cached GET without recharge, failure refund.
- Account and credit balance endpoints.
- Stripe webhook S1 stub returning `202`.
- `scripts/create_first_user.py` local seed helper.
- Offline test suite with 17 tests across the requested 8 test files.

## DRL Adapter Friction

- `codex_extractor.py` has legacy optional persistence when `RESEMBLIO_DB_URL` is set. That insert does not match the new S1 schema because S1 requires `user_id` and `api_key_id`.
- I did not modify `codex_extractor.py` or `drl_adapter.py`.
- `app/extractor_bridge.py` temporarily removes `RESEMBLIO_DB_URL` only while calling `CodexExtractor().extract(url)`, then restores it. API-owned persistence remains the only write path.

## Validation

- `pytest`: passed, `17 passed in 2.88s`.
- `python -m py_compile app/*.py app/routes/*.py`: passed using `PYTHONPYCACHEPREFIX` because this Windows workspace denies creating `__pycache__` inside `app/`.
- Alembic SQLite round trip: `alembic upgrade head` passed, `alembic downgrade base` passed against fresh `alembic_validation.sqlite`.
- Uvicorn startup: passed. `GET /v1/healthz` returned `{"status":"ok"}` on `127.0.0.1:8017`.
- Live smoke with SQLite plus real R2 and real extractor credentials: passed.
  - Seed email: `codex-s1-smoke@resemblio.local`
  - Extraction id: `1`
  - R2 key: `extractions/1/1.zip`
  - R2 ZIP bytes read back: `1135`
  - Key create: `200`
  - Extraction: `200`
  - Cached GET: `200`
  - Old key after rotate: `200`
  - New key after rotate: `200`
  - Revoke: `200`
  - Revoked key after revoke: `401`
  - Final balance: `500`

## Blockers

- Fresh Postgres migration validation was not run. Docker is installed but the daemon is not running, and localhost port `5432` is closed. No `RESEMBLIO_DB_URL` Postgres credential exists in `_credentials/credentials.env`.
- The migration is written with Postgres `JSONB` and `INET` types plus SQLite variants for tests. The SQLite round trip and route tests are green.

## Not Done

- No deployment to `resemblio-prod-01`.
- No Stripe integration beyond the S1 webhook stub.
- No `_handoff` file created, per the outer Tool Coordination instruction to return via stdout and let the caller handle audit state.

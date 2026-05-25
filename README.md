# Resemblio API

FastAPI service for authenticated extraction, API key lifecycle, credit ledger persistence, and R2 ZIP delivery.

## File Map

- `app/main.py` - FastAPI app factory, router wiring, auth middleware.
- `app/config.py` - environment loader. Reads workspace `_credentials/credentials.env` without printing secrets.
- `app/db.py` and `app/models.py` - SQLAlchemy engine, sessions, and the five S1 tables.
- `app/auth.py` - bearer API key middleware with hash lookup, status checks, 48-hour rotation grace, usage events, and rate limiting.
- `app/rate_limit.py` - in-memory token buckets for S1. Redis can replace this behind the same check method later.
- `app/crypto.py` - API key generation, SHA-256 hashing with pepper, display redaction, and Argon2id password hashing.
- `app/storage.py` - Cloudflare R2 S3-compatible storage for extraction ZIP bundles.
- `app/extractor_bridge.py` - thin wrapper around `../extractor/codex_extractor.py`, with API-owned ZIP packaging.
- `app/routes/` - `/v1` endpoints for health, account, API keys, and extractions.
- `migrations/versions/0001_initial_schema.py` - Alembic schema migration for users, API keys, key events, extractions, and credit ledger.
- `tests/` - offline pytest suite using SQLite, fake R2, fake extractor, and moto.
- `scripts/create_first_user.py` - local seed helper for a dev account and starter key.

## Data Flow

1. A client authenticates with `Authorization: Bearer rsmb_live_<token>`.
2. Middleware validates the key format, hashes against `RESEMBLIO_KEY_PEPPER`, checks key status and rotation grace, applies the in-memory rate limit, and attaches the current user and key to `request.state`.
3. `POST /v1/extractions` checks credit balance, writes a pending extraction, appends an `extraction_charge`, calls the existing Codex extractor, converts the flat `TokenSet` to DTCG JSON, writes `tokens.json` plus `manifest.json` into a ZIP, uploads that ZIP to R2, and marks the extraction `ok`.
4. `GET /v1/extractions/{id}` returns the persisted JSON and a fresh 15-minute signed R2 URL without charging credits.
5. Failed extraction or storage work marks the extraction failed and appends a refund ledger row.

## API Contracts

Auth-free:

- `GET /v1/healthz` returns `{"status":"ok"}`.
- `POST /v1/webhooks/stripe` returns `202` and only logs body size in S1.

Authenticated:

- `POST /v1/extractions`
- `GET /v1/extractions?limit=20&before=<id>`
- `GET /v1/extractions/{id}`
- `POST /v1/api_keys`
- `POST /v1/api_keys/{id}/rotate`
- `POST /v1/api_keys/{id}/revoke`
- `GET /v1/api_keys`
- `GET /v1/account`
- `GET /v1/credit/balance`

Extraction JSON and ZIP manifests carry `schema_version`.

## Local Dev

Create Postgres:

```powershell
docker run -d -e POSTGRES_PASSWORD=dev -p 5432:5432 postgres:16
```

Set required env:

```powershell
$env:RESEMBLIO_DB_URL = "postgresql+psycopg://postgres:dev@localhost:5432/resemblio"
$env:RESEMBLIO_KEY_PEPPER = "<32-plus-character-local-secret>"
```

Run migrations:

```powershell
alembic upgrade head
```

Seed a user:

```powershell
python scripts/create_first_user.py frank@optsus.com
```

Run the API:

```powershell
uvicorn app.main:app
```

Run tests:

```powershell
pytest
```

Test extraction:

```powershell
curl -H "Authorization: Bearer rsmb_live_..." -X POST -H "Content-Type: application/json" -d "{\"url\":\"https://posthog.com\"}" http://localhost:8000/v1/extractions
```

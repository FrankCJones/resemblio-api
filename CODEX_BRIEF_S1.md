# Codex brief: Resemblio v1.1 - Stage 1 (backend scaffold + auth + persistence)

## 1. Mission

Build the FastAPI service that wraps the existing Codex extractor (`projects/Resemblio/code/extractor/`) into an authenticated HTTP API with persistence. This is the backend foundation for Resemblio v1.1; Stripe (S2), web UI (S3), MCP (S4), and hardening (S5) all sit on top of what you build here.

Mission brief context: `projects/OptSus Team/missions/resemblio-v1.1.md`. Read Section 2 (locked decisions D1-D24) before writing code; they answer every "what should X be" question.

## 2. Where this work lives

`projects/Resemblio/code/api/` (this folder; create the structure).

Layout you build:

```
projects/Resemblio/code/api/
  README.md                      # subsystem doc (file map, data flow, contracts)
  pyproject.toml                 # FastAPI + SQLAlchemy + Alembic + boto3 + httpx deps
  app/
    __init__.py
    main.py                      # FastAPI app, router wiring, middleware stack
    config.py                    # env loader; pydantic-settings; reads RESEMBLIO_DB_URL, ANTHROPIC_API_KEY, R2 creds
    db.py                        # SQLAlchemy engine, session factory
    models.py                    # SQLAlchemy ORM: User, ApiKey, ApiKeyEvent, Extraction, CreditLedger
    schemas.py                   # Pydantic v2 request/response schemas
    auth.py                      # API key middleware: extract, hash, lookup, rate-check, attach user
    rate_limit.py                # token-bucket per-key + per-user (in-memory for S1; Redis later)
    crypto.py                    # key generation, SHA-256 hash, prefix-redaction
    storage.py                   # R2 client (boto3 S3-compatible); put_zip + get_zip + sign_download_url
    extractor_bridge.py          # thin adapter calling projects/Resemblio/code/extractor/codex_extractor.py
    routes/
      __init__.py
      extractions.py             # POST /extractions, GET /extractions, GET /extractions/{id}
      api_keys.py                # POST /api_keys, POST /api_keys/{id}/rotate, POST /api_keys/{id}/revoke, GET /api_keys
      account.py                 # GET /account, GET /credit/balance
      health.py                  # GET /healthz (liveness; no auth)
  migrations/
    env.py                       # Alembic env
    versions/
      0001_initial_schema.py     # creates all 5 tables + indexes
  tests/
    __init__.py
    conftest.py                  # pytest fixtures: ephemeral SQLite DB, fake R2, fake extractor
    test_crypto.py               # key generation, hashing, redaction
    test_auth.py                 # middleware: missing key, bad key, valid key, rotated-grace, revoked
    test_rate_limit.py           # token bucket fills/empties correctly
    test_extractor_bridge.py     # extractor call with synthetic HTML
    test_routes_extractions.py   # POST creates row + R2 object; GET returns cached without charge
    test_routes_api_keys.py      # create, rotate (48h grace), revoke, audit log captured
    test_routes_account.py       # account info; credit balance
    test_storage.py              # R2 put/get round-trip (fake S3 via moto)
    test_db_migrations.py        # alembic upgrade head + downgrade base round-trips clean
  scripts/
    create_first_user.py         # CLI helper: create a user with a starter API key (for dev seeding only)
```

## 3. The extractor you integrate

`projects/Resemblio/code/extractor/codex_extractor.py` already exists and is live-validated. Import it via `extractor_bridge.py`. Do NOT modify the extractor itself.

The bridge calls `CodexExtractor().extract(url)` which returns `tuple[TokenSet | None, str | None]` per `drl_adapter.ResemblioExtractor` protocol. On success, you persist:
- Flat `TokenSet` as JSON in `extractions.tokens_json` (jsonb column)
- DTCG-formatted JSON (via `drl_adapter.to_dtcg_json(token_set)`) as the canonical download
- A ZIP bundle containing `tokens.json` (DTCG) plus a manifest at `manifest.json` (schema_version, url, extracted_at, sha256 of tokens.json) - uploaded to R2 at `extractions/{user_id}/{extraction_id}.zip`

## 4. Postgres schema (verbatim - do NOT redesign)

These are the five tables. Migration `0001_initial_schema.py` creates all of them. Field types are SQLAlchemy / Postgres dialect; use `sqlalchemy.dialects.postgresql.JSONB` for JSON columns.

### `users`
| Column | Type | Notes |
|---|---|---|
| id | BIGSERIAL PRIMARY KEY | |
| email | TEXT NOT NULL UNIQUE | citext-equivalent: lower() index for case-insensitive lookup |
| password_hash | TEXT NOT NULL | Argon2id; use `argon2-cffi` |
| stripe_customer_id | TEXT NULL | populated by S2; nullable in S1 |
| status | TEXT NOT NULL DEFAULT 'active' | active / suspended / deleted |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | |
| updated_at | TIMESTAMPTZ NOT NULL DEFAULT now() | |

### `api_keys`
| Column | Type | Notes |
|---|---|---|
| id | BIGSERIAL PRIMARY KEY | |
| user_id | BIGINT NOT NULL REFERENCES users(id) | |
| key_hash | TEXT NOT NULL UNIQUE | SHA-256 hex of `rsmb_live_<32 url-safe random bytes>` plus pepper |
| key_prefix | TEXT NOT NULL | `rsmb_live_abcd...wxyz` (first 8 + last 4 of plaintext) for display |
| label | TEXT NOT NULL | user-given label, e.g. "My laptop" |
| scopes | JSONB NOT NULL DEFAULT '["extract"]' | S1 single scope; future-proof |
| status | TEXT NOT NULL DEFAULT 'active' | active / rotated_out / revoked / suspended / expired |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | |
| last_used_at | TIMESTAMPTZ NULL | updated on every successful auth |
| expires_at | TIMESTAMPTZ NULL | optional per-key TTL |
| revoked_at | TIMESTAMPTZ NULL | |
| revoked_reason | TEXT NULL | enum: lost / rotated / no_longer_needed / suspected_compromise / leaked_detected / admin |
| created_from_ip | INET NULL | |
| grace_expires_at | TIMESTAMPTZ NULL | for status=rotated_out: timestamp after which key returns 401 |

Indexes: `ix_api_keys_user_id`, `ix_api_keys_status`, `ix_api_keys_grace_expires_at` (for the expiry sweeper).

### `api_key_events`
| Column | Type | Notes |
|---|---|---|
| id | BIGSERIAL PRIMARY KEY | |
| api_key_id | BIGINT NOT NULL REFERENCES api_keys(id) | |
| event_type | TEXT NOT NULL | created / used / rotated_out / rotated_in / revoked / suspended / expired / attempted_after_revocation |
| occurred_at | TIMESTAMPTZ NOT NULL DEFAULT now() | |
| ip | INET NULL | |
| metadata | JSONB NULL | extra context (e.g. revoke reason, user agent) |

Append-only by convention (no UPDATE / DELETE statements in code). Index: `ix_api_key_events_api_key_id_occurred_at`.

### `extractions`
| Column | Type | Notes |
|---|---|---|
| id | BIGSERIAL PRIMARY KEY | |
| user_id | BIGINT NOT NULL REFERENCES users(id) | |
| api_key_id | BIGINT NOT NULL REFERENCES api_keys(id) | which key created it |
| url | TEXT NOT NULL | |
| url_normalized | TEXT NOT NULL | for dedup queries; lower + trim |
| status | TEXT NOT NULL | pending / ok / failed |
| tokens_json | JSONB NULL | flat TokenSet; null until status=ok |
| dtcg_json | JSONB NULL | DTCG-formatted; null until status=ok |
| r2_zip_key | TEXT NULL | object key in `resemblio-extractions` bucket; null until status=ok |
| zip_sha256 | TEXT NULL | hex sha256 of ZIP contents |
| error_log | TEXT NULL | populated on status=failed |
| extracted_at | TIMESTAMPTZ NOT NULL DEFAULT now() | |
| schema_version | INT NOT NULL | use SCHEMA_VERSION from drl_adapter |
| credit_cents | INT NOT NULL DEFAULT 500 | $5.00 default for v1.1 public extractions |

Indexes: `ix_extractions_user_id_extracted_at`, `ix_extractions_url_normalized`.

### `credit_ledger`
Append-only ledger. Balance is computed by summing.

| Column | Type | Notes |
|---|---|---|
| id | BIGSERIAL PRIMARY KEY | |
| user_id | BIGINT NOT NULL REFERENCES users(id) | |
| entry_type | TEXT NOT NULL | onboarding_grant / topup / extraction_charge / refund / adjustment |
| amount_cents | INT NOT NULL | signed; positive = credit, negative = debit |
| balance_after_cents | INT NOT NULL | computed at insertion, stored for audit |
| stripe_payment_intent_id | TEXT NULL | populated by S2 for topup rows |
| extraction_id | BIGINT NULL REFERENCES extractions(id) | for extraction_charge rows |
| api_key_id | BIGINT NULL REFERENCES api_keys(id) | which key drained the balance |
| note | TEXT NULL | human-readable |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | |

Index: `ix_credit_ledger_user_id_created_at`.

S1 implements the table + the `onboarding_grant` write (so dev users can be seeded with $10) and `extraction_charge` writes on successful extraction. The `topup` flow is S2 (Stripe).

## 5. API endpoints (verbatim contract)

All routes under `/v1`. JSON request/response. Authentication via `Authorization: Bearer rsmb_live_<token>` header.

### Auth-free
- `GET /v1/healthz` -> `{"status":"ok"}` (liveness; no DB call)

### Authenticated
- `POST /v1/extractions` - request `{"url":"https://example.com"}`. Server: validate URL, decrement balance by 500 cents (fail with 402 if insufficient), create extractions row (status=pending), call extractor bridge, on success update row + upload ZIP to R2 + append extraction_charge ledger row, return `{"id":..., "status":"ok", "tokens":..., "dtcg":..., "download_url":...}`. On extractor failure: refund the 500 cents (credit_ledger refund row), return 502 with error_log.
- `GET /v1/extractions?limit=20&before=<id>` - paginated list of user's extractions; newest first
- `GET /v1/extractions/{id}` - single extraction including tokens + dtcg + signed download URL; 404 if not user's; **no charge** (this is the free re-fetch path that enables onboarding)
- `POST /v1/api_keys` - request `{"label":"My laptop"}`; returns the plaintext key ONCE; subsequent fetches see only the prefix
- `POST /v1/api_keys/{id}/rotate` - generates a new key (returned in plaintext once); marks the old as `rotated_out` with `grace_expires_at = now() + interval '48 hours'`
- `POST /v1/api_keys/{id}/revoke` - request `{"reason":"lost"}` (enum); marks as `revoked`, `revoked_at=now()`, `revoked_reason=<reason>`; appends api_key_events row
- `GET /v1/api_keys` - list keys for current user; never includes plaintext
- `GET /v1/account` - returns email, status, created_at, stripe_customer_id (nullable)
- `GET /v1/credit/balance` - returns `{"balance_cents":..., "last_entry_at":...}`

### Webhook (no API key auth; signed)
- `POST /v1/webhooks/stripe` - returns 202 always; S1 implements signature verification stub that accepts any valid request body and logs it (S2 wires actual Stripe event handling)

## 6. Auth middleware behavior (exact)

For each authenticated request:

1. Extract `Authorization: Bearer <token>`. If missing or malformed -> 401 `{"error":"missing_credentials"}`
2. Validate format `^rsmb_(live|test)_[A-Za-z0-9_-]{43}$`. If bad -> 401 `{"error":"invalid_credentials"}`
3. Compute `sha256(token + pepper)`. Lookup in `api_keys.key_hash`.
4. If no match -> 401 `{"error":"invalid_credentials"}`
5. If found but `status` in (`revoked`, `suspended`, `expired`) -> 401 with status-specific error and append `attempted_after_revocation` event (with metadata.ip)
6. If `status = rotated_out` and `now() > grace_expires_at` -> mark `expired`, return 401 `{"error":"key_expired","detail":"This key was rotated; see dashboard"}` and append `expired` event
7. If `status = rotated_out` and `now() <= grace_expires_at` -> proceed but add response header `X-API-Key-Rotation-Warning: This key was rotated. Replace with new key by <grace_expires_at>`
8. Check rate limit (token bucket: 60/min and 5000/day per key; in-memory store keyed by key_hash; S1 is sufficient with in-memory; document for Redis swap later)
9. Update `last_used_at = now()`; append `used` event with metadata `{route, status_code}` AFTER the handler completes (use middleware on_response hook or wrap)
10. Attach `current_user` and `current_api_key` to request.state for handlers

Pepper: env var `RESEMBLIO_KEY_PEPPER` (32+ chars). Old pepper in `RESEMBLIO_KEY_PEPPER_OLD` for rotation transitions; lookup tries new pepper first, then old. If neither env var present, refuse to start with a clear error.

## 7. Crypto utilities (`app/crypto.py`)

- `generate_api_key(env: Literal["live","test"]) -> tuple[str, str, str]` returns `(plaintext, hash, prefix)`. Uses `secrets.token_urlsafe(32)`.
- `hash_api_key(plaintext: str, pepper: str) -> str` SHA-256 hex.
- `redact_api_key(plaintext: str) -> str` returns `rsmb_live_abcd...wxyz` (first 8 of body + last 4 of body).
- `hash_password(plaintext: str) -> str` Argon2id (use `argon2-cffi` defaults).
- `verify_password(plaintext: str, hash: str) -> bool`.

## 8. R2 storage (`app/storage.py`)

Use `boto3` with the S3-compatible R2 endpoint. Env vars: `CLOUDFLARE_R2_ENDPOINT`, `CLOUDFLARE_R2_ACCESS_KEY`, `CLOUDFLARE_R2_SECRET_KEY`. Bucket: `resemblio-extractions` (create it during the first run if it doesn't exist; idempotent).

- `put_extraction_zip(extraction_id: int, user_id: int, zip_bytes: bytes) -> tuple[str, str]` uploads to `extractions/{user_id}/{extraction_id}.zip`, returns `(object_key, sha256_hex)`.
- `get_extraction_zip(object_key: str) -> bytes`.
- `sign_download_url(object_key: str, expires_in: int = 900) -> str` 15-minute presigned URL.

For tests, use `moto` to fake S3.

## 9. Local dev environment

Document in `README.md`:

- Local Postgres via Docker: `docker run -d --name resemblio-pg -e POSTGRES_PASSWORD=dev -p 5432:5432 postgres:16`
- `RESEMBLIO_DB_URL=postgresql+psycopg://postgres:dev@localhost:5432/resemblio`
- Alembic upgrade: `alembic upgrade head`
- Seed first user: `python scripts/create_first_user.py --email frank@optsus.com`
- Run server: `uvicorn app.main:app --reload --port 8000`
- Test live extraction: `curl -H "Authorization: Bearer rsmb_live_..." -X POST -H 'Content-Type: application/json' -d '{"url":"https://posthog.com"}' http://localhost:8000/v1/extractions`

Tests run against SQLite via the conftest fixture (no live DB needed); only the seed script + manual smoke test hit Postgres.

## 10. Quality floor (workspace standard)

Per workspace `CLAUDE.md > Quality floor`:

- Type hints on every function signature
- Docstrings on every public function explaining intent + edge cases
- TypedDict / dataclass / Pydantic for data shapes (no bare dicts in app code)
- Retry with backoff on every outbound network call (extractor LLM call already has it; R2 calls should too)
- Unit tests for every pure-data function in the test files listed above
- Output files (extraction JSON, ZIP manifest) carry `schema_version` (use `SCHEMA_VERSION` from drl_adapter)
- `README.md` at subsystem level explaining file map, data flow, contracts
- Magic numbers in named constants in a shared `constants.py` (e.g., `DEFAULT_EXTRACTION_CENTS = 500`, `ROTATION_GRACE_HOURS = 48`, `RATE_LIMIT_PER_MIN = 60`)
- Single dashes only. No em-dashes. No "nestled."
- No `print` statements in app code; use `logging` configured at startup

## 11. Validation before declaring done

1. `alembic upgrade head` succeeds against fresh Postgres
2. `alembic downgrade base` round-trips clean
3. `pytest` passes 100%
4. `python -m py_compile app/*.py app/routes/*.py` clean
5. `uvicorn app.main:app` starts without errors
6. Seeded first user can: create API key, POST /v1/extractions with a real URL (posthog.com), see the extraction persisted with status=ok, retrieve it via GET /v1/extractions/{id} without re-charging, rotate the key (both old + new work within 48h), revoke the key (subsequent use returns 401)
7. The seeded $10 onboarding_grant ledger row exists; the extraction_charge of -500 cents was appended; balance_after_cents is 500
8. R2 bucket `resemblio-extractions` contains the ZIP for that extraction
9. No em-dashes / banned words in authored files

If any step fails, log it in CODEX_REPORT_S1.md and stop; do not paper over.

## 12. Token budget

Target 800-1200 lines of Python across all files you author (significantly larger than the extractor brief because of the schema + endpoints + tests). If you reach 2000+ lines, stop and ask Claude via a handoff message.

## 13. What you do NOT do in S1

- Stripe integration of any kind (S2 owns this; webhook stub only)
- Web UI (S3 owns this)
- MCP server (S4 owns this)
- Rate limiting beyond in-memory token bucket (Redis is post-S1)
- Anomaly detection (S5 owns this)
- Deploy to `resemblio-prod-01` (S1 ships local; deploy is between S1 and S2)
- Modify `projects/Resemblio/code/extractor/` (read-only; if it needs changes, write them as a proposal in CODEX_REPORT_S1.md and stop)
- Edit files outside `projects/Resemblio/code/api/` and `projects/Resemblio/_handoff/`

## 14. Deliverables to Claude

1. The folder structure above, populated
2. `CODEX_REPORT_S1.md` in `projects/Resemblio/code/api/` with: what shipped, what didn't, any DRL-adapter friction, validation results from Section 11, the seeded user's email + extraction id for verification
3. A final handoff message to claude at `projects/Resemblio/_handoff/inbox/claude/<id>.md` with intent=answer summarizing the result (schema for the message is at `projects/Tool Coordination/_handoff/PROTOCOL.md`)

## 15. Authority (what's GREEN for you)

Per the v1.1 mission brief Section 6 authority bundle:
- All writes under `projects/Resemblio/code/api/`
- Local Postgres schema migrations (no production touches in S1)
- R2 bucket `resemblio-extractions` creation per workspace lifecycle defaults
- Reading from `projects/Resemblio/code/extractor/`, `_credentials/credentials.env`, `context/Infrastructure.md`

Not GREEN: Stripe integration (S2), deploying to `resemblio-prod-01` (between-stage step), modifying the extractor.

---

End of brief. Read, then build.

# `seed_from_drl.py` design

Status: DESIGN ONLY (2026-05-26). No code yet. Frank approval gate before implementation.

## 1. The DRL corpus, in real numbers

Source of truth: `projects/Design Reference Library/corpus.json`. As of 2026-05-21 it reports `asset_count: 955` across `system_count: 41` systems. Each asset is one design-system unit (alphabet, atom, whole, layout, library).

Per-asset fields visible at the top level of `corpus.json`:

- `slug`, `class` (alphabets | atoms | wholes | layouts | libraries), `kind`, `path` (relative to DRL root)
- `tokens_path` - CSS file with the asset's tokens
- `tldr`, `patterns`, `mood`, and provenance metadata
- Parent `systems[].slug`, `name`, `tier` (A / B / C / D), `category`

For 24 systems there is also `_extractions/<slug>/extraction.json` - the full `ExtractionRecord` (TokenSet + SectionOutlines) shape produced by the Gen-2 extraction pipeline. The DRL CLAUDE.md (Section "Read these to understand the system") points to `SCHEMA.md`, `TOKEN_CONTRACT.md`, and `_scripts/extraction.py` for the canonical schemas.

There are NO source URLs that need re-fetching from the live web. DRL already holds the brand-attributed tokens. Seeding therefore is a brand-strip + transform + DB+R2 write, never an extraction.

## 2. Brand strip pipeline

The DRL is read-only from Resemblio (forbidden actions, Resemblio CLAUDE.md L37 + Build constraint "Design Reference Library - upstream, untouched"). Resemblio already vendors a copy at `code/api/_vendored/drl/drl/` and the `code/api/extractor/` adapter consumes that vendored copy at runtime.

A brand-stripping transformer is mentioned in the workspace project index entry for Resemblio ("`code/transformer/` module reads from DRL and produces brand-stripped versions"). At the time of this design that module does not yet exist on disk - only `code/api/extractor/` (the live URL extractor) and `code/api/_vendored/drl/` (the imported DRL scripts).

**Decision needed (Frank):** confirm whether the brand strip should

- (a) reuse the live extractor's brand-normalization passes (run the existing `extractor/drl_adapter.py` on the already-brand-attributed DRL TokenSets and let it produce the brand-stripped output that the live API would ship), OR
- (b) build a separate `code/transformer/` module per the original plan, with its own tests and SCHEMA.

The seeding script is otherwise identical between the two; only the call to "strip brand" differs.

## 3. Ingestion path - bypass HTTP and credit ledger

Direct in-process write. The script:

1. Opens its own SQLAlchemy session against `RESEMBLIO_DB_URL` (the same env var the API reads).
2. Uses `app.storage.R2Storage` directly to PUT the ZIP into the `resemblio-extractions` bucket. Reuses `extractions/<seed_user_id>/<extraction_id>.zip` key shape so the existing `sign_download_url` path keeps working.
3. INSERTs an `Extraction` row with `status="ok"`, `tokens_json`, `dtcg_json`, `r2_zip_key`, `zip_sha256`, `schema_version = SCHEMA_V1`.
4. Skips `credit_ledger` entirely - seed rows are not billed. Skips `api_key_id` charge bookkeeping.

This means the `Extraction.api_key_id` foreign key needs to be nullable, OR seed rows reuse a single system "seed-bot" API key. **Recommendation:** make it nullable via the schema migration in Section 4. The api_key_id field is bookkeeping for organic charges; semantically null is correct for seed rows.

### New column: `extractions.seed_source`

`Text NULL`. Examples: `drl:anthropic:wholes/hero`, `drl:airtable:atoms/button-primary`. Three uses:

- Visibility filter at query time (default API excludes `seed_source IS NOT NULL` until v1.1 public-corpus visibility ships)
- Idempotency anchor (Section 4)
- Provenance audit ("which DRL asset became this row")

## 4. Resumability and idempotency

The script must be safe to re-run end-to-end (Frank kills mid-batch, rerun continues).

**Idempotency key:** `(seed_source)`. A unique constraint on that column when not-null gives us a single source-of-truth row per DRL asset.

**Schema migration:** `migrations/versions/0007_extractions_seed_source.py` (alembic). Two changes:

```
ALTER TABLE extractions ADD COLUMN seed_source TEXT NULL;
ALTER TABLE extractions ALTER COLUMN api_key_id DROP NOT NULL;
CREATE UNIQUE INDEX ux_extractions_seed_source ON extractions (seed_source) WHERE seed_source IS NOT NULL;
```

The partial unique index keeps organic rows (where `seed_source IS NULL`) unaffected.

**UPSERT pattern:** `INSERT ... ON CONFLICT (seed_source) WHERE seed_source IS NOT NULL DO UPDATE SET tokens_json = EXCLUDED.tokens_json, dtcg_json = EXCLUDED.dtcg_json, r2_zip_key = EXCLUDED.r2_zip_key, zip_sha256 = EXCLUDED.zip_sha256, schema_version = EXCLUDED.schema_version`.

R2 writes are naturally idempotent under the key `extractions/<seed_user_id>/<extraction_id>.zip`, but the extraction_id changes on re-insert; for re-runs the existing row's extraction_id is reused (the UPSERT returns the original id), so the R2 key is stable across re-runs.

## 5. Dry-run is mandatory; live mode behind `--apply`

```
python -m scripts.seed_from_drl                 # dry-run by default; prints planned ops
python -m scripts.seed_from_drl --apply         # actually writes
python -m scripts.seed_from_drl --apply --only systems=anthropic,airtable    # subset
python -m scripts.seed_from_drl --apply --batch-size 25 --max-rows 100       # bounded run
```

Dry-run prints, per asset: `seed_source`, `INSERT` vs `UPDATE`, estimated bytes (tokens JSON + ZIP), and a running total. No DB writes, no R2 writes.

## 6. Quality floor

- Each public function: docstring explaining intent + edge cases (workspace floor)
- `DrlAssetRow` TypedDict for the corpus.json row shape; `SeedPlanRow` for the dry-run plan
- Transactional batches of `BATCH_SIZE = 25` rows. One R2 PUT per row inside the batch; the SQL transaction wraps the row INSERTs only (R2 PUTs are outside the txn because S3 has no two-phase commit; on partial failure the next dry-run will reconcile)
- `schema_version = SCHEMA_V1` stamped per row, consistent with organic rows
- Logger configured (`logging.getLogger("seed_from_drl")`) since the script will eventually run unattended for the bulk-seed pass
- Tests in `tests/test_seed_from_drl.py` covering: corpus.json parsing, brand-strip wiring (mocked), dry-run plan generation, UPSERT idempotency on re-run, partial-batch resume (synthetic fixture; sqlite is fine for the table-shape test)

## 7. Resource sizing (1k assets)

Assumptions (Frank's brief): ~1 KB tokens JSON per asset, ~50 KB ZIP per asset.

- Postgres: 1,000 rows. Tokens + DTCG JSONB ~ 5 KB compressed per row (the DTCG payload is larger than raw tokens). Total row size ~10 KB inc. metadata. **~10 MB Postgres growth.** Negligible against the cpx21 80 GB disk.
- R2: 1,000 ZIPs at 50 KB each = **~50 MB R2 storage.** At R2 pricing ($0.015/GB-month) this is **~$0.001/month** ongoing. Egress only when the seeded corpus becomes browseable in v1.1; class B operations (write) are $4.50/million, so 1k writes is ~$0.005 one-time.

Total marginal infra cost: under one cent / month. The sizing is not the constraint; the constraint is curation quality.

## 8. Out of scope for this design (explicit)

- **Public-corpus visibility.** Per Resemblio CLAUDE.md "Public corpus hidden in v1, visible in v1.1 once moderation tooling exists." This design seeds the rows; the visibility flag stays off until v1.1. Frank-side D20 reassessment governs that flip, not this script.
- **Brand-strip moderation.** DRL is already curated (the `/dl` modes enforce quality grading A through D before an asset lands in the corpus). The seed script trusts DRL's grading; it does not re-moderate.
- **MCP / converter format generation.** The seed script writes the canonical extraction (tokens + DTCG + ZIP). Per-format derivatives (Tailwind, Style Dictionary, shadcn) are a v1.1+ extraction-service concern.
- **Live re-extraction.** Seed rows are static snapshots of the DRL state at seed time. Watchdog re-extract behavior applies to organic rows only.

## 5-line summary (for Jim/Frank)

DRL is `corpus.json` with 955 assets across 41 systems, already brand-attributed and curated. The seed script reads that file, brand-strips each asset (via the existing extractor adapter), and writes directly to Postgres + R2 from a script-owned session, bypassing the HTTP API and credit ledger. Idempotency is anchored by a new nullable `extractions.seed_source` column with a partial unique index, so the script is safe to re-run. Dry-run is the default; `--apply` is required to actually write. Total infra cost for the full seed is roughly $0.005 one-time plus $0.001/month - the constraint is curation, not capacity.

## Open questions for Frank

1. **Brand-strip module:** reuse the live extractor adapter (option a in Section 2) or build a separate `code/transformer/` per the original project index entry (option b)? Recommendation: option a for v1 seed, option b only if a second consumer emerges.
2. **Seed-bot user:** create one synthetic `User` row owned by `frank@optsus.com` and tagged `email = "seed-bot@resemblio.internal"`, OR assign all seed rows to Frank's existing user_id? Recommendation: synthetic seed-bot user so seed rows are filterable by user_id as a second defense beyond `seed_source IS NOT NULL`.
3. **Public-visibility flip path:** when v1.1 ships moderation tooling, do we want a one-shot script to flip all seed rows visible, or a per-asset moderation gate (likely the second per the "hidden in v1, visible in v1.1 once moderation tooling exists" language)? Not a blocker for this design; flagging so the seed schema can accommodate either.
4. **Tier filter on first seed pass:** seed only Tier A systems (which 41-system tier count is unknown without a sweep)? Or seed all 41 systems and let the public-corpus moderation step in v1.1 filter on visibility? Recommendation: seed everything; visibility is a v1.1 concern.

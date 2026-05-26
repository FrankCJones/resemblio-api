# S2 Merge Plan - Resemblio v1.1 Payment Surface

Status: PREP ONLY. Do not merge until the gating signal below is GREEN.

## Gating signal

Codex cycle 6 cross-review GREEN on the payment-code bundle.

Current state as of 2026-05-26 04:21 UTC: Codex cycle 6 returned BLOCKER (handoff `projects/Resemblio/_handoff/inbox/claude/2026-05-26T04-21-39-37ad54.md`):

1. `payment_status` gate fails open when the field is absent. Must require `checkout.payment_status == "paid"`.
2. Stuck `processing` idempotency rows have no recovery path. Need a claim lease (`claimed_at`) plus stale-claim reclamation after a short timeout.
3. Broad `IntegrityError` handler in `app/routes/webhooks.py:102-110` can mark unrelated failures as processed. Narrow to verified duplicate case only.
4. Minor: comment in `migrations/versions/0005_stripe_event_status.py:14` is stale (says row removed on failure; implementation now leaves `status='failed'`).

This merge plan assumes cycle 7 lands those fixes and Codex returns GREEN. Hold the merge until that handoff arrives.

## Branch state

- Working tree: `projects/Resemblio/code/api/`
- S2 scope across `app/` (payments, webhooks, credit routes, email, users, config) and migrations `0002`-`0005`
- Tests: `tests/test_credit_balance.py`, `test_extraction_pricing.py`, `test_spend_cap.py`, `test_stripe_checkout_create.py`, `test_stripe_webhook_signature.py`, `test_stripe_webhook_topup.py`, `test_s2_acceptance.py`, `test_stripe_event_status` coverage in `test_concurrency.py`, `test_db_migrations.py`
- Run `git status` and `git log main..HEAD` on the operator workstation to capture the actual diff before opening the PR

## PR body draft

```
Title: S2 - Stripe payments, credit ledger, webhook idempotency

## Summary
Adds the TEST-mode payment surface to the Resemblio API: Stripe customer creation,
credit ledger, public/private extraction pricing, per-key spend caps, Stripe
Checkout top-ups, and a signature-verified webhook handler with stateful
idempotency.

## Fix log - cycles 1-6
- Cycle 1: initial S2 implementation per CODEX_BRIEF_S2.md (customer create,
  ledger, pricing, spend caps, top-up endpoint, webhook stub replacement).
- Cycle 2: tightened webhook signature verification; added replay protection
  via `stripe_events_seen` (migration 0003).
- Cycle 3: added `topup_sessions` table and `balance_after_cents >= 0` CHECK on
  `credit_ledger` (migration 0004) so a malformed handler cannot drive a user
  negative.
- Cycle 4: hardened Checkout creation to card-only; added retry-with-backoff on
  all Stripe API calls; redacted PII from any logged event.
- Cycle 5: added atomic TopupSession status transition so concurrent webhook
  redeliveries cannot double-credit; added `test_concurrency.py`.
- Cycle 6: introduced stateful idempotency via `status` column on
  `stripe_events_seen` (migration 0005); event is claimed as `processing`,
  flipped to `processed` only after side effects commit; failure path marks
  `failed` so Stripe redelivery can re-claim.
- Cycle 7 (post-review fixes): require `payment_status == "paid"` (fail-closed);
  add `claimed_at` lease and stale-claim reclamation on `stripe_events_seen`;
  narrow IntegrityError handler to verified duplicate case; correct migration
  0005 docstring.

## Test evidence
- Local: 56/56 green via `.\.venv\Scripts\python.exe -m pytest`
- Alembic round trip: `alembic upgrade head` and `alembic downgrade base` clean
  on `sqlite+pysqlite:///s2_migration_validation.sqlite`
- `python -m py_compile app/*.py app/routes/*.py`: clean
- Style scan: no em-dashes, no double-dashes, no "nestled"

## Codex paired-review confirmation
- [ ] Codex cycle 7 cross-review: GREEN
- Handoff path: projects/Resemblio/_handoff/inbox/claude/<id>.md
- Reviewer: codex-gpt-5
- Per workspace CLAUDE.md + mission brief Section 8, payment code requires
  Tool Coordination `cross-review-diff` GREEN before merge to main.

## Migration 0005 deployment notes
- 0005 adds `status TEXT NOT NULL DEFAULT 'processed'` to `stripe_events_seen`.
- Existing rows backfill to `processed` via `server_default` so the ADD COLUMN
  statement carries no manual data step.
- Postgres applies the default during ADD COLUMN; SQLite recreates the table
  via `batch_alter_table`. Both paths are exercised by `test_db_migrations.py`.
- Apply order on prod: stop API -> `alembic upgrade head` -> start API.
  The webhook endpoint must not accept traffic while the column is being added;
  Stripe will retry any deliveries dropped during the brief window.

## Required env vars in prod
The systemd EnvironmentFile on resemblio-prod-01 must carry:

- `RESEMBLIO_KEY_PEPPER` (>= 32 chars; existing value, do not rotate during S2 merge)
- `STRIPE_RESTRICTED_KEY_RESEMBLIO_TEST` (rk_test_...; server-side Stripe calls)
- `STRIPE_WEBHOOK_SECRET_RESEMBLIO_TEST` (whsec_...; captured from Stripe
  Dashboard at endpoint creation - see STRIPE_WEBHOOK_SETUP.md)
- `RESEMBLIO_TOPUP_SUCCESS_URL` (default: https://resemblio.com/dashboard/credit?topup=success)
- `RESEMBLIO_TOPUP_CANCEL_URL` (default: https://resemblio.com/dashboard/credit?topup=cancel)
- `RESEND_API_KEY`
- `RESEMBLIO_RESEND_FROM_EMAIL` (default: "Resemblio <hello@resemblio.com>")
- `RESEMBLIO_DB_URL` (Postgres DSN; existing)
- Stripe price IDs are NOT used in S2 - Checkout sessions are built with
  `line_items.price_data` (ad-hoc unit_amount per request). When subscriptions
  land in S3+, add `STRIPE_PRICE_ID_*` vars then.

`STRIPE_*_LIVE` values must remain absent from the prod env. `app/config.py`
filters them out of the local credentials loader; the systemd unit MUST NOT
set them.

## Out of scope (deferred)
- Bulk top-up bonuses (+10% / +20%)
- Subscription billing
- Dashboard signup + top-up UI (S3)
- Live Stripe mode (YELLOW, Frank-only, after S5)
```

## Merge sequence (when GREEN)

1. Pull latest `main` into the S2 branch and rebase (clean any drift).
2. Run the full test suite locally one more time; confirm 56/56 + concurrency tests.
3. Run `alembic upgrade head` then `alembic downgrade base` against an ephemeral SQLite to re-verify round trip.
4. Open the PR with the body above.
5. Wait for CI green.
6. Merge with a merge commit (preserve cycle history); do not squash.
7. Deploy per `STRIPE_WEBHOOK_SETUP.md` order of operations.
8. Watch the monitoring signals listed in `S2_ROLLBACK.md` for the first hour.

## Pre-merge checklist

- [ ] Codex cycle 7 review GREEN, handoff archived
- [ ] Local pytest 56/56
- [ ] Alembic round trip clean
- [ ] py_compile clean across `app/*.py` and `app/routes/*.py`
- [ ] No `STRIPE_*_LIVE` references anywhere in the diff
- [ ] STRIPE_WEBHOOK_SETUP.md walked through on Stripe TEST dashboard
- [ ] Webhook signing secret captured into prod systemd EnvironmentFile
- [ ] Resend API key present in prod env, sender verified for resemblio.com
- [ ] Rollback runbook (`S2_ROLLBACK.md`) re-read by operator

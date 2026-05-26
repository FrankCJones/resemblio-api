# S2 Rollback Runbook

When to use: a live Stripe event hits a regression after the S2 merge and the safest move is to restore the pre-S2 state. Run this in order. Do not improvise; the data path touches real customer credit.

## Decision rule

Roll back if any of the following is true within the first hour after deploy:

- A `checkout.session.completed` event for `purpose=credit_topup` does not produce a `topup` row in `credit_ledger` AND the matching `stripe_events_seen` row is stuck in `status='processing'`
- A user's balance goes negative (CHECK constraint should prevent this; if you see the error, the constraint is doing its job, but investigate)
- `payment_status` of an event is something other than `"paid"` and the user is still credited
- Resend send rate jumps for "topup cleared" emails with no matching ledger growth (double-send symptom)
- 5xx rate on `POST /v1/webhooks/stripe` exceeds 1% over a 10-minute window

Otherwise, prefer a forward fix.

## Step 1 - Halt the webhook

- In the Stripe Dashboard: Developers -> Webhooks -> the `https://api.resemblio.com/v1/webhooks/stripe` endpoint -> Disable.
- Stripe queues redeliveries for up to 3 days. No customer event is lost while the endpoint is disabled; deliveries resume when re-enabled.
- Optionally also block the route at Caddy with a temporary 503 reply, but the Stripe-side disable is the canonical halt.

## Step 2 - Snapshot state

Before any destructive action:

- `pg_dump --schema-only` and `pg_dump --data-only --table=credit_ledger --table=stripe_events_seen --table=topup_sessions` from `resemblio-prod-01` to a timestamped file under `/var/backups/resemblio/`.
- Copy the file to R2 (`resemblio-prod-backups` bucket) under `rollback/<timestamp>/`.
- `journalctl -u resemblio-api --since "1 hour ago" > /var/log/resemblio/rollback-<timestamp>.log`.

## Step 3 - Alembic downgrade for 0005

The 0005 migration adds `status` to `stripe_events_seen`. To revert:

```
cd /opt/resemblio-api/app
sudo -u resemblio /opt/resemblio-api/venv/bin/alembic downgrade 0004_topup_sessions_and_balance_check
```

Notes:

- `downgrade()` drops the `status` column via `batch_alter_table`. Postgres uses `ALTER TABLE DROP COLUMN`; the operation is fast but takes an `ACCESS EXCLUSIVE` lock for the duration. Expect a brief stall on any concurrent query against the table.
- Once `status` is gone, the pre-S2 webhook handler logic (where presence of a row meant "processed") becomes correct again, so this downgrade is only meaningful in tandem with Step 4 (code revert).
- If the regression is isolated to the idempotency state machine and the ledger writes are correct, you may choose to keep 0005 and only revert the code; document the decision in the incident log.

To fully revert all S2 migrations (extreme case, after data audit):

```
sudo -u resemblio /opt/resemblio-api/venv/bin/alembic downgrade 0001_initial_schema
```

This drops `topup_sessions`, the ledger CHECK, `stripe_events_seen`, and `api_keys.spend_cap_cents`. Do this only if the corresponding ledger rows have been audited and quarantined. Existing `credit_ledger` rows survive because `0001` already defines the table; the entry types added in S2 remain as data and must be reconciled manually.

## Step 4 - Git revert path

The S2 merge commit on `main` should be a merge commit (preserves cycle history). To revert:

```
cd /home/operator/resemblio-api
git fetch origin
git checkout main
git pull --ff-only
git revert -m 1 <S2-merge-sha>
git push origin main
```

CI will redeploy the pre-S2 image to resemblio-prod-01. systemd `resemblio-api` restarts automatically on deploy completion.

If CI is wedged, manual deploy:

```
ssh operator@resemblio-prod-01
cd /opt/resemblio-api
sudo git fetch && sudo git checkout <pre-S2-sha>
sudo /opt/resemblio-api/venv/bin/pip install -r requirements.txt
sudo systemctl restart resemblio-api
```

After revert, run `alembic current` to confirm the live schema matches the reverted code's expectations. If schema is ahead of code, run the downgrade in Step 3.

## Step 5 - Webhook secret rotation

If the regression hint suggests secret leakage (unexpected signed events from unknown sources, log lines that include the secret, repo grep hit), rotate immediately:

1. Stripe Dashboard -> Developers -> Webhooks -> the endpoint -> Signing secret -> Roll secret.
2. Capture the new secret.
3. Update the prod systemd EnvironmentFile: `STRIPE_WEBHOOK_SECRET_RESEMBLIO_TEST=whsec_<new>`.
4. `sudo systemctl restart resemblio-api`.
5. Update `_credentials/credentials.env` in the operator workstation.
6. Re-enable the webhook endpoint in Stripe.
7. Trigger a Stripe test event from the dashboard and verify a 202 response.

Rotation is non-disruptive to in-flight events because Stripe accepts the previous secret for a short overlap window. Confirm the overlap window in the Stripe dashboard at rotation time.

## Step 6 - Re-enable the endpoint

Only after Steps 3-5 are complete and the operator has verified:

- `alembic current` matches the deployed code's expected head
- `select count(*) from stripe_events_seen where status='processing' and claimed_at < now() - interval '5 minutes'` returns 0 (no stuck rows left from the regression window)
- A manual Stripe test webhook returns 202

Then re-enable the endpoint in the Stripe Dashboard. Stripe will replay queued events; the idempotency table handles duplicates.

## Step 7 - First-hour monitoring signals

Watch these signals for the first hour after re-enable (or after the original S2 deploy):

- **Sentry**: any new issue tagged `payments` or `webhooks` -> page operator
- **systemd journal**: `journalctl -u resemblio-api -f` filtered for `ERROR` or `webhook` -> spike investigation
- **Postgres queries** (run every 5 minutes from operator workstation):
  - `SELECT status, count(*) FROM stripe_events_seen WHERE created_at > now() - interval '1 hour' GROUP BY status;` -> `processing` should approach 0, `failed` should be 0, `processed` matches the Stripe Dashboard event count
  - `SELECT count(*), sum(amount_cents) FROM credit_ledger WHERE entry_type='topup' AND created_at > now() - interval '1 hour';` -> matches the sum of Stripe `checkout.session.completed` events with `purpose=credit_topup`
  - `SELECT count(*) FROM topup_sessions WHERE status='pending' AND created_at < now() - interval '15 minutes';` -> should be 0 or small; growth means the webhook is not closing sessions
- **Stripe Dashboard**: Developers -> Webhooks -> endpoint -> success rate must be 100%; any 4xx is a code bug, any 5xx is an infra bug
- **Resend Dashboard**: top-up cleared email count must equal the count of new `topup` ledger rows
- **Plausible**: traffic to `/dashboard/credit?topup=success` should approximate the Checkout completion count (rough cross-check from the user side)

## Post-rollback

- Open an incident note at `projects/Resemblio/_handoff/inbox/human/<timestamp>-s2-rollback.md` with: trigger signal, time of rollback, what was rolled back (code only, schema only, both), customer impact estimate, follow-up actions.
- Reconcile any partially-processed Stripe events manually against `credit_ledger`. Stripe is the source of truth for what the customer paid; the ledger must match.
- Do not attempt a re-deploy of S2 until the underlying regression has a fix branch, a passing test that reproduces the regression, and a fresh Codex cross-review.

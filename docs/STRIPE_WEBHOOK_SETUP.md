# Live Stripe Webhook Setup - Resemblio v1.1 S2

Scope: registering the Stripe webhook endpoint against the resemblio-prod-01 API and capturing the signing secret into the prod environment. TEST mode only; LIVE mode is YELLOW and out of S2 scope.

Order of operations matters. The API will fail-loud on startup if `STRIPE_WEBHOOK_SECRET_RESEMBLIO_TEST` is missing (see `app/config.py:validate_stripe_test_settings`), so the secret must be in place BEFORE the post-merge restart.

## Prerequisites

- Stripe Dashboard access for the Resemblio account, TEST mode toggled on
- SSH access to `resemblio-prod-01`
- Write access to the systemd EnvironmentFile (typically `/etc/resemblio/api.env`) and to `_credentials/credentials.env` on the operator workstation
- The S2 code is merged and CI-deployed, but the systemd service has NOT yet been restarted (or is in the brief window where the old code is still running)

## Step 1 - Confirm the production endpoint URL

Public webhook URL: `https://api.resemblio.com/v1/webhooks/stripe`

Verify the DNS + Caddy path is up before registering with Stripe:

```
curl -i https://api.resemblio.com/v1/health
```

Expect `200 OK`. If the health check fails, fix the front door first; registering a Stripe endpoint that 5xx's will generate noise in the Stripe Dashboard.

## Step 2 - Register the webhook endpoint in Stripe

1. Stripe Dashboard -> top-right toggle set to **Test mode**.
2. Developers -> Webhooks -> **Add an endpoint**.
3. Endpoint URL: `https://api.resemblio.com/v1/webhooks/stripe`
4. Description: `Resemblio API - credit top-up handler (S2)`
5. API version: leave at the account default (Stripe sends events shaped by this version; the handler uses the SDK's own parser so version drift within the same major is tolerated).
6. Events to send: select **Select events** and add exactly these:
   - `checkout.session.completed`
   - `checkout.session.async_payment_succeeded`
   - `checkout.session.async_payment_failed`
7. Click **Add endpoint**.

Notes on the event list:

- `checkout.session.completed` is the primary credit path. The handler also requires `payment_status == "paid"` on the embedded checkout object before crediting; async card methods deliver the completed event with `payment_status="unpaid"` first and finalize via the async events below.
- `checkout.session.async_payment_succeeded` carries the eventual `paid` state for delayed payment methods (ACH, some wallets). The handler treats it the same as a paid completion.
- `checkout.session.async_payment_failed` is informational; the handler marks the `topup_sessions` row failed and does not credit. Required so we close the loop on the session and surface the failure in monitoring.
- Do NOT subscribe to `payment_intent.succeeded` for credit flow. The earlier S1/S2 stub accepted it; the cycle 6+ handler ignores it (acks with 202 and writes no ledger row). Subscribing would be harmless but adds noise.

## Step 3 - Capture the signing secret

1. After clicking **Add endpoint**, the endpoint detail page shows **Signing secret** with a `Reveal` action.
2. Click **Reveal** and copy the value. Format: `whsec_<random>`.
3. Treat this value as a credential. Do not paste it into chat, the build log, or any committed file.

## Step 4 - Write the secret to the prod environment

On the operator workstation, append (or update) `_credentials/credentials.env`:

```
STRIPE_WEBHOOK_SECRET_RESEMBLIO_TEST=whsec_<value>
```

On `resemblio-prod-01`, update the systemd EnvironmentFile:

```
sudo install -m 0640 -o root -g resemblio /dev/stdin /etc/resemblio/api.env <<'EOF'
... existing vars ...
STRIPE_WEBHOOK_SECRET_RESEMBLIO_TEST=whsec_<value>
EOF
```

(Or use the operator's preferred secret-management flow; the file must be readable by the `resemblio` system user and not world-readable.)

Also confirm these companion vars are present in the same file:

- `STRIPE_RESTRICTED_KEY_RESEMBLIO_TEST=rk_test_<value>`
- `RESEMBLIO_TOPUP_SUCCESS_URL=https://resemblio.com/dashboard/credit?topup=success`
- `RESEMBLIO_TOPUP_CANCEL_URL=https://resemblio.com/dashboard/credit?topup=cancel`
- `RESEND_API_KEY=re_<value>`
- `RESEMBLIO_RESEND_FROM_EMAIL=Resemblio <hello@resemblio.com>` (or whatever verified sender on resemblio.com is in use)
- `RESEMBLIO_KEY_PEPPER=<existing 32+ char value>` (do not change during S2)

## Step 5 - Restart the API

```
sudo systemctl restart resemblio-api
sudo systemctl status resemblio-api --no-pager
sudo journalctl -u resemblio-api -n 50 --no-pager
```

Expect:

- Status: `active (running)`
- Logs include a startup line confirming Stripe TEST mode validated
- No `RuntimeError: STRIPE_WEBHOOK_SECRET_RESEMBLIO_TEST is required` (that means Step 4 did not land)
- No `RuntimeError: ... contains Stripe LIVE key material` (that means a `_LIVE` value sneaked into a TEST slot; remove it)

## Step 6 - Send a test webhook ping

In the Stripe Dashboard, from the endpoint detail page:

1. Click **Send test webhook**.
2. Select event type **checkout.session.completed**.
3. Click **Send test webhook**.

Expected:

- The "Webhook attempts" panel shows a **202 Accepted** response from the endpoint
- `journalctl -u resemblio-api -f` shows a log line for the received event (no PII, no raw payload)
- Postgres: `SELECT id, status FROM stripe_events_seen ORDER BY created_at DESC LIMIT 1;` returns a row with the Stripe event id and `status='processed'`
- No `credit_ledger` row is written (the test event has no real `topup_sessions` row to match, so the handler acks but writes no credit; this is correct)

If the response is 400 with `Stripe-Signature` mentioned in the log, the secret in Step 4 does not match. Revisit, restart, retry.

If the response is 5xx, capture the journal output and stop the rollout per `S2_ROLLBACK.md` Step 1.

## Step 7 - Verify end-to-end with a real test Checkout

1. From the operator workstation, hit the live API:

```
curl -X POST https://api.resemblio.com/v1/credit/topup \
  -H "Authorization: Bearer <test-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"amount_cents": 2000}'
```

Expect `200 OK` with a `checkout_url`.

2. Open the `checkout_url` in a browser. Stripe's test card `4242 4242 4242 4242` (any future expiry, any CVC, any postal) completes the payment.

3. After redirect to the success URL, verify:
   - Stripe Dashboard -> the endpoint -> Attempts shows a 202 for `checkout.session.completed`
   - Postgres: a new `topup_sessions` row with `status='completed'`
   - Postgres: a new `credit_ledger` row with `entry_type='topup'`, `amount_cents=2000`, the right `user_id`
   - Resend Dashboard shows the "Your top-up of $20 has cleared" email delivered
   - `GET /v1/credit/balance` for that user reflects the new balance

4. Trigger Stripe's redeliver on the same event from the dashboard. Verify:
   - The handler returns 202
   - No second `credit_ledger` row is written (idempotency via `stripe_events_seen`)

## Step 8 - Document the secret rotation date

Update `projects/Resemblio/STATUS.md` (or the next operator handoff) with:

- Date the webhook endpoint was registered
- Date the signing secret was captured
- Reminder: rotate the signing secret on the 90-day cadence per `Resemblio_INFRA.md` (or per workspace `context/Infrastructure.md`, whichever is stricter)

## Failure modes and fixes

| Symptom | Likely cause | Fix |
|---|---|---|
| Stripe shows 400 with "Invalid signature" | Wrong or missing `STRIPE_WEBHOOK_SECRET_RESEMBLIO_TEST` | Re-copy from Stripe, update env, restart service |
| Stripe shows 500 | Unhandled exception in handler | Capture journal output, page operator, consider rollback |
| 202 returned but no ledger row | Event was a `checkout.session.completed` for a non-credit-topup purpose, or `payment_status != "paid"` | Inspect `metadata` and `payment_status` on the event in Stripe; if `purpose=credit_topup` and `payment_status=paid` but no ledger row, this is a bug - rollback |
| Duplicate ledger row for one event | Idempotency table not seeing the event id | Check `stripe_events_seen` for the id; if absent, the migration did not apply; if present with `status='processing'`, the handler crashed mid-flight - investigate per `S2_ROLLBACK.md` |
| Service refuses to start, logs `_LIVE` error | A live-mode key landed in a TEST env var slot | Remove the `_LIVE` value from `/etc/resemblio/api.env`; never put LIVE values in TEST slots |

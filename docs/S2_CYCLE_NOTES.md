# S2 Payment Code - Cycle Notes

Append-only log of fix bundles applied to the S2 webhook + credit ledger path.
Each entry should name the cycle, date, and the issues addressed.

## Cycle 7 (2026-05-26)

Fixed Codex cycle-6 cross-review findings:

- **BLOCKER 1 (strict payment_status gate):** `_process_checkout_completed` in
  `app/routes/webhooks.py` now requires `payment_status == "paid"` exactly. A
  missing field is treated as not-paid and refused. Helper payloads in
  `tests/test_concurrency.py`, `tests/test_stripe_webhook_topup.py`,
  `tests/test_stripe_webhook_signature.py`, and `tests/test_s2_acceptance.py`
  were updated to include `"payment_status": "paid"` on positive-path payloads.
  Added regression test
  `test_webhook_missing_payment_status_does_not_credit`.
- **BLOCKER 2 (stale-processing-claim lease):** Added `claimed_at` column to
  `StripeEventSeen` via migration `0006_stripe_event_claim_lease.py`. Named
  constant `_STALE_PROCESSING_LEASE_SECONDS = 300` lives in `webhooks.py`. The
  `_claim_event` ON CONFLICT WHERE clause now also re-claims rows where
  `status='processing' AND claimed_at < now() - 5 min`. Stranding scenario
  (handler crash + `_mark_event_failed` itself failing) self-heals on the next
  Stripe redelivery. Added regression test
  `test_webhook_stale_processing_claim_recovered`.
- **MAJOR (narrowed IntegrityError handler):** Removed the broad
  `except IntegrityError -> mark processed` branch in the webhook outer try.
  With the atomic TopupSession UPDATE in place, the race loser returns False
  cleanly (rowcount == 0); any IntegrityError reaching the outer handler is now
  an unexpected bug and propagates through `_mark_event_failed` so Stripe
  redelivers. Removed the now-unused `_finalize_event_processed_after_rollback`
  helper.
- **MINOR (migration 0005 comment):** Updated the docstring in
  `migrations/versions/0005_stripe_event_status.py` to reflect the cycle-6
  switch from delete-row to status='failed' on handler failure.

Tests authored: 2 new cases (missing-payment-status, stale-claim recovery)
appended to `tests/test_concurrency.py`. Test execution unverified in this
session: pytest invocation was sandbox-denied for both Bash and PowerShell;
parent agent must run `pytest tests/test_concurrency.py` and the full suite to
confirm green.

## Cycle 8 (2026-05-26)

Fixed Codex cycle-7 cross-review findings:

- **BLOCKER (fresh-processing redelivery consumed the only recovery retry):**
  The `in_flight` branch in `stripe_webhook` previously returned 200, which
  Stripe interprets as "done" and stops retrying. If the original in-flight
  worker died before either committing the credit or writing a 'failed'
  marker, the only recovery path (stale-claim re-claim after the 300s lease
  expires) never ran because no future redelivery arrived. Fixed: in_flight
  now returns 409 Conflict so Stripe keeps the retry schedule active; once
  the lease expires or the original worker writes a 'failed' marker, the
  next retry re-claims and completes. Trade is a small extra retry under
  happy-path concurrency for guaranteed recovery from the strand-the-customer
  failure mode. Added regression test
  `test_webhook_in_flight_within_lease_returns_retryable`.
- **MAJOR (broad IntegrityError swallow in `_claim_event`):** The broad
  `except IntegrityError` around the ON CONFLICT insert was dead code for
  normal event-id races (ON CONFLICT handles those atomically). It only
  fired on UNEXPECTED constraint or schema failures, where it silently
  swallowed the error, found no row on readback, and returned 200 with no
  credit and no marker - dropping the event. Fixed: removed the except
  entirely. Any IntegrityError now propagates to the outer handler in
  `stripe_webhook`, which writes a 'failed' marker (best-effort) and
  re-raises so Stripe receives a 5xx and redelivers. Removed the now-unused
  `IntegrityError` import. Added regression test
  `test_claim_event_integrity_error_no_row_raises`.

Existing test updated: `test_concurrent_identical_webhooks_credit_once` now
allows one 200 + one 409 (in addition to the prior two-200 outcome). The
exactly-once-credit invariant remains the assertion that matters; the status
mix depends on which thread wins the claim race.

Tests authored: 2 new cases appended to `tests/test_concurrency.py`. Verified
locally: `pytest tests/test_concurrency.py` → 12 passed; full `pytest` →
71 passed, 1 skipped.

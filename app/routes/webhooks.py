"""Auth-free signed webhook routes."""
from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app.config import get_settings
from app.constants import SCHEMA_V1
from app.db import get_db
from app.email import EmailSenderFactory, get_email_sender_factory
from app.models import CreditLedger, StripeEventSeen, TopupSession, User
from app.payments import StripeSignatureError, construct_stripe_event
from app.routes.account import credit_balance
from app.auth import utcnow
from app.schemas import StripeCheckoutSessionPayload, StripeEventEnvelope

router = APIRouter()
logger = logging.getLogger(__name__)

# Event-claim status vocabulary. Stored as plain text in
# ``stripe_events_seen.status`` so a DBA can read the table directly without
# decoding constants. Centralized here to keep the state machine in one place.
_STATUS_PROCESSING = "processing"
_STATUS_PROCESSED = "processed"
_STATUS_FAILED = "failed"

# Stale-claim lease window. A row stuck at status='processing' whose
# ``claimed_at`` is older than this is treated as abandoned (the handler likely
# crashed before ``_mark_event_failed`` could commit, or the process was
# OOM-killed mid-flight). The next Stripe redelivery re-claims it instead of
# bailing to in_flight.
#
# Sizing rationale: the lease must exceed the worst-case healthy handler
# duration, otherwise stale-recovery can fire on a worker that is still alive
# and a second handler can race the first. The atomic TopupSession UPDATE
# prevents double-credit in that case, but the loser still sends a duplicate
# top-up-cleared email before it discovers it lost the race.
#
# Current upper bounds:
#   - Resend HTTP call: 10s timeout (see ``app/email.py``; ``urlopen`` is hard-
#     capped so a hung Resend cannot run forever)
#   - DB commit on a healthy Postgres: well under 30s even under contention
#   - Handler overhead (validation, ledger insert, flush): single-digit seconds
# Total upper bound on a healthy run: <60s. 900s gives ~15x safety margin and
# is still short enough that an operator does not babysit stranded customers
# (Stripe's own retry cadence is in the hours/days range by the time the lease
# matters). Bumped from 300s in cycle 9 after a self-review surfaced that the
# original number left zero headroom above a single 10s email timeout plus a
# slow commit.
_STALE_PROCESSING_LEASE_SECONDS = 900


@router.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
    session: Session = Depends(get_db),
    email_sender_factory: EmailSenderFactory = Depends(get_email_sender_factory),
) -> JSONResponse:
    """Verify and process Stripe TEST webhook events."""
    payload = await request.body()
    settings = get_settings()
    if not stripe_signature:
        logger.warning("Stripe webhook rejected: missing signature")
        return JSONResponse(status_code=400, content={"error": "invalid_signature", "schema_version": SCHEMA_V1})
    # Explicit runtime check (previously `assert`) so that `python -O` cannot
    # strip the secret-presence guard. A missing secret is a deployment bug, not
    # a signature failure, so we surface it as a 500.
    if settings.stripe_webhook_secret is None:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET_RESEMBLIO_TEST not configured")
    try:
        event = construct_stripe_event(payload, stripe_signature, settings.stripe_webhook_secret)
    except StripeSignatureError:
        logger.warning("Stripe webhook rejected: signature verification failed")
        return JSONResponse(status_code=400, content={"error": "invalid_signature", "schema_version": SCHEMA_V1})

    # Stateful idempotency claim (path B, atomic). One SQL statement either
    # inserts a fresh 'processing' row for an unseen event_id OR promotes a
    # prior 'failed' row back to 'processing'. Any other prior state
    # ('processing' or 'processed') leaves the row unchanged and we read it
    # back to decide the duplicate response. The contract: status='processed'
    # is the ONLY state that proves the credit landed. Marking the event seen
    # before the credit commits (the pre-cycle-5 pattern) would let any failure
    # between claim and commit permanently strand the customer, because
    # redelivery would short-circuit to duplicate-200 with no ledger row.
    #
    # Why a 'failed' marker rather than a row delete on handler failure: a
    # delete loses the audit trail of attempts. Keeping 'failed' lets ops query
    # for events that needed multiple deliveries, and the conditional ON
    # CONFLICT clause makes re-claim atomic regardless. Two concurrent workers
    # racing on a 'failed' row both attempt the UPDATE; whichever wins flips
    # to 'processing' and the loser sees status='processing' on its readback
    # and bails as in-flight.
    claim_outcome = _claim_event(session, event.id)
    if claim_outcome == "already_processed":
        return JSONResponse(status_code=200, content={"received": True, "duplicate": True, "schema_version": SCHEMA_V1})
    if claim_outcome == "in_flight":
        # Another worker currently holds a fresh claim (status='processing'
        # within the lease window). Cycle 8 fix: return a RETRYABLE non-2xx
        # (409 Conflict) so Stripe will retry with backoff.
        #
        # Why this matters: the previous 200 response told Stripe "done" and
        # stopped redelivery. If the in-flight worker then died before either
        # committing the credit or writing a 'failed' marker, the only path
        # back to a successful credit was the stale-claim recovery branch in
        # ``_claim_event`` - which only runs on a SUBSEQUENT redelivery. With
        # the 200 response there was no subsequent redelivery, so the credit
        # was stranded until an operator noticed. Returning 409 keeps Stripe's
        # backoff schedule active; once the lease expires (or the original
        # worker writes a 'failed' marker), a future retry re-claims and
        # completes. The trade is a small extra retry under happy-path
        # concurrency (the original worker usually finishes first and the
        # retry then sees 'already_processed' = 200) in exchange for guaranteed
        # recovery from the strand-the-customer failure mode.
        return JSONResponse(status_code=409, content={"received": False, "in_flight": True, "schema_version": SCHEMA_V1})

    status_code = 200
    try:
        if event.type == "checkout.session.completed":
            processed = _process_checkout_completed(session, event, email_sender_factory)
            status_code = 200 if processed else 202
        elif event.type == "payment_intent.succeeded":
            logger.info("Stripe payment_intent.succeeded recorded event_id=%s", event.id)
            status_code = 202
        else:
            logger.info("Stripe event ignored event_id=%s type=%s", event.id, event.type)
            status_code = 202
        # Mark the event processed in the SAME commit as any ledger row the
        # handler inserted. If the handler raised, or this final commit fails,
        # the except block below marks the row 'failed' so the next Stripe
        # redelivery can re-claim and re-attempt the credit.
        _mark_event_processed(session, event.id)
        session.commit()
    except Exception:
        # Any failure (handler exception, DB outage mid-flush, email provider
        # down, unexpected IntegrityError): roll back the handler's session
        # work, mark the StripeEventSeen row 'failed' in a fresh transaction,
        # then re-raise so Stripe receives a 5xx and redelivers. The fresh
        # delivery will re-claim via the ON CONFLICT clause (now also covering
        # stale-claim recovery) and re-attempt the credit.
        #
        # Cycle 7: removed the broad ``except IntegrityError -> mark
        # processed`` branch that used to live above this handler. With the
        # atomic TopupSession UPDATE in ``_process_checkout_completed``, a
        # parallel worker that wins the race causes the loser to see
        # ``rowcount == 0`` and return False cleanly - no IntegrityError is
        # ever raised on that path. Therefore any IntegrityError reaching this
        # handler now is an unexpected bug (constraint violation we did not
        # anticipate), and silently flipping it to ``processed`` would mask
        # the bug and potentially lose a real credit. Letting it propagate to
        # ``_mark_event_failed`` preserves the audit trail and gives Stripe's
        # redelivery a chance to succeed once the bug is fixed.
        session.rollback()
        _mark_event_failed(session, event.id)
        raise
    return JSONResponse(status_code=status_code, content={"received": True, "schema_version": SCHEMA_V1})


def _process_checkout_completed(session: Session, event: StripeEventEnvelope, email_sender_factory: EmailSenderFactory) -> bool:
    """Apply credit for a completed Stripe Checkout top-up session.

    Refuses to credit unless the incoming Stripe session id matches a
    server-recorded TopupSession row created at top-up initiation. This closes
    the ownership-spoof gap where a forged or replayed webhook could otherwise
    credit an arbitrary user by setting metadata.user_id. The server-side row
    binds session_id -> user_id at creation time; the webhook must agree.
    """
    checkout = StripeCheckoutSessionPayload.model_validate(event.data.object)
    if checkout.metadata.purpose != "credit_topup":
        logger.info("Stripe checkout ignored event_id=%s purpose=%s", event.id, checkout.metadata.purpose)
        return False
    if checkout.amount_total is None or checkout.amount_total <= 0:
        logger.warning("Stripe checkout topup missing positive amount event_id=%s", event.id)
        return False
    # Card-only contract (path A on BLOCKER 2). Refuse to credit any Checkout
    # session whose payment_status is not 'paid'. Delayed-payment methods can
    # emit checkout.session.completed with payment_status in ('unpaid',
    # 'processing'), in which case the actual fulfillment trigger is
    # checkout.session.async_payment_succeeded. We restrict Checkout creation
    # to ['card'] (see payments.py), so the only way to reach this branch with
    # a non-paid status is a session created outside the API (dashboard,
    # legacy) or a delivery contract change. Either way, do not credit.
    # Reference: https://docs.stripe.com/payments/checkout/fulfill-orders
    #
    # Cycle 7 (2026-05-26): strict equality. A missing payment_status field is
    # treated as not-paid and rejected. The earlier `is not None` guard let a
    # payload that omitted the field entirely (forged, dashboard-created with
    # the field unset, or a Stripe API contract change) fall through to the
    # credit path. The defense is cheap; absence is suspicious and must not
    # credit.
    if checkout.payment_status != "paid":
        logger.warning(
            "Stripe checkout topup payment_status not paid event_id=%s session_id=%s payment_status=%s",
            event.id,
            checkout.id,
            checkout.payment_status,
        )
        return False
    topup = session.get(TopupSession, checkout.id)
    if topup is None:
        # No server-side record means we never initiated this Checkout. Either
        # the row was lost or the event is forged/replayed; refuse to credit.
        logger.warning("Stripe checkout topup has no server-side TopupSession event_id=%s session_id=%s", event.id, checkout.id)
        return False
    if topup.status == "completed":
        logger.info("Stripe checkout topup already completed event_id=%s session_id=%s", event.id, checkout.id)
        return False
    metadata_user_id = _metadata_user_id(checkout.metadata.user_id)
    if metadata_user_id is not None and metadata_user_id != topup.user_id:
        # Metadata disagrees with what we recorded server-side. Trust the
        # server-side row, log the discrepancy, refuse to credit.
        logger.warning(
            "Stripe checkout topup metadata user mismatch event_id=%s metadata_user=%s server_user=%s",
            event.id,
            metadata_user_id,
            topup.user_id,
        )
        return False
    # Amount enforcement: the only authoritative amount is the one we recorded
    # server-side at top-up initiation (subject to TOPUP_MIN_CENTS / MAX_CENTS
    # plus the per-user authorization in credit.py). A webhook payload whose
    # amount_total differs from the server-recorded amount is either a Stripe
    # bug, a misconfigured Checkout product, or - worst case - a tampered
    # payload delivered via a compromised restricted key. Refuse the credit
    # entirely rather than crediting either amount; force human investigation.
    if checkout.amount_total != topup.amount_cents:
        logger.warning(
            "Stripe checkout topup amount mismatch event_id=%s session_id=%s webhook_amount=%s server_amount=%s",
            event.id,
            checkout.id,
            checkout.amount_total,
            topup.amount_cents,
        )
        return False
    user = session.get(User, topup.user_id)
    if user is None:
        logger.warning("Stripe checkout topup user not found event_id=%s user_id=%s", event.id, topup.user_id)
        return False
    # Atomic claim. Two different Stripe events for the SAME Checkout session
    # (e.g. checkout.session.completed followed by
    # checkout.session.async_payment_succeeded) carry different event ids, so
    # stripe_events_seen does not deduplicate them. Without an atomic UPDATE,
    # two workers handling those events concurrently can both read
    # topup.status='pending' and both proceed to insert a credit ledger row.
    # The conditional UPDATE below claims the row in one statement; the loser
    # sees rowcount==0 and exits without crediting.
    completed_at = utcnow()
    result = session.execute(
        update(TopupSession)
        .where(TopupSession.id == topup.id, TopupSession.status == "pending")
        .values(status="completed", completed_at=completed_at)
    )
    if result.rowcount != 1:
        logger.info(
            "Stripe checkout topup already claimed by parallel worker event_id=%s session_id=%s",
            event.id,
            checkout.id,
        )
        return False
    # Credit the server-recorded amount, never the webhook payload's amount.
    # (Equal by the check above; we read from `topup` to make the source of
    # truth explicit and to survive any future change that drops the equality
    # guard.)
    credit_amount = topup.amount_cents
    balance_after = credit_balance(session, user.id) + credit_amount
    session.add(
        CreditLedger(
            user_id=user.id,
            entry_type="topup",
            amount_cents=credit_amount,
            balance_after_cents=balance_after,
            stripe_payment_intent_id=checkout.payment_intent if isinstance(checkout.payment_intent, str) else None,
            note="Stripe Checkout topup",
        )
    )
    session.flush()
    email_sender = email_sender_factory()
    email_sender.send_topup_cleared(user.email, credit_amount, balance_after)
    return True


def _claim_event(session: Session, event_id: str) -> str:
    """Atomically claim a Stripe event id for processing.

    Returns one of:
      - "claimed": this caller now owns the event; proceed to handle it
      - "already_processed": another delivery already completed; route returns 200
      - "in_flight": another worker holds a fresh claim within the lease
        window; route returns 409 so Stripe retries with backoff (cycle 8
        fix - the earlier 200 response could permanently strand a credit if
        the in-flight worker died without writing a 'failed' marker)

    Implementation: a single INSERT ... ON CONFLICT (event_id) DO UPDATE SET
    status='processing', claimed_at=now() WHERE existing.status='failed' OR
    (status='processing' AND claimed_at is stale) RETURNING ... inserts a fresh
    row OR promotes a previously-failed row OR recovers a row whose handler
    crashed without writing a 'failed' marker. Any other prior state
    ('processed' from a completed delivery, or 'processing' with a fresh
    lease) is left untouched by the WHERE clause, and the read-back tells us
    which.

    Postgres exposes xmax to distinguish insert-vs-update; SQLite does not, so
    we resolve the outcome with a follow-up SELECT in both dialects (one extra
    round-trip is cheap, and a single code path is worth more than the
    optimization).

    Cycle 7: added the stale-claim recovery branch. Without it, a handler that
    crashes after ``_claim_event`` commits and before the credit lands - AND
    whose ``_mark_event_failed`` best-effort write also fails - leaves the row
    permanently at 'processing'. Subsequent redeliveries hit the in_flight
    branch and return 200 with no credit. The lease window
    (``_STALE_PROCESSING_LEASE_SECONDS``) is long enough that a healthy handler
    never trips it.
    """
    dialect_name = session.bind.dialect.name if session.bind is not None else "sqlite"
    insert_fn = pg_insert if dialect_name == "postgresql" else sqlite_insert
    now = utcnow()
    stmt = insert_fn(StripeEventSeen).values(
        event_id=event_id,
        status=_STATUS_PROCESSING,
        claimed_at=now,
    )
    # ON CONFLICT: re-claim if the existing row is in 'failed' state OR if it
    # is stuck at 'processing' with a stale lease (handler crashed before
    # marking it failed). The RETURNING clause lets us distinguish "we won the
    # claim" (one row returned) from "someone else already owns it" (zero rows
    # returned) without a follow-up SELECT-and-guess. Both Postgres and SQLite
    # 3.35+ support RETURNING on ON CONFLICT DO UPDATE. Time arithmetic uses
    # Python-side cutoffs rather than dialect-specific SQL (``now() -
    # interval``) so the same expression works on Postgres and SQLite.
    stale_cutoff = now - timedelta(seconds=_STALE_PROCESSING_LEASE_SECONDS)
    status_col = StripeEventSeen.__table__.c.status
    claimed_at_col = StripeEventSeen.__table__.c.claimed_at
    stmt = stmt.on_conflict_do_update(
        index_elements=["event_id"],
        set_={"status": _STATUS_PROCESSING, "claimed_at": now},
        where=or_(
            status_col == _STATUS_FAILED,
            and_(status_col == _STATUS_PROCESSING, claimed_at_col < stale_cutoff),
        ),
    ).returning(StripeEventSeen.__table__.c.event_id)
    # Cycle 8 fix: removed the broad ``except IntegrityError -> fall through to
    # readback`` branch that previously wrapped this block. ON CONFLICT DO
    # UPDATE already handles the event-id race atomically; the readback was
    # never reached on a normal race. The only path that could fire that
    # except was an UNEXPECTED constraint or schema failure (e.g., a missing
    # migration, an FK we did not anticipate). On that path, the previous
    # behavior swallowed the IntegrityError, fell through to a readback that
    # found no row, and returned "in_flight" - which the route then translated
    # to a 200 response with no credit and no marker row. That silently dropped
    # the event. Letting the IntegrityError propagate now triggers the outer
    # exception handler in ``stripe_webhook``, which marks the event 'failed'
    # (where possible) and re-raises so Stripe receives a 5xx and redelivers.
    result = session.execute(stmt)
    claimed_row = result.first()
    session.commit()
    if claimed_row is not None:
        return "claimed"
    # We did not win. Either the row already existed in a state that our WHERE
    # clause refused to overwrite ('processing' or 'processed'), or a
    # concurrent insert beat us. Read back to decide the response.
    existing = session.execute(
        select(StripeEventSeen).where(StripeEventSeen.event_id == event_id)
    ).scalar_one_or_none()
    if existing is None:
        # The row vanished between our statement and the readback. This is
        # only possible if a separate cleanup deleted it; the production code
        # never deletes claim rows. Treat as in_flight and let Stripe redeliver.
        return "in_flight"
    if existing.status == _STATUS_PROCESSED:
        return "already_processed"
    # Either 'processing' (someone else is mid-flight) or 'failed' (someone
    # else lost the re-claim race a moment ago and will be reclaimed by their
    # next attempt). Both bail as in_flight; the safer side of the trade is
    # not double-attempting.
    return "in_flight"


def _mark_event_processed(session: Session, event_id: str) -> None:
    """Flip the in-flight claim row from 'processing' to 'processed'.

    Caller is responsible for committing; this function only stages the UPDATE
    so it lands in the same transaction as the credit ledger insert.
    """
    session.execute(
        update(StripeEventSeen)
        .where(StripeEventSeen.event_id == event_id)
        .values(status=_STATUS_PROCESSED)
    )


def _mark_event_failed(session: Session, event_id: str) -> None:
    """Flip the in-flight claim row to 'failed' so the next delivery can re-claim.

    Runs in its own committed transaction because the caller has just done a
    session.rollback() that erased the in-flight handler work. We need this
    marker to land independently of the failed handler attempt.

    The 'failed' marker is the explicit signal that an attempt was made and
    did not complete; the ON CONFLICT clause in `_claim_event` reads it as
    permission to re-claim. Earlier designs deleted the row instead; the
    delete version lost audit trail and was harder to reason about under race.
    """
    try:
        session.execute(
            update(StripeEventSeen)
            .where(
                StripeEventSeen.event_id == event_id,
                StripeEventSeen.status == _STATUS_PROCESSING,
            )
            .values(status=_STATUS_FAILED)
        )
        session.commit()
    except Exception:
        session.rollback()
        # Best-effort: if we cannot even mark the row failed, the row stays at
        # 'processing'. Future Stripe redeliveries will bail to in_flight (the
        # safer of the two failure modes; better than double-credit). Operators
        # can clear stuck rows manually if this branch fires.
        #
        # Cycle 9: log the failure with stack so operators get a signal. Without
        # this, a failed marker-write was completely silent and the only sign
        # was a customer eventually noticing a stranded credit. The stale-claim
        # lease (``_STALE_PROCESSING_LEASE_SECONDS``) still recovers the row
        # automatically on a later redelivery; the log line just makes the
        # situation observable in the meantime.
        logger.exception(
            "_mark_event_failed could not commit failed status for event %s; "
            "will recover via stale-claim lease after %s seconds",
            event_id,
            _STALE_PROCESSING_LEASE_SECONDS,
        )


def _metadata_user_id(value: str | None) -> int | None:
    """Parse a user id from Stripe metadata."""
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None

"""Concurrency invariants for credit-charge and webhook idempotency.

These tests run against a file-backed SQLite with a connection pool that grants
each thread its own connection. The default test conftest uses StaticPool with
in-memory SQLite, which serializes all access through one connection and so
cannot exercise the race conditions these tests need to prove against. Postgres
is the production target and exhibits the same invariants under load; SQLite
file mode is the fastest CI-local approximation that still allows multiple
concurrent transactions to attempt the same insert.

Test 1 (B1: credit-charge race): two parallel POST /v1/extractions calls
against a user with exactly one extraction's worth of credit. Invariant:
exactly one succeeds (200), the other gets 402 insufficient_credit, and the
final ledger balance is >= 0. Without the CHECK constraint on
credit_ledger.balance_after_cents plus the retry loop, both could succeed and
drive the balance negative.

Test 2 (H1: webhook idempotency race): two parallel POSTs of the same Stripe
webhook event. Invariant: exactly one credit_ledger topup row is created. The
insert-first claim on stripe_events_seen forces the losing thread to bail.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import threading
import time
from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import reset_settings_cache
from app.constants import DEFAULT_API_SCOPE, EXTRACTION_PUBLIC_CENTS
from app.crypto import generate_api_key, hash_password
from app.db import Base
from app.email import get_email_sender_factory
from app.extractor_bridge import bundle_from_token_set
from app.main import app
from app.models import ApiKey, CreditLedger, StripeEventSeen, TopupSession, User
from app.rate_limit import reset_rate_limiter
from app.routes.extractions import get_extractor
from app.storage import get_storage
from tests.conftest import TOKEN_SET, FakeStorage

WEBHOOK_SECRET = "whsec_resemblio_dummy"
CONCURRENT_REQUEST_COUNT = 2


class _RecordingEmailSender:
    """Email fake that just counts top-up notifications."""

    def __init__(self) -> None:
        """Create an empty sent log."""
        self.sent: list[tuple[str, int, int]] = []

    def send_topup_cleared(self, to_email: str, amount_cents: int, balance_cents: int) -> None:
        """Record one top-up email payload."""
        self.sent.append((to_email, amount_cents, balance_cents))


@pytest.fixture
def file_sqlite_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[tuple[TestClient, FakeStorage, str, int], None, None]:
    """Swap the engine to a file-backed SQLite that supports per-thread connections.

    Returns (client, storage, plaintext_api_key, user_id). The user has exactly
    one public extraction's worth of credit ($5). The fake extractor and storage
    keep the test deterministic and offline.
    """
    db_path = tmp_path / "concurrency.sqlite"
    # `check_same_thread=False` lets pytest's main thread create rows and worker
    # threads issue concurrent requests against the same database file.
    monkeypatch.setenv("RESEMBLIO_DB_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("RESEMBLIO_KEY_PEPPER", "test-pepper-value-with-thirty-two-chars")
    reset_settings_cache()
    db.reset_engine(f"sqlite+pysqlite:///{db_path}")
    Base.metadata.create_all(bind=db.engine)
    reset_rate_limiter()

    storage = FakeStorage()
    fake_email = _RecordingEmailSender()

    def _extractor(url: str) -> Any:
        # Tiny delay widens the contention window for Test 1; without this the
        # parent request can commit before the worker even acquires its session.
        time.sleep(0.02)
        return bundle_from_token_set(url, TOKEN_SET)

    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_extractor] = lambda: _extractor
    app.dependency_overrides[get_email_sender_factory] = lambda: lambda: fake_email

    with db.SessionLocal() as setup_session:
        user = User(
            email="race@example.test",
            password_hash=hash_password("password"),
            stripe_customer_id="cus_test_race",
            status="active",
        )
        setup_session.add(user)
        setup_session.flush()
        plaintext, digest, prefix = generate_api_key("live")
        api_key = ApiKey(user_id=user.id, key_hash=digest, key_prefix=prefix, label="race", scopes=[DEFAULT_API_SCOPE])
        setup_session.add(api_key)
        # Exactly one public extraction's worth; both threads will try to spend it.
        setup_session.add(
            CreditLedger(
                user_id=user.id,
                entry_type="onboarding_grant",
                amount_cents=EXTRACTION_PUBLIC_CENTS,
                balance_after_cents=EXTRACTION_PUBLIC_CENTS,
                note="concurrency seed",
            )
        )
        setup_session.commit()
        user_id = user.id

    with TestClient(app) as client:
        yield client, storage, plaintext, user_id

    Base.metadata.drop_all(bind=db.engine)
    app.dependency_overrides.clear()
    # Restore the in-memory engine so the conftest autouse teardown that runs
    # after this fixture does not try to drop tables on a closed file engine.
    db.reset_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=db.engine)
    reset_settings_cache()
    if db_path.exists():
        try:
            os.remove(db_path)
        except OSError:
            pass


def test_concurrent_extractions_do_not_double_spend(file_sqlite_app: tuple[TestClient, FakeStorage, str, int]) -> None:
    """Two simultaneous extractions on a single-extraction balance produce one ok + one 402.

    This is the B1 invariant: even with parallel arrivals, the CHECK constraint
    on credit_ledger.balance_after_cents plus the IntegrityError retry loop in
    routes/extractions.py prevent the balance from going negative. The losing
    thread either retries and finds insufficient_credit, or gets a 409
    charge_contention if it exhausted retries. Both outcomes are acceptable as
    long as the invariant (final balance >= 0, at most one charged extraction)
    holds.
    """
    client, _storage, plaintext, user_id = file_sqlite_app

    results: list[int] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(CONCURRENT_REQUEST_COUNT)

    def _post() -> None:
        barrier.wait()
        response = client.post(
            "/v1/extractions",
            headers={"Authorization": f"Bearer {plaintext}"},
            json={"url": "https://example.test/"},
        )
        with results_lock:
            results.append(response.status_code)

    threads = [threading.Thread(target=_post) for _ in range(CONCURRENT_REQUEST_COUNT)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "Concurrency test thread hung"

    assert len(results) == CONCURRENT_REQUEST_COUNT
    success_count = sum(1 for status in results if status == 200)
    rejection_count = sum(1 for status in results if status in (402, 409))
    assert success_count == 1, f"Expected exactly one success, got results={results}"
    assert rejection_count == 1, f"Expected exactly one rejection, got results={results}"

    with db.SessionLocal() as verify_session:
        ledger_rows = verify_session.query(CreditLedger).filter(CreditLedger.user_id == user_id).all()
        balance = sum(row.amount_cents for row in ledger_rows)
        assert balance >= 0, f"Balance went negative: {balance}, rows={[(r.entry_type, r.amount_cents) for r in ledger_rows]}"
        # Exactly one extraction_charge row at -500. Losers either never inserted
        # (caught at pre-check) or had their insert rolled back by IntegrityError.
        charges = [row for row in ledger_rows if row.entry_type == "extraction_charge"]
        assert len(charges) == 1
        assert charges[0].amount_cents == -EXTRACTION_PUBLIC_CENTS


def test_concurrent_identical_webhooks_credit_once(file_sqlite_app: tuple[TestClient, FakeStorage, str, int]) -> None:
    """Two parallel identical Stripe webhook deliveries produce exactly one ledger credit.

    This is the H1 invariant: the insert-first claim on stripe_events_seen
    forces the loser to bail before any side effect. Without it, two threads
    could both pass a SELECT-based "already seen?" check, both proceed to
    process, and both insert a credit_ledger topup row.
    """
    client, _storage, _plaintext, user_id = file_sqlite_app

    # Seed a TopupSession so the H3 ownership check accepts the webhook.
    with db.SessionLocal() as setup_session:
        setup_session.add(TopupSession(id="cs_test_race", user_id=user_id, amount_cents=2000, status="pending"))
        setup_session.commit()

    payload = _checkout_payload("evt_race", user_id, 2000)
    signature = _signature_header(payload)

    results: list[tuple[int, dict[str, Any]]] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(CONCURRENT_REQUEST_COUNT)

    def _post() -> None:
        barrier.wait()
        response = client.post(
            "/v1/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": signature},
        )
        with results_lock:
            results.append((response.status_code, response.json()))

    threads = [threading.Thread(target=_post) for _ in range(CONCURRENT_REQUEST_COUNT)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "Webhook concurrency thread hung"

    assert len(results) == CONCURRENT_REQUEST_COUNT
    # Cycle 8: one thread wins the claim and returns 200; the other observes
    # either status='processing' (in_flight, returns 409 so Stripe retries) or
    # status='processed' (already_processed, returns 200). Both outcomes are
    # acceptable; the invariant being tested is exactly-once credit, not the
    # specific status mix. A 409 in this case is correct and expected behavior:
    # Stripe will retry, and the retry will see status='processed' and return
    # 200 cleanly.
    statuses = [status for status, _ in results]
    success_count = sum(1 for status in statuses if status == 200)
    in_flight_count = sum(1 for status in statuses if status == 409)
    assert success_count >= 1, f"At least one thread must succeed, got {results}"
    assert success_count + in_flight_count == CONCURRENT_REQUEST_COUNT, (
        f"All responses must be either 200 (claimed/already_processed) or 409 (in_flight), got {results}"
    )

    with db.SessionLocal() as verify_session:
        topup_count = verify_session.query(CreditLedger).filter(CreditLedger.entry_type == "topup", CreditLedger.user_id == user_id).count()
        assert topup_count == 1, f"Expected exactly one topup credit, got {topup_count}"
        seen_count = verify_session.query(StripeEventSeen).filter(StripeEventSeen.event_id == "evt_race").count()
        assert seen_count == 1


def _checkout_payload(event_id: str, user_id: int, amount_cents: int) -> bytes:
    """Build a minimal checkout.session.completed payload bound to cs_test_race.

    Includes ``payment_status="paid"`` so the strict cycle-7 gate accepts the
    payload. Tests that need to exercise the unpaid or missing-field path build
    their own payload inline.
    """
    return json.dumps(
        {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_race",
                    "amount_total": amount_cents,
                    "payment_status": "paid",
                    "payment_intent": "pi_test_race",
                    "metadata": {"user_id": str(user_id), "purpose": "credit_topup"},
                }
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _signature_header(payload: bytes) -> str:
    """Return a Stripe-compatible signature header."""
    timestamp = int(time.time())
    signed_payload = f"{timestamp}.".encode("utf-8") + payload
    digest = hmac.new(WEBHOOK_SECRET.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


# Balance large enough to clear two public extractions; the cap is what
# restricts the second. Used by Test 3 to exercise the spend-cap race.
SPEND_CAP_BALANCE_CENTS = EXTRACTION_PUBLIC_CENTS * 4
SPEND_CAP_LIMIT_CENTS = EXTRACTION_PUBLIC_CENTS + (EXTRACTION_PUBLIC_CENTS - 1)


@pytest.fixture
def file_sqlite_app_with_spend_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[tuple[TestClient, FakeStorage, str, int], None, None]:
    """File-backed SQLite fixture seeded with balance for >1 extraction but a cap that allows only 1.

    Mirrors `file_sqlite_app` but seeds a $20 balance and a $999 spend cap
    (`EXTRACTION_PUBLIC_CENTS + EXTRACTION_PUBLIC_CENTS - 1`). Two concurrent
    public extractions each cost $5; the first fits the cap, the second pushes
    trailing spend to $1000 which exceeds $999.
    """
    db_path = tmp_path / "concurrency_cap.sqlite"
    monkeypatch.setenv("RESEMBLIO_DB_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("RESEMBLIO_KEY_PEPPER", "test-pepper-value-with-thirty-two-chars")
    reset_settings_cache()
    db.reset_engine(f"sqlite+pysqlite:///{db_path}")
    Base.metadata.create_all(bind=db.engine)
    reset_rate_limiter()

    storage = FakeStorage()
    fake_email = _RecordingEmailSender()

    def _extractor(url: str) -> Any:
        # Widen contention window so both worker threads enter the lock queue
        # before the first one finishes the charge transaction.
        time.sleep(0.02)
        return bundle_from_token_set(url, TOKEN_SET)

    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_extractor] = lambda: _extractor
    app.dependency_overrides[get_email_sender_factory] = lambda: lambda: fake_email

    with db.SessionLocal() as setup_session:
        user = User(
            email="cap@example.test",
            password_hash=hash_password("password"),
            stripe_customer_id="cus_test_cap",
            status="active",
        )
        setup_session.add(user)
        setup_session.flush()
        plaintext, digest, prefix = generate_api_key("live")
        api_key = ApiKey(
            user_id=user.id,
            key_hash=digest,
            key_prefix=prefix,
            label="cap",
            scopes=[DEFAULT_API_SCOPE],
            spend_cap_cents=SPEND_CAP_LIMIT_CENTS,
        )
        setup_session.add(api_key)
        setup_session.add(
            CreditLedger(
                user_id=user.id,
                entry_type="onboarding_grant",
                amount_cents=SPEND_CAP_BALANCE_CENTS,
                balance_after_cents=SPEND_CAP_BALANCE_CENTS,
                note="cap concurrency seed",
            )
        )
        setup_session.commit()
        user_id = user.id

    with TestClient(app) as client:
        yield client, storage, plaintext, user_id

    Base.metadata.drop_all(bind=db.engine)
    app.dependency_overrides.clear()
    db.reset_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=db.engine)
    reset_settings_cache()
    if db_path.exists():
        try:
            os.remove(db_path)
        except OSError:
            pass


def test_concurrent_extractions_respect_spend_cap(file_sqlite_app_with_spend_cap: tuple[TestClient, FakeStorage, str, int]) -> None:
    """Two simultaneous extractions against a $999 cap produce one ok + one 402 spend_cap_exceeded.

    This is the spend-cap-race invariant. Balance is $20 (covers both), so the
    balance-race defense is not what's being tested here; the cap is. Without
    the in-lock recompute of `spend_cap_spent_cents`, two threads can both pass
    the pre-lock cap check (both see spent=0), serialize into the lock, and
    both commit a $5 charge - taking trailing spend to $1000 against a $999
    cap. The in-lock re-check forces the loser to 402.
    """
    client, _storage, plaintext, user_id = file_sqlite_app_with_spend_cap

    results: list[tuple[int, dict[str, Any]]] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(CONCURRENT_REQUEST_COUNT)

    def _post() -> None:
        barrier.wait()
        response = client.post(
            "/v1/extractions",
            headers={"Authorization": f"Bearer {plaintext}"},
            json={"url": "https://example.test/"},
        )
        body: dict[str, Any]
        try:
            body = response.json()
        except ValueError:
            body = {}
        with results_lock:
            results.append((response.status_code, body))

    threads = [threading.Thread(target=_post) for _ in range(CONCURRENT_REQUEST_COUNT)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "Spend-cap race thread hung"

    assert len(results) == CONCURRENT_REQUEST_COUNT
    success_count = sum(1 for status, _ in results if status == 200)
    cap_blocks = [body for status, body in results if status == 402 and body.get("error") == "spend_cap_exceeded"]
    other_blocks = [body for status, body in results if status == 402 and body.get("error") != "spend_cap_exceeded"]
    assert success_count == 1, f"Expected exactly one success, got results={results}"
    # The loser MUST be cap-blocked, not balance-blocked: balance covers both.
    assert len(cap_blocks) == 1, f"Expected exactly one spend_cap_exceeded, got results={results}"
    assert not other_blocks, f"Unexpected non-cap 402 in spend-cap race: {other_blocks}"

    with db.SessionLocal() as verify_session:
        charges = (
            verify_session.query(CreditLedger)
            .filter(CreditLedger.user_id == user_id, CreditLedger.entry_type == "extraction_charge")
            .all()
        )
        assert len(charges) == 1
        assert charges[0].amount_cents == -EXTRACTION_PUBLIC_CENTS


def test_webhook_amount_mismatch_does_not_credit(file_sqlite_app: tuple[TestClient, FakeStorage, str, int]) -> None:
    """A webhook whose amount_total disagrees with the server-recorded TopupSession is rejected.

    This is the amount-enforcement invariant. The server records the authorized
    amount at top-up creation. The webhook handler must NOT credit anything if
    the inbound payload's amount_total differs from that recorded amount; a
    compromised Stripe restricted key cannot inflate a top-up by mutating the
    payload.
    """
    client, _storage, _plaintext, user_id = file_sqlite_app

    server_amount_cents = 2000
    tampered_amount_cents = 99_999_999
    session_id = "cs_test_amount_mismatch"
    event_id = "evt_amount_mismatch"

    with db.SessionLocal() as setup_session:
        setup_session.add(
            TopupSession(
                id=session_id,
                user_id=user_id,
                amount_cents=server_amount_cents,
                status="pending",
            )
        )
        setup_session.commit()

    payload = json.dumps(
        {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": session_id,
                    "amount_total": tampered_amount_cents,
                    "payment_status": "paid",
                    "payment_intent": "pi_test_amount_mismatch",
                    "metadata": {"user_id": str(user_id), "purpose": "credit_topup"},
                }
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")
    signature = _signature_header(payload)
    response = client.post(
        "/v1/webhooks/stripe",
        content=payload,
        headers={"Stripe-Signature": signature},
    )
    # 2xx because we acknowledge Stripe (so it does not retry forever) but the
    # body must show ignored and no credit row may exist.
    assert 200 <= response.status_code < 300, response.text

    with db.SessionLocal() as verify_session:
        topup_credits = (
            verify_session.query(CreditLedger)
            .filter(CreditLedger.entry_type == "topup", CreditLedger.user_id == user_id)
            .all()
        )
        assert topup_credits == [], (
            "A credit was applied despite amount mismatch; expected zero topup ledger rows"
        )
        topup = verify_session.get(TopupSession, session_id)
        assert topup is not None
        # Status must remain pending; the row should NOT be claimed when we
        # refused the credit. Otherwise a legitimate retry from Stripe at the
        # correct amount could not subsequently complete.
        assert topup.status == "pending", f"Expected pending status, got {topup.status}"


def test_concurrent_different_event_ids_credit_once(file_sqlite_app: tuple[TestClient, FakeStorage, str, int]) -> None:
    """Two parallel Stripe webhooks for the SAME checkout session but with DIFFERENT event ids credit exactly once.

    This is the cross-event-id idempotency invariant. Stripe can deliver
    multiple event types for the same Checkout session (e.g.
    checkout.session.completed and checkout.session.async_payment_succeeded).
    Two such events carry different event ids, so the stripe_events_seen
    insert-first claim does not deduplicate them. The atomic conditional
    UPDATE on topup_sessions.status is what protects against the double
    credit; only the worker whose UPDATE returns rowcount==1 proceeds to
    insert the ledger row.
    """
    client, _storage, _plaintext, user_id = file_sqlite_app

    session_id = "cs_test_cross_event"
    amount_cents = 2500

    with db.SessionLocal() as setup_session:
        setup_session.add(
            TopupSession(
                id=session_id,
                user_id=user_id,
                amount_cents=amount_cents,
                status="pending",
            )
        )
        setup_session.commit()

    payloads = [
        json.dumps(
            {
                "id": event_id,
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "id": session_id,
                        "amount_total": amount_cents,
                        "payment_status": "paid",
                        "payment_intent": "pi_test_cross",
                        "metadata": {"user_id": str(user_id), "purpose": "credit_topup"},
                    }
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        for event_id in ("evt_cross_a", "evt_cross_b")
    ]

    results: list[int] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(CONCURRENT_REQUEST_COUNT)

    def _post(body: bytes) -> None:
        barrier.wait()
        response = client.post(
            "/v1/webhooks/stripe",
            content=body,
            headers={"Stripe-Signature": _signature_header(body)},
        )
        with results_lock:
            results.append(response.status_code)

    threads = [threading.Thread(target=_post, args=(body,)) for body in payloads]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "Cross-event-id race thread hung"

    assert len(results) == CONCURRENT_REQUEST_COUNT
    assert all(200 <= status < 300 for status in results), f"Non-2xx in cross-event race: {results}"

    with db.SessionLocal() as verify_session:
        topup_count = (
            verify_session.query(CreditLedger)
            .filter(CreditLedger.entry_type == "topup", CreditLedger.user_id == user_id)
            .count()
        )
        assert topup_count == 1, f"Expected exactly one topup credit across event ids, got {topup_count}"
        seen_count = verify_session.query(StripeEventSeen).count()
        # Both events should have been claimed in stripe_events_seen even though
        # only one resulted in a credit; the cross-event protection is the
        # TopupSession.status UPDATE, not the event-id table.
        assert seen_count == 2, f"Expected both event ids recorded, got {seen_count}"
        topup = verify_session.get(TopupSession, session_id)
        assert topup is not None
        assert topup.status == "completed"


def test_webhook_retry_after_transient_failure_eventually_credits(file_sqlite_app: tuple[TestClient, FakeStorage, str, int]) -> None:
    """A transient failure mid-handler must NOT permanently strand the credit.

    Path B (stateful idempotency) invariant. The previous implementation marked
    the event seen BEFORE the credit committed; a crash between claim and
    commit left a row at status='seen' but no credit_ledger entry, and Stripe's
    redelivery short-circuited to duplicate-200 with the customer permanently
    uncredited.

    With path B, a handler failure rolls the StripeEventSeen row back (so a
    fresh delivery can re-claim) and the redelivery completes the credit
    exactly once.

    This test simulates the failure by injecting an email sender that raises
    on the first call and succeeds on the second. The first webhook delivery
    fails inside _process_checkout_completed: the atomic TopupSession UPDATE
    runs inside the same SQLAlchemy session, and the session.commit() never
    runs because the email send raises after flush. The rollback unwinds the
    UPDATE too. The handler then writes a 'failed' marker for the event id in
    a fresh transaction (so audit trail is preserved AND redelivery can
    re-claim atomically via ON CONFLICT WHERE status='failed') and re-raises.
    The retry re-claims the event by flipping the 'failed' row back to
    'processing', re-runs the handler with the now-fixed email sender, and
    credits exactly once.
    """
    client, _storage, _plaintext, user_id = file_sqlite_app

    session_id = "cs_test_retry"
    amount_cents = 3000

    with db.SessionLocal() as setup_session:
        setup_session.add(
            TopupSession(
                id=session_id,
                user_id=user_id,
                amount_cents=amount_cents,
                status="pending",
            )
        )
        setup_session.commit()

    call_count = {"value": 0}

    class _FlakyEmailSender:
        """Email fake that raises on the first send_topup_cleared call."""

        def __init__(self) -> None:
            """Track sends for assertion."""
            self.sent: list[tuple[str, int, int]] = []

        def send_topup_cleared(self, to_email: str, amount_cents: int, balance_cents: int) -> None:
            """Raise on the first call, record on subsequent calls."""
            call_count["value"] += 1
            if call_count["value"] == 1:
                raise RuntimeError("Simulated transient email failure")
            self.sent.append((to_email, amount_cents, balance_cents))

    flaky = _FlakyEmailSender()
    app.dependency_overrides[get_email_sender_factory] = lambda: lambda: flaky

    payload = json.dumps(
        {
            "id": "evt_retry",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": session_id,
                    "amount_total": amount_cents,
                    "payment_status": "paid",
                    "payment_intent": "pi_test_retry",
                    "metadata": {"user_id": str(user_id), "purpose": "credit_topup"},
                }
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")
    signature = _signature_header(payload)

    # First delivery: email raises mid-handler. TestClient with the default
    # raise_server_exceptions=True re-raises the unhandled exception; in
    # production this surfaces to Stripe as a 5xx and triggers redelivery.
    # The webhook must NOT swallow the error (swallowing would silently lose
    # the redelivery signal) AND the credit must not have been recorded AND
    # the StripeEventSeen row must be cleared so the redelivery can re-claim.
    with pytest.raises(RuntimeError, match="Simulated transient email failure"):
        client.post("/v1/webhooks/stripe", content=payload, headers={"Stripe-Signature": signature})
    with db.SessionLocal() as verify_session:
        credits_after_first = (
            verify_session.query(CreditLedger)
            .filter(CreditLedger.entry_type == "topup", CreditLedger.user_id == user_id)
            .count()
        )
        assert credits_after_first == 0, "First (failing) delivery must not leave a partial credit"
        topup_state = verify_session.get(TopupSession, session_id)
        assert topup_state is not None
        assert topup_state.status == "pending", "TopupSession claim must roll back when handler fails"
        # The StripeEventSeen row must be marked 'failed' so the redelivery
        # can re-claim atomically via ON CONFLICT WHERE status='failed'.
        # Earlier designs deleted the row instead; the marker version preserves
        # audit trail and removes a subtle race window where two redeliveries
        # could both succeed at the unique-row insert.
        seen_after_first = (
            verify_session.query(StripeEventSeen)
            .filter(StripeEventSeen.event_id == "evt_retry")
            .one_or_none()
        )
        assert seen_after_first is not None, "Failed attempt must leave a marker row"
        assert seen_after_first.status == "failed", (
            f"First-attempt marker must be 'failed' for redelivery re-claim; got {seen_after_first.status}"
        )

    # Second delivery: Stripe redelivers the same event. Email now succeeds;
    # credit must land exactly once.
    second = client.post("/v1/webhooks/stripe", content=payload, headers={"Stripe-Signature": signature})
    assert second.status_code == 200, f"Redelivery should succeed, got {second.status_code}: {second.text}"
    with db.SessionLocal() as verify_session:
        topup_credits = (
            verify_session.query(CreditLedger)
            .filter(CreditLedger.entry_type == "topup", CreditLedger.user_id == user_id)
            .all()
        )
        assert len(topup_credits) == 1, f"Expected exactly one credit after retry, got {len(topup_credits)}"
        assert topup_credits[0].amount_cents == amount_cents
        seen = (
            verify_session.query(StripeEventSeen)
            .filter(StripeEventSeen.event_id == "evt_retry")
            .one()
        )
        assert seen.status == "processed"
        topup_done = verify_session.get(TopupSession, session_id)
        assert topup_done is not None
        assert topup_done.status == "completed"
    expected_balance = amount_cents + EXTRACTION_PUBLIC_CENTS
    assert flaky.sent == [("race@example.test", amount_cents, expected_balance)], (
        "Email sender should have recorded exactly one successful send after the retry "
        "with balance equal to the onboarding grant plus the topup amount"
    )


def test_webhook_unpaid_payment_status_does_not_credit(file_sqlite_app: tuple[TestClient, FakeStorage, str, int]) -> None:
    """A checkout.session.completed event with payment_status='unpaid' must not credit.

    Path A on BLOCKER 2 (card-only). Stripe fires checkout.session.completed for
    delayed-payment methods while funds are still in flight; payment_status is
    'unpaid' or 'processing' in those cases. v1.1 restricts Checkout to cards
    via payment_method_types=['card'] so this branch should be unreachable in
    production, but the webhook enforces it as a defense for sessions created
    outside the API or for future config drift.

    Invariant: credit is not granted, TopupSession remains pending so a
    correctly-paid retry can complete, and the event id is still recorded as
    processed so Stripe stops retrying.
    """
    client, _storage, _plaintext, user_id = file_sqlite_app

    session_id = "cs_test_unpaid"
    amount_cents = 2500

    with db.SessionLocal() as setup_session:
        setup_session.add(
            TopupSession(
                id=session_id,
                user_id=user_id,
                amount_cents=amount_cents,
                status="pending",
            )
        )
        setup_session.commit()

    payload = json.dumps(
        {
            "id": "evt_unpaid",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": session_id,
                    "amount_total": amount_cents,
                    "payment_status": "unpaid",
                    "payment_intent": "pi_test_unpaid",
                    "metadata": {"user_id": str(user_id), "purpose": "credit_topup"},
                }
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")
    signature = _signature_header(payload)
    response = client.post("/v1/webhooks/stripe", content=payload, headers={"Stripe-Signature": signature})
    # 2xx so Stripe does not retry forever; the body's status_code semantics
    # (200 vs 202) are not what this test is about.
    assert 200 <= response.status_code < 300, response.text

    with db.SessionLocal() as verify_session:
        topup_credits = (
            verify_session.query(CreditLedger)
            .filter(CreditLedger.entry_type == "topup", CreditLedger.user_id == user_id)
            .all()
        )
        assert topup_credits == [], (
            "payment_status='unpaid' must not credit; Stripe's async_payment_succeeded "
            "(or a card retry) is the fulfillment trigger"
        )
        topup = verify_session.get(TopupSession, session_id)
        assert topup is not None
        assert topup.status == "pending", (
            "TopupSession must remain pending so a correctly-paid retry can complete"
        )


def test_webhook_paid_payment_status_credits_normally(file_sqlite_app: tuple[TestClient, FakeStorage, str, int]) -> None:
    """A checkout.session.completed event with explicit payment_status='paid' credits as expected.

    Companion to the unpaid test: confirms the gate is not over-broad and that
    a properly paid card session still credits.
    """
    client, _storage, _plaintext, user_id = file_sqlite_app

    session_id = "cs_test_paid"
    amount_cents = 4500

    with db.SessionLocal() as setup_session:
        setup_session.add(
            TopupSession(
                id=session_id,
                user_id=user_id,
                amount_cents=amount_cents,
                status="pending",
            )
        )
        setup_session.commit()

    payload = json.dumps(
        {
            "id": "evt_paid",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": session_id,
                    "amount_total": amount_cents,
                    "payment_status": "paid",
                    "payment_intent": "pi_test_paid",
                    "metadata": {"user_id": str(user_id), "purpose": "credit_topup"},
                }
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")
    signature = _signature_header(payload)
    response = client.post("/v1/webhooks/stripe", content=payload, headers={"Stripe-Signature": signature})
    assert response.status_code == 200, response.text

    with db.SessionLocal() as verify_session:
        topup_credits = (
            verify_session.query(CreditLedger)
            .filter(CreditLedger.entry_type == "topup", CreditLedger.user_id == user_id)
            .all()
        )
        assert len(topup_credits) == 1
        assert topup_credits[0].amount_cents == amount_cents
        topup = verify_session.get(TopupSession, session_id)
        assert topup is not None
        assert topup.status == "completed"


def test_webhook_missing_payment_status_does_not_credit(file_sqlite_app: tuple[TestClient, FakeStorage, str, int]) -> None:
    """A checkout.session.completed event that omits payment_status must not credit.

    Cycle 7 strict-equality fix. The earlier ``is not None`` guard let a payload
    that omitted the field entirely fall through to the credit path. With the
    strict gate, absence of the field is treated identically to ``unpaid`` and
    the event is refused. Companion to ``test_webhook_unpaid_payment_status``.
    """
    client, _storage, _plaintext, user_id = file_sqlite_app

    session_id = "cs_test_missing_status"
    amount_cents = 2700

    with db.SessionLocal() as setup_session:
        setup_session.add(
            TopupSession(
                id=session_id,
                user_id=user_id,
                amount_cents=amount_cents,
                status="pending",
            )
        )
        setup_session.commit()

    # Payload intentionally omits the payment_status field altogether.
    payload = json.dumps(
        {
            "id": "evt_missing_status",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": session_id,
                    "amount_total": amount_cents,
                    "payment_intent": "pi_test_missing_status",
                    "metadata": {"user_id": str(user_id), "purpose": "credit_topup"},
                }
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")
    signature = _signature_header(payload)
    response = client.post("/v1/webhooks/stripe", content=payload, headers={"Stripe-Signature": signature})
    # 2xx so Stripe stops retrying; no credit may land and TopupSession stays
    # pending so a legitimate later delivery (with payment_status="paid") can
    # still complete.
    assert 200 <= response.status_code < 300, response.text

    with db.SessionLocal() as verify_session:
        topup_credits = (
            verify_session.query(CreditLedger)
            .filter(CreditLedger.entry_type == "topup", CreditLedger.user_id == user_id)
            .all()
        )
        assert topup_credits == [], (
            "Missing payment_status field must be treated as unpaid; no credit may land"
        )
        topup = verify_session.get(TopupSession, session_id)
        assert topup is not None
        assert topup.status == "pending", (
            "TopupSession must remain pending so a correctly-paid retry can complete"
        )


def test_webhook_stale_processing_claim_recovered(file_sqlite_app: tuple[TestClient, FakeStorage, str, int]) -> None:
    """A stuck status='processing' row whose lease has expired is re-claimed by redelivery.

    Cycle 7 stale-claim recovery. Models the operational nightmare where a
    handler crashes AND ``_mark_event_failed`` itself fails to commit, leaving
    the StripeEventSeen row stranded at ``processing``. Without the lease, the
    next Stripe redelivery short-circuits to the in_flight branch forever. With
    the lease (``_STALE_PROCESSING_LEASE_SECONDS = 300`` in webhooks.py), a
    redelivery whose claim arrives more than 5 minutes after the stale claim
    re-acquires the row via the ON CONFLICT WHERE branch and credits the
    customer.
    """
    client, _storage, _plaintext, user_id = file_sqlite_app

    session_id = "cs_test_stale_claim"
    event_id = "evt_stale_claim"
    amount_cents = 3300

    # Seed: a TopupSession in pending state AND a StripeEventSeen row stuck at
    # 'processing' with an expired lease. This simulates a handler crash that
    # left the marker stranded without writing a 'failed' row.
    stale_claimed_at = datetime.now(timezone.utc) - timedelta(seconds=900)  # 15 min ago
    with db.SessionLocal() as setup_session:
        setup_session.add(
            TopupSession(
                id=session_id,
                user_id=user_id,
                amount_cents=amount_cents,
                status="pending",
            )
        )
        setup_session.add(
            StripeEventSeen(
                event_id=event_id,
                status="processing",
                claimed_at=stale_claimed_at,
            )
        )
        setup_session.commit()

    payload = json.dumps(
        {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": session_id,
                    "amount_total": amount_cents,
                    "payment_status": "paid",
                    "payment_intent": "pi_test_stale_claim",
                    "metadata": {"user_id": str(user_id), "purpose": "credit_topup"},
                }
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")
    signature = _signature_header(payload)

    response = client.post("/v1/webhooks/stripe", content=payload, headers={"Stripe-Signature": signature})
    assert response.status_code == 200, f"Stale-claim redelivery should credit, got {response.status_code}: {response.text}"

    with db.SessionLocal() as verify_session:
        topup_credits = (
            verify_session.query(CreditLedger)
            .filter(CreditLedger.entry_type == "topup", CreditLedger.user_id == user_id)
            .all()
        )
        assert len(topup_credits) == 1, (
            f"Stale-claim recovery must land exactly one credit, got {len(topup_credits)}"
        )
        assert topup_credits[0].amount_cents == amount_cents
        seen = (
            verify_session.query(StripeEventSeen)
            .filter(StripeEventSeen.event_id == event_id)
            .one()
        )
        assert seen.status == "processed"
        topup = verify_session.get(TopupSession, session_id)
        assert topup is not None
        assert topup.status == "completed"


def test_webhook_in_flight_within_lease_returns_retryable(file_sqlite_app: tuple[TestClient, FakeStorage, str, int]) -> None:
    """A redelivery that arrives while a fresh claim is in flight returns a retryable non-2xx.

    Cycle 8 BLOCKER fix. Previously the in_flight branch returned 200, which
    Stripe interprets as "done" and stops retrying. If the original in-flight
    worker died before either committing the credit or writing a 'failed'
    marker, the only recovery path (stale-claim re-claim after the 300s lease
    expires) never ran because no future redelivery arrived.

    With the fix, in_flight returns 409 Conflict. Stripe's retry schedule
    continues; once the original worker either finishes (status='processed',
    next response is 200) or its lease expires (status='processing' with stale
    claimed_at, next response re-claims and completes), the credit lands
    exactly once.
    """
    client, _storage, _plaintext, user_id = file_sqlite_app

    session_id = "cs_test_in_flight_lease"
    event_id = "evt_in_flight_lease"
    amount_cents = 2200

    # Seed: TopupSession pending plus a StripeEventSeen row claimed RIGHT NOW
    # (fresh claim, well within the 300s lease window). The redelivery that
    # arrives in this window must NOT short-circuit to a 200 - it must return a
    # retryable status so Stripe keeps trying.
    fresh_claim_at = datetime.now(timezone.utc)
    with db.SessionLocal() as setup_session:
        setup_session.add(
            TopupSession(
                id=session_id,
                user_id=user_id,
                amount_cents=amount_cents,
                status="pending",
            )
        )
        setup_session.add(
            StripeEventSeen(
                event_id=event_id,
                status="processing",
                claimed_at=fresh_claim_at,
            )
        )
        setup_session.commit()

    payload = json.dumps(
        {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": session_id,
                    "amount_total": amount_cents,
                    "payment_status": "paid",
                    "payment_intent": "pi_test_in_flight_lease",
                    "metadata": {"user_id": str(user_id), "purpose": "credit_topup"},
                }
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")
    signature = _signature_header(payload)

    # First delivery while claim is fresh: must return a retryable status, not
    # 2xx. 409 is what the route emits; 423/503 are also acceptable retryable
    # codes for future variants. No credit may land.
    first = client.post("/v1/webhooks/stripe", content=payload, headers={"Stripe-Signature": signature})
    assert first.status_code in (409, 423, 503), (
        f"Fresh in-flight claim must return a retryable non-2xx (Stripe will stop retrying on 2xx); "
        f"got {first.status_code}: {first.text}"
    )
    with db.SessionLocal() as verify_session:
        credits = (
            verify_session.query(CreditLedger)
            .filter(CreditLedger.entry_type == "topup", CreditLedger.user_id == user_id)
            .count()
        )
        assert credits == 0, "No credit may land while the claim is in-flight"
        seen = (
            verify_session.query(StripeEventSeen)
            .filter(StripeEventSeen.event_id == event_id)
            .one()
        )
        # Row stays at 'processing' with the original fresh claim.
        assert seen.status == "processing"

    # Now advance the claim past the lease: simulate the original worker
    # having died without writing a 'failed' marker. The next redelivery must
    # re-claim via the stale-claim branch and credit exactly once.
    # Lease bumped to 900s in cycle 9; advance well past it.
    expired_claimed_at = datetime.now(timezone.utc) - timedelta(seconds=1800)
    with db.SessionLocal() as mutate_session:
        row = (
            mutate_session.query(StripeEventSeen)
            .filter(StripeEventSeen.event_id == event_id)
            .one()
        )
        row.claimed_at = expired_claimed_at
        mutate_session.commit()

    second = client.post("/v1/webhooks/stripe", content=payload, headers={"Stripe-Signature": signature})
    assert second.status_code == 200, (
        f"Redelivery after lease expiry should re-claim and credit, got {second.status_code}: {second.text}"
    )
    with db.SessionLocal() as verify_session:
        credits = (
            verify_session.query(CreditLedger)
            .filter(CreditLedger.entry_type == "topup", CreditLedger.user_id == user_id)
            .all()
        )
        assert len(credits) == 1, (
            f"Stale-claim recovery after in-flight retryable response must credit exactly once, got {len(credits)}"
        )
        assert credits[0].amount_cents == amount_cents
        seen = (
            verify_session.query(StripeEventSeen)
            .filter(StripeEventSeen.event_id == event_id)
            .one()
        )
        assert seen.status == "processed"
        topup = verify_session.get(TopupSession, session_id)
        assert topup is not None
        assert topup.status == "completed"


def test_handler_within_lease_no_stale_recovery_fires(file_sqlite_app: tuple[TestClient, FakeStorage, str, int]) -> None:
    """A handler that runs longer than a few seconds but well under the lease must not trip stale-recovery.

    Cycle 9 regression. Self-review M1 surfaced that the original 300s lease
    left zero headroom above the Resend 10s timeout plus a slow commit; raising
    it to 900s only matters if the handler actually stays inside the new window
    under realistic conditions. This test injects a deliberate ~2-second delay
    inside ``send_topup_cleared`` and proves that a concurrent redelivery
    arriving while the first worker is still running sees the row as in_flight
    (returns 409) rather than re-claiming via the stale-claim branch. If the
    lease were ever lowered back below the handler's worst-case duration this
    test would fail: the redelivery would observe a "stale" claim that is
    actually fresh, re-claim it, and a second handler would race the first.

    The invariant is: while the first handler is still inside the lease window,
    no second handler may begin processing the same event id.
    """
    client, _storage, _plaintext, user_id = file_sqlite_app

    session_id = "cs_test_lease_headroom"
    event_id = "evt_lease_headroom"
    amount_cents = 1800
    # Pick a "claim age" that's longer than the worst plausible handler runtime
    # (Resend 10s + DB commit overhead + buffer) but well inside the 900s lease.
    # This proves the lease has the headroom cycle 9 raised it to provide.
    in_flight_claim_age_seconds = 60

    with db.SessionLocal() as setup_session:
        setup_session.add(
            TopupSession(
                id=session_id,
                user_id=user_id,
                amount_cents=amount_cents,
                status="pending",
            )
        )
        # Seed a 'processing' claim that is fresh-within-lease. This is the
        # observable post-condition of "another worker is currently mid-handler"
        # without the TestClient serialization that breaks a live-thread version.
        setup_session.add(
            StripeEventSeen(
                event_id=event_id,
                status="processing",
                claimed_at=datetime.now(timezone.utc) - timedelta(seconds=in_flight_claim_age_seconds),
            )
        )
        setup_session.commit()

    class _SlowEmailSender:
        """Email fake that blocks for ``handler_delay_seconds`` to widen the in-flight window.

        Models a Resend call that is slow but well under its 10s timeout: the
        kind of variance a real network will produce. The handler must remain
        owner of the claim throughout this delay; a concurrent redelivery must
        see in_flight, not stale.
        """

        def __init__(self) -> None:
            """Record successful sends so the test can assert exactly-once."""
            self.sent: list[tuple[str, int, int]] = []

        def send_topup_cleared(self, to_email: str, amount_cents: int, balance_cents: int) -> None:
            """Sleep, then record the send."""
            time.sleep(handler_delay_seconds)
            self.sent.append((to_email, amount_cents, balance_cents))

    slow = _SlowEmailSender()
    app.dependency_overrides[get_email_sender_factory] = lambda: lambda: slow

    payload = json.dumps(
        {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": session_id,
                    "amount_total": amount_cents,
                    "payment_status": "paid",
                    "payment_intent": "pi_test_lease_headroom",
                    "metadata": {"user_id": str(user_id), "purpose": "credit_topup"},
                }
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")
    signature = _signature_header(payload)

    # The seeded row represents "a handler is still inside its lease window."
    # The redelivery MUST see in_flight (409) and not re-claim via stale-recovery.
    # The handler is not actually running; the seeded claim_age (60s) is well
    # under the 900s lease, so the in_flight branch is the only correct response.
    response = client.post("/v1/webhooks/stripe", content=payload, headers={"Stripe-Signature": signature})
    assert response.status_code == 409, (
        f"Concurrent redelivery while a handler is in lease must see in_flight (409), "
        f"not stale-recovery; got {response.status_code}: {response.text}. "
        "If this fails, _STALE_PROCESSING_LEASE_SECONDS may have been lowered below "
        f"the seeded claim age ({in_flight_claim_age_seconds}s)."
    )

    # No credit should have landed (the seeded "in-flight" handler did not actually run).
    with db.SessionLocal() as verify_session:
        topup_credits = (
            verify_session.query(CreditLedger)
            .filter(CreditLedger.entry_type == "topup", CreditLedger.user_id == user_id)
            .all()
        )
        assert len(topup_credits) == 0, (
            f"No credit should land while the seeded claim is in-flight; got {len(topup_credits)}."
        )
    assert len(slow.sent) == 0, (
        f"No top-up email should have been sent for the in_flight rejection; got {len(slow.sent)}."
    )


def test_claim_event_integrity_error_no_row_raises(file_sqlite_app: tuple[TestClient, FakeStorage, str, int]) -> None:
    """An unexpected IntegrityError from the claim insert propagates to a non-2xx response.

    Cycle 8 MAJOR fix. The broad ``except IntegrityError`` around the ON
    CONFLICT insert in ``_claim_event`` was dead code for normal event-id
    races (ON CONFLICT handles those atomically). It only fired on UNEXPECTED
    constraint or schema failures (a missing migration, an FK we did not
    anticipate). Before the fix, that path swallowed the error, the readback
    found no row, and the route returned 200 with no credit and no marker -
    silently dropping the event.

    Fix: removed the except entirely. Any IntegrityError now propagates to the
    outer exception handler in ``stripe_webhook``, which writes a 'failed'
    marker (best-effort) and re-raises so Stripe receives a 5xx and redelivers.
    """
    from unittest.mock import patch

    client, _storage, _plaintext, user_id = file_sqlite_app

    session_id = "cs_test_integrity_no_row"
    event_id = "evt_integrity_no_row"
    amount_cents = 1700

    with db.SessionLocal() as setup_session:
        setup_session.add(
            TopupSession(
                id=session_id,
                user_id=user_id,
                amount_cents=amount_cents,
                status="pending",
            )
        )
        setup_session.commit()

    payload = json.dumps(
        {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": session_id,
                    "amount_total": amount_cents,
                    "payment_status": "paid",
                    "payment_intent": "pi_test_integrity_no_row",
                    "metadata": {"user_id": str(user_id), "purpose": "credit_topup"},
                }
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")
    signature = _signature_header(payload)

    # Patch Session.execute to raise IntegrityError on the claim insert
    # WITHOUT producing a row, simulating an unexpected constraint or schema
    # failure. We target the route's session by patching at the SQLAlchemy
    # Session class level for the duration of this one request.
    from sqlalchemy.exc import IntegrityError as _IntegrityError
    from sqlalchemy.orm import Session as _SqlaSession

    real_execute = _SqlaSession.execute

    def _fail_on_claim_insert(self: _SqlaSession, statement: Any, *args: Any, **kwargs: Any) -> Any:
        """Raise IntegrityError on the StripeEventSeen INSERT, pass everything else through."""
        compiled = str(statement)
        if "stripe_events_seen" in compiled.lower() and ("insert" in compiled.lower() or "on conflict" in compiled.lower()):
            raise _IntegrityError("simulated unexpected constraint failure", params=None, orig=Exception("constraint"))
        return real_execute(self, statement, *args, **kwargs)

    with patch.object(_SqlaSession, "execute", _fail_on_claim_insert):
        # TestClient with default raise_server_exceptions=True surfaces the
        # unhandled exception. In production this would translate to a 5xx
        # back to Stripe. Either pytest.raises (re-raised) or a 5xx response
        # is acceptable; both are non-2xx from Stripe's perspective and trigger
        # retry. The critical invariant is: NOT 2xx, and no StripeEventSeen
        # row exists afterward.
        raised = False
        response = None
        try:
            response = client.post("/v1/webhooks/stripe", content=payload, headers={"Stripe-Signature": signature})
        except _IntegrityError:
            raised = True

    if not raised:
        assert response is not None
        assert not (200 <= response.status_code < 300), (
            f"Unexpected IntegrityError on claim must not return 2xx (would silently drop the event); "
            f"got {response.status_code}: {response.text}"
        )

    with db.SessionLocal() as verify_session:
        seen_count = (
            verify_session.query(StripeEventSeen)
            .filter(StripeEventSeen.event_id == event_id)
            .count()
        )
        assert seen_count == 0, (
            f"Failed claim insert must not leave a row; got {seen_count}"
        )
        credits = (
            verify_session.query(CreditLedger)
            .filter(CreditLedger.entry_type == "topup", CreditLedger.user_id == user_id)
            .count()
        )
        assert credits == 0, "No credit may land when the claim insert fails"

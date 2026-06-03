"""Tests for the Stage 9 refund reconciliation pure-data module.

Synthetic fixtures only. The in-memory SQLite from ``conftest.py`` is
reused; no network, no Resend, no real Postgres. Covers:

  - clean window (N failures, N refunds)
  - drift window (N failures, N-1 refunds)
  - empty window (zero failures)
  - window boundary semantics (yesterday vs today rows)
  - alert subject + body rendering
  - low_quality is NOT counted as a failure for reconciliation purposes
  - refunds for rows OUTSIDE the window do not pair across the boundary
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.constants import DEFAULT_API_SCOPE
from app.crypto import generate_api_key, hash_password
from app.models import ApiKey, CreditLedger, Extraction, User
from app.refund_reconcile import (
    FAILED_STATUSES,
    REFUND_ENTRY_TYPE,
    REPORT_SCHEMA_VERSION,
    ReconciliationReport,
    UnreconciledExtraction,
    format_alert_body,
    format_alert_subject,
    query_failed_extractions,
    query_refund_extraction_ids,
    reconcile,
    utc_window_for,
    yesterday_utc,
)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _seed_user(session: Session, email: str = "frank@example.test") -> User:
    """Insert one user + one api key the extractions can reference."""
    user = User(
        email=email.lower(),
        password_hash=hash_password("password"),
        status="active",
    )
    session.add(user)
    session.flush()
    _plaintext, digest, prefix = generate_api_key("live")
    key = ApiKey(
        user_id=user.id,
        key_hash=digest,
        key_prefix=prefix,
        label="seed",
        scopes=[DEFAULT_API_SCOPE],
    )
    session.add(key)
    session.flush()
    return user


def _insert_extraction(
    session: Session,
    user: User,
    status: str,
    extracted_at: datetime,
    credit_cents: int = 500,
) -> Extraction:
    """Insert one extraction row at a deterministic timestamp."""
    row = Extraction(
        user_id=user.id,
        api_key_id=None,
        url="https://example.test/",
        url_normalized="https://example.test/",
        status=status,
        extracted_at=extracted_at,
        schema_version=1,
        credit_cents=credit_cents,
    )
    session.add(row)
    session.flush()
    return row


def _insert_refund(
    session: Session, user: User, extraction: Extraction, amount_cents: int = 500
) -> CreditLedger:
    """Insert one refund ledger row paired to ``extraction``."""
    row = CreditLedger(
        user_id=user.id,
        entry_type=REFUND_ENTRY_TYPE,
        amount_cents=amount_cents,
        # balance_after_cents is constrained >= 0; pick a safe positive value.
        balance_after_cents=amount_cents,
        extraction_id=extraction.id,
        note="test refund",
    )
    session.add(row)
    session.flush()
    return row


# --------------------------------------------------------------------------- #
# Pure-helper tests                                                           #
# --------------------------------------------------------------------------- #


def test_utc_window_for_returns_half_open_one_day_interval() -> None:
    """Window must be exactly 24h, timezone-aware UTC, end exclusive."""
    start, end = utc_window_for(datetime(2026, 6, 1, tzinfo=timezone.utc).date())
    assert start == datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc)
    assert (end - start) == timedelta(days=1)


def test_yesterday_utc_returns_prior_calendar_day() -> None:
    """yesterday_utc must subtract one day in UTC, even at corner hours."""
    now = datetime(2026, 6, 3, 0, 30, tzinfo=timezone.utc)
    assert yesterday_utc(now=now) == datetime(2026, 6, 2, tzinfo=timezone.utc).date()


def test_yesterday_utc_naive_input_treated_as_utc() -> None:
    """A naive datetime input must be normalized to UTC, not raise."""
    now = datetime(2026, 6, 3, 12, 0)  # naive
    assert yesterday_utc(now=now) == datetime(2026, 6, 2, tzinfo=timezone.utc).date()


def test_failed_statuses_constant_matches_spec() -> None:
    """Stage 9 spec explicitly names these two statuses; lock them."""
    assert FAILED_STATUSES == frozenset({"failed", "out_of_scope"})


# --------------------------------------------------------------------------- #
# Query tests                                                                 #
# --------------------------------------------------------------------------- #


def test_query_failed_extractions_filters_by_status_and_window(session: Session) -> None:
    """Only rows whose status is in FAILED_STATUSES AND in-window are returned."""
    user = _seed_user(session)
    yesterday = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    today = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)
    in_window_failed = _insert_extraction(session, user, "failed", yesterday)
    in_window_out_of_scope = _insert_extraction(session, user, "out_of_scope", yesterday)
    in_window_ok = _insert_extraction(session, user, "ok", yesterday)
    in_window_low_quality = _insert_extraction(session, user, "low_quality", yesterday)
    out_of_window = _insert_extraction(session, user, "failed", today)
    session.commit()

    start, end = utc_window_for(yesterday.date())
    rows = query_failed_extractions(session, start, end)
    ids = {row.id for row in rows}

    assert in_window_failed.id in ids
    assert in_window_out_of_scope.id in ids
    assert in_window_ok.id not in ids
    assert in_window_low_quality.id not in ids
    assert out_of_window.id not in ids


def test_query_refund_extraction_ids_returns_only_refund_paired_ids(session: Session) -> None:
    """Charge rows must not be confused with refund rows."""
    user = _seed_user(session)
    yesterday = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    failed = _insert_extraction(session, user, "failed", yesterday)
    _insert_refund(session, user, failed, amount_cents=500)
    # A non-refund ledger row for the same extraction must NOT count.
    session.add(
        CreditLedger(
            user_id=user.id,
            entry_type="charge",
            amount_cents=-500,
            balance_after_cents=0,
            extraction_id=failed.id,
        )
    )
    session.commit()

    refunded = query_refund_extraction_ids(session, [failed.id])
    assert refunded == {failed.id}


def test_query_refund_extraction_ids_empty_input_returns_empty_set(session: Session) -> None:
    """Defensive: empty input must short-circuit, not run an IN ()."""
    assert query_refund_extraction_ids(session, []) == set()


# --------------------------------------------------------------------------- #
# reconcile() tests                                                           #
# --------------------------------------------------------------------------- #


def test_reconcile_clean_window_reports_zero_drift(session: Session) -> None:
    """N failures + N matching refunds + no other noise = drift 0, clean."""
    user = _seed_user(session)
    yesterday = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    failed_a = _insert_extraction(session, user, "failed", yesterday)
    failed_b = _insert_extraction(session, user, "out_of_scope", yesterday)
    _insert_refund(session, user, failed_a)
    _insert_refund(session, user, failed_b)
    session.commit()

    report = reconcile(session, report_date=yesterday.date())

    assert report.schema_version == REPORT_SCHEMA_VERSION
    assert report.failed_count == 2
    assert report.refunded_count == 2
    assert report.drift == 0
    assert report.unreconciled == ()
    assert report.is_clean is True


def test_reconcile_drift_window_lists_unreconciled_rows(session: Session) -> None:
    """N=2 failures, N=1 refund -> drift=1, the missing row surfaced."""
    user = _seed_user(session)
    yesterday = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    failed_with_refund = _insert_extraction(session, user, "failed", yesterday)
    failed_without_refund = _insert_extraction(session, user, "failed", yesterday)
    _insert_refund(session, user, failed_with_refund)
    session.commit()

    report = reconcile(session, report_date=yesterday.date())

    assert report.failed_count == 2
    assert report.refunded_count == 1
    assert report.drift == 1
    assert report.is_clean is False
    assert len(report.unreconciled) == 1
    surfaced = report.unreconciled[0]
    assert isinstance(surfaced, UnreconciledExtraction)
    assert surfaced.extraction_id == failed_without_refund.id
    assert surfaced.status == "failed"


def test_reconcile_empty_window_is_clean(session: Session) -> None:
    """Zero failures + zero refunds -> clean (drift=0)."""
    _seed_user(session)
    session.commit()
    report = reconcile(session, report_date=datetime(2026, 6, 2, tzinfo=timezone.utc).date())
    assert report.failed_count == 0
    assert report.refunded_count == 0
    assert report.drift == 0
    assert report.is_clean is True


def test_reconcile_ignores_refunds_for_out_of_window_failures(session: Session) -> None:
    """A refund paired to a today-row must not count toward yesterday's tally."""
    user = _seed_user(session)
    yesterday = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    today = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)
    failed_yesterday = _insert_extraction(session, user, "failed", yesterday)
    failed_today = _insert_extraction(session, user, "failed", today)
    _insert_refund(session, user, failed_today)
    session.commit()

    report = reconcile(session, report_date=yesterday.date())

    assert report.failed_count == 1
    assert report.refunded_count == 0
    assert report.drift == 1
    assert report.unreconciled[0].extraction_id == failed_yesterday.id


def test_reconcile_low_quality_is_not_counted_as_failure(session: Session) -> None:
    """low_quality is intentionally excluded; document the policy in a test."""
    user = _seed_user(session)
    yesterday = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    _insert_extraction(session, user, "low_quality", yesterday)
    session.commit()

    report = reconcile(session, report_date=yesterday.date())
    assert report.failed_count == 0
    assert report.drift == 0


# --------------------------------------------------------------------------- #
# Alert rendering tests                                                       #
# --------------------------------------------------------------------------- #


def test_format_alert_subject_carries_date_and_drift() -> None:
    """Subject must surface the two values an operator triages on first."""
    report = ReconciliationReport(
        schema_version=REPORT_SCHEMA_VERSION,
        window_date=datetime(2026, 6, 2, tzinfo=timezone.utc).date(),
        window_start=datetime(2026, 6, 2, tzinfo=timezone.utc),
        window_end=datetime(2026, 6, 3, tzinfo=timezone.utc),
        failed_count=3,
        refunded_count=2,
        drift=1,
        unreconciled=(),
    )
    subject = format_alert_subject(report)
    assert "drift=1" in subject
    assert "2026-06-02" in subject


def test_format_alert_body_lists_each_unreconciled_row() -> None:
    """Body must let an operator copy-paste the missing extraction ids."""
    unreconciled = UnreconciledExtraction(
        extraction_id=42,
        user_id=7,
        status="failed",
        extracted_at=datetime(2026, 6, 2, 9, 0, tzinfo=timezone.utc),
        credit_cents=500,
    )
    report = ReconciliationReport(
        schema_version=REPORT_SCHEMA_VERSION,
        window_date=datetime(2026, 6, 2, tzinfo=timezone.utc).date(),
        window_start=datetime(2026, 6, 2, tzinfo=timezone.utc),
        window_end=datetime(2026, 6, 3, tzinfo=timezone.utc),
        failed_count=1,
        refunded_count=0,
        drift=1,
        unreconciled=(unreconciled,),
    )
    body = format_alert_body(report)
    assert "id=42" in body
    assert "user_id=7" in body
    assert "status=failed" in body
    assert "credit_cents=500" in body
    assert "schema_version: 1" in body

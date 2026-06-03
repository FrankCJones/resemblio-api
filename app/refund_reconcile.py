"""Pure-data refund reconciliation for the Resemblio API.

Why this module exists
======================
Stage 9 of the 2026-06-03 back-on-track TDD plan (failure inventory item
#18, observability gap). We ship the refund-on-failure code path; what we
do NOT yet observe is whether the path fires for every charged extraction
that ends in a terminal-failure status. A daily reconciliation that
compares the failure count to the refund count is the simplest assertion
that surfaces drift before a customer notices.

The reconciliation answers one question per day: for every extraction the
system marked as a terminal failure yesterday, is there a paired refund
ledger row that returned the charge to the customer?

What counts as a "failure"
==========================
Per Stage 9 spec: ``extractions.status IN ('failed', 'out_of_scope')``.
``low_quality`` is NOT in the set because the route handler treats it as a
review-pending state, not a refund-required terminal state (the auto-
refund quality-scoring path is a separate concern with its own audit
table). Adding ``low_quality`` here would mis-count rows the system
intentionally leaves charged pending operator triage.

What counts as a "refund"
=========================
A row in ``credit_ledger`` with ``entry_type='refund'`` whose
``extraction_id`` points at the failed extraction row. The
``_refund`` helper in ``app/routes/extractions.py`` is the canonical
write path; any other source must use the same shape.

Window semantics
================
"Yesterday in UTC" is the canonical window. The CLI passes
``datetime.now(timezone.utc).date() - timedelta(days=1)`` as the report
date. Pure-data functions accept the date explicitly so tests can drive
boundaries deterministically.

A row is counted as "yesterday's" when
``extracted_at`` falls in ``[yesterday 00:00 UTC, today 00:00 UTC)``.

Drift policy
============
The script alerts on ANY non-zero drift. False positives are cheap (one
Resend email to Frank); false negatives are a silent stranded customer
charge. Per the workspace bias-toward-action rule, we accept the email
volume in exchange for the floor.

Schema
======
``ReconciliationReport`` carries ``schema_version=1``. Bumped together
with the migration if the failure-or-refund row shapes change in a way
downstream consumers must notice.

Testability
===========
Every function in this module is pure: it takes a SQLAlchemy session and
a date, and returns a dataclass. No network. No alert dispatch. The
script (``scripts/refund_reconcile.py``) is the thin wrapper that wires
real Postgres + Resend.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CreditLedger, Extraction

# --------------------------------------------------------------------------- #
# Schema version + named constants                                            #
# --------------------------------------------------------------------------- #

REPORT_SCHEMA_VERSION = 1
"""Schema version stamped onto every ``ReconciliationReport`` instance."""

FAILED_STATUSES: frozenset[str] = frozenset({"failed", "out_of_scope"})
"""Extraction.status values that REQUIRE a paired refund ledger row.

Per Stage 9 spec. ``low_quality`` is excluded by design; see module
docstring.
"""

REFUND_ENTRY_TYPE: str = "refund"
"""CreditLedger.entry_type value the reconciliation expects on the pair side."""


# --------------------------------------------------------------------------- #
# Data shapes                                                                 #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class UnreconciledExtraction:
    """One terminal-failure extraction that lacks a paired refund row.

    The CLI surfaces these in the alert body so an operator can trace
    the missing refund to a specific extraction id + user without
    cross-referencing Postgres by hand.
    """

    extraction_id: int
    user_id: int
    status: str
    extracted_at: datetime
    credit_cents: int


@dataclass(frozen=True)
class ReconciliationReport:
    """Daily refund-reconciliation result for one UTC window.

    ``drift`` is positive when failures exceed refunds (the bad case:
    customer charged, not refunded). ``drift`` is negative if refunds
    exceed failures, which shouldn't happen under normal operation but
    is surfaced anyway because it indicates either a bug in the
    counting logic or a refund issued for a non-terminal extraction
    (which is also actionable).
    """

    schema_version: int
    window_date: date
    window_start: datetime
    window_end: datetime
    failed_count: int
    refunded_count: int
    drift: int
    unreconciled: tuple[UnreconciledExtraction, ...]

    @property
    def is_clean(self) -> bool:
        """True when failure count equals refund count AND no rows are unreconciled."""
        return self.drift == 0 and not self.unreconciled


# --------------------------------------------------------------------------- #
# Pure functions                                                              #
# --------------------------------------------------------------------------- #


def utc_window_for(report_date: date) -> tuple[datetime, datetime]:
    """Return the ``[start, end)`` UTC datetime bounds for one calendar day.

    ``report_date`` is interpreted as a UTC calendar date; the returned
    pair is always timezone-aware in UTC. End is exclusive (next-day
    midnight) so the half-open interval composes cleanly with
    ``BETWEEN``-style WHERE clauses written as ``>=`` + ``<``.
    """
    start = datetime.combine(report_date, time.min, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start, end


def yesterday_utc(now: datetime | None = None) -> date:
    """Return the UTC calendar date one day before ``now`` (defaults to wall-clock).

    Centralized so the CLI and the tests agree on "yesterday" without
    inlining ``now() - timedelta(days=1)`` literals.
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment.astimezone(timezone.utc) - timedelta(days=1)).date()


def query_failed_extractions(
    session: Session, window_start: datetime, window_end: datetime
) -> list[Extraction]:
    """Return every terminal-failure extraction created in the half-open UTC window.

    Pure read query. Caller owns the session lifecycle. Returns rows
    ordered by ``id`` so the diff against refunds is deterministic.
    """
    rows = session.execute(
        select(Extraction)
        .where(
            Extraction.status.in_(FAILED_STATUSES),
            Extraction.extracted_at >= window_start,
            Extraction.extracted_at < window_end,
        )
        .order_by(Extraction.id)
    ).scalars().all()
    return list(rows)


def query_refund_extraction_ids(
    session: Session, extraction_ids: Iterable[int]
) -> set[int]:
    """Return the subset of ``extraction_ids`` that have at least one paired refund.

    A "paired refund" is a ``credit_ledger`` row with
    ``entry_type='refund'`` and ``extraction_id`` equal to one of the
    supplied ids. Multiple refund rows for the same extraction (should
    not happen because ``_refund`` is idempotent, but theoretically
    possible) still count as one pairing.
    """
    ids = list(extraction_ids)
    if not ids:
        return set()
    rows = session.execute(
        select(CreditLedger.extraction_id)
        .where(
            CreditLedger.entry_type == REFUND_ENTRY_TYPE,
            CreditLedger.extraction_id.in_(ids),
        )
    ).scalars().all()
    # extraction_id is nullable on credit_ledger; filter Nones defensively
    # even though the WHERE clause already restricts to non-null values.
    return {extraction_id for extraction_id in rows if extraction_id is not None}


def reconcile(
    session: Session, report_date: date | None = None, *, now: datetime | None = None
) -> ReconciliationReport:
    """Compute the refund reconciliation for one UTC calendar day.

    Defaults ``report_date`` to ``yesterday_utc(now)``. The two-argument
    surface lets tests pin both the date and the wall-clock reference;
    callers in production only ever pass ``now`` (or nothing) and let
    the helper derive yesterday.

    The returned report is a value object: it captures everything
    needed to render an alert body, write a log line, or persist a
    history row without touching the session again.
    """
    target = report_date or yesterday_utc(now=now)
    window_start, window_end = utc_window_for(target)
    failures = query_failed_extractions(session, window_start, window_end)
    failure_ids = [row.id for row in failures]
    refunded_ids = query_refund_extraction_ids(session, failure_ids)
    failed_count = len(failure_ids)
    refunded_count = sum(1 for fid in failure_ids if fid in refunded_ids)
    unreconciled = tuple(
        UnreconciledExtraction(
            extraction_id=row.id,
            user_id=row.user_id,
            status=row.status,
            extracted_at=row.extracted_at,
            credit_cents=row.credit_cents,
        )
        for row in failures
        if row.id not in refunded_ids
    )
    return ReconciliationReport(
        schema_version=REPORT_SCHEMA_VERSION,
        window_date=target,
        window_start=window_start,
        window_end=window_end,
        failed_count=failed_count,
        refunded_count=refunded_count,
        drift=failed_count - refunded_count,
        unreconciled=unreconciled,
    )


# --------------------------------------------------------------------------- #
# Alert payload helpers (pure; the CLI dispatches via Resend)                 #
# --------------------------------------------------------------------------- #


def format_alert_subject(report: ReconciliationReport) -> str:
    """Render the one-line Resend subject for a drift alert.

    Kept distinct from the body so the CLI can log the subject and
    body separately and so future tests can assert the wording.
    """
    return (
        f"[Resemblio] refund reconciliation drift={report.drift} "
        f"on {report.window_date.isoformat()}"
    )


def format_alert_body(report: ReconciliationReport) -> str:
    """Render the multi-line Resend body for a drift alert.

    Lists every unreconciled extraction (id, user_id, status, charge)
    so an operator can run a targeted ``SELECT`` against Postgres
    without re-deriving the window math.
    """
    lines = [
        "Refund reconciliation drift detected.",
        "",
        f"schema_version: {report.schema_version}",
        f"window: {report.window_start.isoformat()} -> {report.window_end.isoformat()}",
        f"failed_count: {report.failed_count}",
        f"refunded_count: {report.refunded_count}",
        f"drift: {report.drift}",
        "",
        "Unreconciled extractions (failure rows missing a paired refund):",
    ]
    if not report.unreconciled:
        lines.append("  (none; drift is negative, which itself is actionable)")
    else:
        for row in report.unreconciled:
            lines.append(
                f"  - id={row.extraction_id} user_id={row.user_id} "
                f"status={row.status} credit_cents={row.credit_cents} "
                f"extracted_at={row.extracted_at.isoformat()}"
            )
    lines.append("")
    lines.append(
        "Action: confirm refund or open ticket. Source: app/refund_reconcile.py."
    )
    return "\n".join(lines)

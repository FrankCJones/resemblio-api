"""Regression tests for the idempotency-keys sweep job.

Contract:

* Rows older than ``IDEMPOTENCY_KEY_TTL_SECONDS`` are deleted.
* Rows within the TTL window are preserved.
* An empty table is a no-op (deleted_count == 0, no error).
* An all-fresh table is a no-op (deleted_count == 0).
* The cutoff returned in the result equals ``now - TTL`` for the
  caller-supplied ``now`` so log lines and tests can reason about it.

Provenance: 2026-06-02 R7 follow-on dispatch
(``2026-06-02-idempotency-sweep-job.md``).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.cli.sweep_idempotency import SweepResult, _compute_cutoff, sweep_expired
from app.constants import IDEMPOTENCY_KEY_TTL_SECONDS
from app.models import IdempotencyKey
from tests.conftest import seed_user


def _insert_row(
    session: Session,
    user_id: int,
    key_suffix: str,
    created_at: datetime,
) -> None:
    """Insert one idempotency row at the requested ``created_at`` instant.

    Bypasses the production write path (``store_response``) because that
    function only writes ``now()`` server-side; the sweep tests need
    explicit control over the timestamp.
    """
    session.add(
        IdempotencyKey(
            user_id=user_id,
            key=f"sweep-test-key-{key_suffix}",
            request_hash="a" * 64,
            status_code=200,
            response_body="{}",
            created_at=created_at,
        )
    )
    session.commit()


def _row_count(session: Session, user_id: int) -> int:
    """Return how many idempotency rows exist for ``user_id``."""
    return (
        session.query(IdempotencyKey)
        .filter(IdempotencyKey.user_id == user_id)
        .count()
    )


def test_compute_cutoff_subtracts_ttl() -> None:
    """``_compute_cutoff`` returns ``now - TTL`` exactly."""
    now = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)
    cutoff = _compute_cutoff(now)
    assert cutoff == now - timedelta(seconds=IDEMPOTENCY_KEY_TTL_SECONDS)


def test_sweep_deletes_expired_preserves_fresh(session: Session) -> None:
    """Seed rows spanning the TTL boundary; sweep only deletes the old ones."""
    user, _, _ = seed_user(session)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=IDEMPOTENCY_KEY_TTL_SECONDS)

    # 10 rows total: 4 expired (older than cutoff), 6 fresh (newer).
    expired_offsets_hours = [25, 30, 48, 72]
    fresh_offsets_hours = [0, 1, 6, 12, 18, 23]

    for idx, hours in enumerate(expired_offsets_hours):
        _insert_row(session, user.id, f"old-{idx}", now - timedelta(hours=hours))
    for idx, hours in enumerate(fresh_offsets_hours):
        _insert_row(session, user.id, f"new-{idx}", now - timedelta(hours=hours))

    assert _row_count(session, user.id) == 10

    result = sweep_expired(session, now=now)

    assert isinstance(result, SweepResult)
    assert result.deleted_count == len(expired_offsets_hours)
    assert result.cutoff == cutoff
    assert _row_count(session, user.id) == len(fresh_offsets_hours)

    # Spot-check: no remaining row is older than the cutoff.
    remaining = session.query(IdempotencyKey).filter(IdempotencyKey.user_id == user.id).all()
    for row in remaining:
        row_ts = row.created_at
        if row_ts.tzinfo is None:
            row_ts = row_ts.replace(tzinfo=timezone.utc)
        assert row_ts >= cutoff


def test_sweep_empty_table_is_noop(session: Session) -> None:
    """No rows present: sweep deletes zero, raises nothing."""
    result = sweep_expired(session)
    assert result.deleted_count == 0


def test_sweep_all_fresh_is_noop(session: Session) -> None:
    """All rows within the TTL window: sweep deletes zero."""
    user, _, _ = seed_user(session)
    now = datetime.now(timezone.utc)
    for idx in range(5):
        _insert_row(session, user.id, f"fresh-{idx}", now - timedelta(hours=idx))
    assert _row_count(session, user.id) == 5

    result = sweep_expired(session, now=now)

    assert result.deleted_count == 0
    assert _row_count(session, user.id) == 5


def test_sweep_boundary_row_strictly_older_deleted(session: Session) -> None:
    """A row exactly at ``cutoff - 1s`` is deleted; exactly at ``cutoff`` is kept.

    The query uses ``created_at < cutoff`` (strict less-than) so a row
    timestamped at the cutoff itself survives the sweep. This test pins
    that boundary semantics.
    """
    user, _, _ = seed_user(session)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=IDEMPOTENCY_KEY_TTL_SECONDS)

    _insert_row(session, user.id, "at-cutoff", cutoff)
    _insert_row(session, user.id, "one-sec-before-cutoff", cutoff - timedelta(seconds=1))

    result = sweep_expired(session, now=now)

    assert result.deleted_count == 1
    remaining_keys = {
        row.key
        for row in session.query(IdempotencyKey).filter(IdempotencyKey.user_id == user.id).all()
    }
    assert remaining_keys == {"sweep-test-key-at-cutoff"}

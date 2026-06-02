"""Sweep expired rows from the ``idempotency_keys`` table.

Wire-up
-------
Invoked as ``python -m app.cli.sweep_idempotency`` from a systemd timer
(``deploy/systemd/resemblio-idempotency-sweep.timer``) once per hour on
``resemblio-prod-01``. The 24h TTL constant
(``IDEMPOTENCY_KEY_TTL_SECONDS``) is the same one the route's read path
uses to decide a row is expired; this job is the housekeeping arm of the
same contract, deleting rows the read path would have ignored anyway.

Why hourly (not 24h)
--------------------
The ``ix_idempotency_keys_created_at`` index makes a bounded ``DELETE``
trivially cheap (single index range, no full scan). Running hourly keeps
the table small enough that the index never grows past O(thousands), and
the upper bound on stale-row count between sweeps is one hour of traffic
even in a burst scenario. Running every 24h would let a burst day balloon
the table for a full day before reclamation.

Reliability
-----------
The session is opened via ``SessionLocal`` so the engine's
``pool_pre_ping`` (in ``app/db.py``) re-validates a connection before
checkout. A transient DB blip surfaces as a non-zero exit code; systemd's
``OnFailure`` plus the timer's natural hourly cadence means a one-off
failure self-heals on the next tick. No retry loop in-process: a sweep
job that fails ten times in a row should escalate to alerting, not silently
absorb the failures.

Authority
---------
GREEN per ``projects/Resemblio/AUTHORITY.yml`` (read-mutate on
``idempotency_keys`` is a documented housekeeping action; rows pruned here
are by definition unreachable from the API's read path).
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.constants import IDEMPOTENCY_KEY_TTL_SECONDS
from app.db import SessionLocal
from app.models import IdempotencyKey


logger = logging.getLogger("resemblio.cli.sweep_idempotency")


@dataclass(frozen=True)
class SweepResult:
    """Outcome of one sweep invocation.

    ``deleted_count`` is the number of expired rows removed in this run.
    ``cutoff`` is the wall-clock instant the sweep used as the
    older-than boundary; a row's ``created_at < cutoff`` was the deletion
    criterion. ``schema_version`` lets log scrapers and downstream
    operators detect shape changes if this dataclass ever grows fields.
    """

    deleted_count: int
    cutoff: datetime
    schema_version: str = "sweep_idempotency_result_v1"


def _compute_cutoff(now: datetime) -> datetime:
    """Return the older-than boundary for the sweep.

    Anything created strictly before ``now - IDEMPOTENCY_KEY_TTL_SECONDS``
    is expired and safe to delete. Carved out as its own function so the
    test suite can pin ``now`` without monkey-patching ``datetime``.
    """
    return now - timedelta(seconds=IDEMPOTENCY_KEY_TTL_SECONDS)


def sweep_expired(session: Session, now: datetime | None = None) -> SweepResult:
    """Delete idempotency rows older than the TTL and return the count.

    The query uses the indexed ``created_at`` column so deletion cost is
    O(expired_count) regardless of table size. ``synchronize_session=False``
    is appropriate because no other ORM-mapped instances in this session
    are tracking these rows (the sweep opens a fresh session).

    On an empty table or all-fresh table the ``DELETE`` matches zero rows
    and commits cleanly; this is the no-op happy path and not an error.
    """
    effective_now = now if now is not None else datetime.now(timezone.utc)
    cutoff = _compute_cutoff(effective_now)
    stmt = delete(IdempotencyKey).where(IdempotencyKey.created_at < cutoff)
    result = session.execute(stmt)
    session.commit()
    deleted = int(result.rowcount or 0)
    return SweepResult(deleted_count=deleted, cutoff=cutoff)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns a shell exit code.

    Logging is configured to stdout so journald captures the deletion
    count line under ``journalctl -u resemblio-idempotency-sweep``. The
    log line shape is intentionally stable so an operator alert can grep
    for ``sweep_idempotency_complete`` without parsing prose.
    """
    del argv  # no flags in v1; preserved for future --dry-run wiring
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    session = SessionLocal()
    try:
        result = sweep_expired(session)
    except Exception:  # pragma: no cover - logged-and-reraised for journald
        logger.exception("sweep_idempotency_failed")
        session.rollback()
        return 1
    finally:
        session.close()
    logger.info(
        "sweep_idempotency_complete deleted=%d cutoff=%s schema_version=%s",
        result.deleted_count,
        result.cutoff.isoformat(),
        result.schema_version,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via systemd
    raise SystemExit(main(sys.argv[1:]))

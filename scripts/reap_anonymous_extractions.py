"""Reap expired anonymous-extraction registry rows.

Daily cleanup script for Stage O1. Walks ``anonymous_extractions`` and
flips ``status="expired"`` on any row whose ``expires_at`` has passed
while still ``status="pending"``. Old rows (>30 days past expiry) are
hard-deleted to keep the table compact; the underlying
``extractions`` rows stay (they carry billing audit history even when
the claim_token expired).

Run as: ``python -m scripts.reap_anonymous_extractions``

Cron suggestion: daily at 03:00 UTC via systemd timer. See
``deploy/scripts/`` for the canonical timer pattern used by the
backup automation.

schema_version: 1
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# scripts/ folder self-inserts its parent so `from app...` resolves
# without requiring `pip install -e .` in dev.
_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent.parent))

from sqlalchemy import delete, select, update  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import AnonymousExtraction  # noqa: E402


REAP_HARD_DELETE_AFTER_DAYS = 30
SCHEMA_VERSION = 1


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def reap(now: datetime | None = None) -> tuple[int, int]:
    """Flip expired rows to ``status='expired'`` and hard-delete old ones.

    Returns ``(expired_count, deleted_count)``. The expired-flip is
    idempotent: a second run flips no rows. The hard-delete is bounded
    by ``REAP_HARD_DELETE_AFTER_DAYS`` so an operator can audit recently
    expired claim_tokens.
    """
    moment = now or utcnow()
    hard_delete_cutoff = moment - timedelta(days=REAP_HARD_DELETE_AFTER_DAYS)
    with SessionLocal() as session:
        expired_result = session.execute(
            update(AnonymousExtraction)
            .where(
                AnonymousExtraction.expires_at < moment,
                AnonymousExtraction.status == "pending",
            )
            .values(status="expired")
        )
        deleted_result = session.execute(
            delete(AnonymousExtraction).where(
                AnonymousExtraction.expires_at < hard_delete_cutoff,
            )
        )
        session.commit()
        return (expired_result.rowcount or 0, deleted_result.rowcount or 0)


def main() -> int:
    """CLI entry. Logs counts and exits 0; non-zero only on hard error."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("reap_anonymous_extractions")
    expired, deleted = reap()
    log.info("reap_complete schema_version=%d expired=%d hard_deleted=%d", SCHEMA_VERSION, expired, deleted)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

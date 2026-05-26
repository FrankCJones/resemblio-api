"""One-shot backfill: provision Stripe customers for users missing one.

THROWAWAY SCRIPT. Resolves the v1.1 S2 stripe_customer_missing gap for users
created before the auto-provisioning flow landed. Iterates users where
stripe_customer_id IS NULL, calls the Stripe gateway for each, commits per
user so a mid-run failure does not lose earlier progress.

Run from `code/api`:
    python scripts/backfill_stripe_customers.py
    python scripts/backfill_stripe_customers.py --dry-run

Safe to re-run; users that already have an id are skipped.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings, validate_startup_settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import User  # noqa: E402
from app.payments import StripeClient  # noqa: E402
from app.users import provision_stripe_customer  # noqa: E402

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse CLI args for the throwaway backfill script."""
    parser = argparse.ArgumentParser(description="Backfill Stripe customers for users missing one")
    parser.add_argument("--dry-run", action="store_true", help="List affected users without calling Stripe")
    return parser.parse_args()


def main() -> int:
    """Provision a Stripe customer for every user where stripe_customer_id IS NULL."""
    logging.basicConfig(level=logging.INFO)
    args = parse_args()
    settings = get_settings()
    validate_startup_settings(settings)
    succeeded = 0
    failed = 0
    with SessionLocal() as session:
        users = session.query(User).filter(User.stripe_customer_id.is_(None)).all()
        logger.info("backfill candidates count=%s", len(users))
        if args.dry_run:
            for user in users:
                print(f"would provision user_id={user.id} email={user.email}")
            return 0
        stripe_service = StripeClient(settings)
        for user in users:
            ok = provision_stripe_customer(session, user, stripe_service)
            if ok:
                session.commit()
                succeeded += 1
                logger.info("provisioned user_id=%s customer_id=%s", user.id, user.stripe_customer_id)
            else:
                session.rollback()
                failed += 1
    logger.info("backfill complete succeeded=%s failed=%s", succeeded, failed)
    print(f"succeeded={succeeded} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

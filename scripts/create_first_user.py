"""Create a development user and starter API key.

Run from `code/api` after migrations:
    python scripts/create_first_user.py frank@optsus.com
"""
from __future__ import annotations

import argparse
import logging
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings, validate_startup_settings  # noqa: E402
from app.constants import DEFAULT_API_SCOPE  # noqa: E402
from app.crypto import generate_api_key, hash_password  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import ApiKey, ApiKeyEvent, User  # noqa: E402
from app.payments import StripeClient  # noqa: E402
from app.users import ensure_onboarding_grant, provision_stripe_customer  # noqa: E402

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse CLI args for the local-only seeding helper."""
    parser = argparse.ArgumentParser(description="Create the first Resemblio API user")
    parser.add_argument("email", nargs="?")
    parser.add_argument("-" + "-email", dest="email_flag", default=None)
    parser.add_argument("-p", "-" + "-password", dest="password", default=None)
    parser.add_argument("-l", "-" + "-label", dest="label", default="dev seed")
    return parser.parse_args()


def main() -> int:
    """Seed a user, onboarding grant, and starter API key."""
    logging.basicConfig(level=logging.INFO)
    args = parse_args()
    email = (args.email or args.email_flag or "").lower()
    if not email:
        raise SystemExit("email is required")
    settings = get_settings()
    validate_startup_settings(settings)
    stripe_service = StripeClient(settings)
    password = args.password or secrets.token_urlsafe(18)
    with SessionLocal() as session:
        user = session.query(User).filter(User.email == email).first()
        if user is None:
            user = User(email=email, password_hash=hash_password(password), status="active")
            session.add(user)
            session.flush()
        stripe_ok = provision_stripe_customer(session, user, stripe_service)
        if not stripe_ok:
            logger.warning(
                "stripe customer not provisioned for user=%s; topup will reject until /v1/account/provision-stripe is run",
                user.email,
            )
        ensure_onboarding_grant(session, user)
        plaintext, digest, prefix = generate_api_key("live")
        api_key = ApiKey(
            user_id=user.id,
            key_hash=digest,
            key_prefix=prefix,
            label=args.label,
            scopes=[DEFAULT_API_SCOPE],
        )
        session.add(api_key)
        session.flush()
        session.add(ApiKeyEvent(api_key_id=api_key.id, event_type="created", metadata_json={"source": "create_first_user"}))
        session.commit()
        print(f"email={user.email}")
        print(f"password={password}")
        print(f"api_key={plaintext}")
        print(f"key_prefix={prefix}")
    logger.info("created or updated first user email=%s", email)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

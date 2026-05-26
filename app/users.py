"""User creation helpers shared by seed scripts and future signup routes."""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.constants import ONBOARDING_GRANT_CENTS
from app.crypto import hash_password
from app.models import CreditLedger, User
from app.payments import StripeGateway
from app.routes.account import credit_balance

logger = logging.getLogger(__name__)


def create_user_with_customer_and_grant(
    session: Session,
    email: str,
    password: str,
    stripe_service: StripeGateway,
) -> User:
    """Create a user, Stripe TEST customer, and onboarding grant in one flow."""
    normalized_email = email.strip().lower()
    existing = session.query(User).filter(User.email == normalized_email).first()
    if existing is not None:
        raise ValueError("user already exists")
    stripe_customer_id = stripe_service.create_customer(normalized_email)
    user = User(
        email=normalized_email,
        password_hash=hash_password(password),
        stripe_customer_id=stripe_customer_id,
        status="active",
    )
    session.add(user)
    session.flush()
    session.add(
        CreditLedger(
            user_id=user.id,
            entry_type="onboarding_grant",
            amount_cents=ONBOARDING_GRANT_CENTS,
            balance_after_cents=ONBOARDING_GRANT_CENTS,
            note="Signup onboarding grant",
        )
    )
    session.flush()
    return user


def ensure_user_has_stripe_customer(session: Session, user: User, stripe_service: StripeGateway) -> None:
    """Backfill a missing Stripe TEST customer id for an existing user.

    Raises whatever the Stripe gateway raises. For soft-fail behavior at
    user-creation time, use ``provision_stripe_customer`` instead.
    """
    if user.stripe_customer_id:
        return
    user.stripe_customer_id = stripe_service.create_customer(user.email)
    session.flush()


def provision_stripe_customer(session: Session, user: User, stripe_service: StripeGateway) -> bool:
    """Best-effort Stripe customer provisioning during user creation.

    Returns True if the customer id is set on the user after the call, False
    if Stripe failed and the id remains null. Failures are logged at warning
    level so the surrounding flow can continue. A retry path is expected via
    a future ``/v1/account/provision-stripe`` endpoint.
    """
    if user.stripe_customer_id:
        return True
    try:
        user.stripe_customer_id = stripe_service.create_customer(user.email)
    except Exception as exc:  # noqa: BLE001 - Stripe SDK raises several transient subclasses; we tolerate any failure here.
        logger.warning(
            "stripe customer provisioning failed during user creation user_id=%s email=%s error=%s",
            user.id,
            user.email,
            exc.__class__.__name__,
        )
        return False
    session.flush()
    return True


def create_user_with_optional_customer(
    session: Session,
    email: str,
    password: str,
    stripe_service: StripeGateway,
) -> tuple[User, bool]:
    """Create a user and best-effort Stripe customer plus onboarding grant.

    Unlike ``create_user_with_customer_and_grant``, this entry point never
    fails user creation when Stripe is unavailable. Returns ``(user, stripe_ok)``
    so callers can log or surface the deferred-provisioning state.
    """
    normalized_email = email.strip().lower()
    existing = session.query(User).filter(User.email == normalized_email).first()
    if existing is not None:
        raise ValueError("user already exists")
    user = User(
        email=normalized_email,
        password_hash=hash_password(password),
        status="active",
    )
    session.add(user)
    session.flush()
    stripe_ok = provision_stripe_customer(session, user, stripe_service)
    ensure_onboarding_grant(session, user)
    return user, stripe_ok


def ensure_onboarding_grant(session: Session, user: User) -> None:
    """Append the onboarding grant if this user has no credit history yet."""
    if credit_balance(session, user.id) != 0:
        return
    session.add(
        CreditLedger(
            user_id=user.id,
            entry_type="onboarding_grant",
            amount_cents=ONBOARDING_GRANT_CENTS,
            balance_after_cents=ONBOARDING_GRANT_CENTS,
            note="Signup onboarding grant",
        )
    )
    session.flush()

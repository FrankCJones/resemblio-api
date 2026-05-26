"""User creation helpers shared by seed scripts and future signup routes."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.constants import ONBOARDING_GRANT_CENTS
from app.crypto import hash_password
from app.models import CreditLedger, User
from app.payments import StripeGateway
from app.routes.account import credit_balance


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
    """Backfill a missing Stripe TEST customer id for an existing user."""
    if user.stripe_customer_id:
        return
    user.stripe_customer_id = stripe_service.create_customer(user.email)
    session.flush()


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

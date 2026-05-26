"""Tests for app.users user-creation helpers and Stripe auto-provisioning.

Covers the contract introduced for the v1.1 S2 stripe_customer_missing gap:
user creation must succeed regardless of Stripe availability, but should
auto-provision a Stripe customer when the gateway is healthy. The /credit/topup
endpoint rejects users with stripe_customer_id IS NULL, so a follow-up
provision-stripe endpoint is expected to retry failed provisioning. These
tests pin both happy- and failure-path behavior so the soft-fail contract
does not regress.
"""
from __future__ import annotations

import logging

import pytest
from sqlalchemy.orm import Session

from app.payments import StripeGateway
from app.users import (
    create_user_with_optional_customer,
    provision_stripe_customer,
)


class _HealthyStripe:
    """StripeGateway double that returns a deterministic customer id."""

    def __init__(self, customer_id: str = "cus_test_healthy") -> None:
        """Store the id this fake will return from ``create_customer``."""
        self.customer_id = customer_id
        self.calls: list[str] = []

    def create_customer(self, email: str) -> str:
        """Record the call and return the configured customer id."""
        self.calls.append(email)
        return self.customer_id

    def create_checkout_session(self, user_id: int, stripe_customer_id: str, amount_cents: int):  # pragma: no cover - unused here
        """Unused in these tests; present to satisfy the gateway protocol."""
        raise NotImplementedError


class _FailingStripe:
    """StripeGateway double that always raises, simulating an outage."""

    def __init__(self) -> None:
        """Track call count so tests can assert the gateway was invoked."""
        self.calls: list[str] = []

    def create_customer(self, email: str) -> str:
        """Record the call and raise, mimicking Stripe SDK failures."""
        self.calls.append(email)
        raise RuntimeError("simulated Stripe outage")

    def create_checkout_session(self, user_id: int, stripe_customer_id: str, amount_cents: int):  # pragma: no cover - unused here
        """Unused in these tests; present to satisfy the gateway protocol."""
        raise NotImplementedError


def _gateway(stripe: object) -> StripeGateway:
    """Cast a duck-typed double to the gateway Protocol for type-checkers."""
    return stripe  # type: ignore[return-value]


def test_new_user_gets_stripe_customer_when_gateway_healthy(session: Session) -> None:
    """A healthy Stripe gateway returns a customer id and the user row stores it."""
    stripe = _HealthyStripe(customer_id="cus_test_new_user")
    user, stripe_ok = create_user_with_optional_customer(
        session,
        email="new-user@example.com",
        password="correct-horse-battery-staple",
        stripe_service=_gateway(stripe),
    )
    session.commit()
    assert stripe_ok is True
    assert user.id is not None
    assert user.stripe_customer_id == "cus_test_new_user"
    assert stripe.calls == ["new-user@example.com"]


def test_new_user_created_without_stripe_when_gateway_fails(session: Session) -> None:
    """A failing Stripe gateway leaves stripe_customer_id null and logs a warning."""
    from app import users as users_module

    captured: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _ListHandler(level=logging.DEBUG)
    users_module.logger.addHandler(handler)
    previous_level = users_module.logger.level
    previous_disabled = users_module.logger.disabled
    users_module.logger.setLevel(logging.DEBUG)
    users_module.logger.disabled = False
    try:
        stripe = _FailingStripe()
        user, stripe_ok = create_user_with_optional_customer(
            session,
            email="outage@example.com",
            password="correct-horse-battery-staple",
            stripe_service=_gateway(stripe),
        )
        session.commit()
    finally:
        users_module.logger.removeHandler(handler)
        users_module.logger.setLevel(previous_level)
        users_module.logger.disabled = previous_disabled
    assert stripe_ok is False
    assert user.id is not None
    assert user.stripe_customer_id is None
    assert stripe.calls == ["outage@example.com"]
    warning_messages = [record.getMessage() for record in captured if record.levelno == logging.WARNING]
    assert any("stripe customer provisioning failed" in message for message in warning_messages), warning_messages


def test_provision_stripe_customer_is_idempotent_when_already_set(session: Session) -> None:
    """If the user already has a customer id, the gateway is not called again."""
    stripe = _HealthyStripe(customer_id="cus_test_should_not_be_used")
    user, _ = create_user_with_optional_customer(
        session,
        email="idempotent@example.com",
        password="correct-horse-battery-staple",
        stripe_service=_gateway(_HealthyStripe(customer_id="cus_test_first")),
    )
    session.commit()
    result = provision_stripe_customer(session, user, _gateway(stripe))
    assert result is True
    assert user.stripe_customer_id == "cus_test_first"
    assert stripe.calls == []


def test_create_user_with_optional_customer_rejects_duplicate_email(session: Session) -> None:
    """Re-creating a user by email raises ValueError before touching Stripe."""
    stripe_first = _HealthyStripe(customer_id="cus_test_first_dup")
    create_user_with_optional_customer(
        session,
        email="dup@example.com",
        password="correct-horse-battery-staple",
        stripe_service=_gateway(stripe_first),
    )
    session.commit()
    stripe_second = _HealthyStripe(customer_id="cus_test_second_dup")
    with pytest.raises(ValueError, match="user already exists"):
        create_user_with_optional_customer(
            session,
            email="dup@example.com",
            password="correct-horse-battery-staple",
            stripe_service=_gateway(stripe_second),
        )
    assert stripe_second.calls == []

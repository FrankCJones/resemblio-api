"""Stripe helpers for customer, checkout, and webhook work.

Mode (TEST vs LIVE) is determined by ``settings.stripe_mode`` and enforced at
startup by ``app.config.validate_startup_settings``. This module makes the
same Stripe API calls in either mode; the bound restricted key decides which
Stripe environment responds.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar

from app.config import Settings, get_settings
from app.constants import STRIPE_RETRY_DELAYS_SECONDS
from app.schemas import StripeEventEnvelope

logger = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass(frozen=True)
class CheckoutSessionResult:
    """Stripe Checkout session fields returned to API callers."""

    id: str
    url: str


class StripeGateway(Protocol):
    """Protocol implemented by real and fake Stripe clients."""

    def create_customer(self, email: str) -> str:
        """Create a Stripe customer and return its id."""
        ...

    def create_checkout_session(self, user_id: int, stripe_customer_id: str, amount_cents: int) -> CheckoutSessionResult:
        """Create a Checkout session for a credit top-up."""
        ...


class StripeSignatureError(ValueError):
    """Raised when a Stripe webhook signature cannot be verified."""


class StripeClient:
    """Small wrapper around the Stripe SDK using the configured restricted key.

    The mode (TEST or LIVE) is determined by ``settings.stripe_mode`` and the
    matching key value bound to ``STRIPE_RESTRICTED_KEY_RESEMBLIO_TEST`` (alias
    name is fixed; value carries the mode).
    """

    def __init__(self, settings: Settings) -> None:
        """Store settings without importing Stripe until the first API call."""
        if settings.stripe_restricted_key is None:
            raise RuntimeError("STRIPE_RESTRICTED_KEY_RESEMBLIO_TEST is required")
        self._api_key = settings.stripe_restricted_key
        self._success_url = settings.topup_success_url
        self._cancel_url = settings.topup_cancel_url

    def create_customer(self, email: str) -> str:
        """Create a Stripe customer with retry and return the customer id."""
        stripe = _stripe_module()

        def _call() -> object:
            stripe.api_key = self._api_key
            return stripe.Customer.create(email=email)

        try:
            customer = _with_retries(_call, "customer.create")
        except Exception as exc:  # noqa: BLE001 - Preserve Stripe detail behind a clear signup error.
            raise RuntimeError("Stripe customer creation failed after retries") from exc
        customer_id = getattr(customer, "id", None) or _mapping_value(customer, "id")
        if not customer_id:
            raise RuntimeError("Stripe customer creation did not return an id")
        return str(customer_id)

    def create_checkout_session(self, user_id: int, stripe_customer_id: str, amount_cents: int) -> CheckoutSessionResult:
        """Create a Stripe Checkout session for credit top-up."""
        stripe = _stripe_module()

        def _call() -> object:
            stripe.api_key = self._api_key
            # Card-only for v1.1. Delayed-payment methods (SEPA debit, bank
            # debit, OXXO, etc.) emit checkout.session.completed with
            # payment_status in ("unpaid", "processing"); the fulfillment trigger
            # for those is checkout.session.async_payment_succeeded, which v1.1
            # does not yet handle. Restricting payment_method_types here keeps
            # the credit-grant path on the strict synchronous-paid contract the
            # webhook handler enforces. v1.2 will add delayed-method support
            # alongside subscription billing.
            # Reference: https://docs.stripe.com/payments/checkout/fulfill-orders
            return stripe.checkout.Session.create(
                mode="payment",
                customer=stripe_customer_id,
                payment_method_types=["card"],
                success_url=self._success_url,
                cancel_url=self._cancel_url,
                line_items=[
                    {
                        "price_data": {
                            "currency": "usd",
                            "product_data": {"name": "Resemblio credit top-up"},
                            "unit_amount": amount_cents,
                        },
                        "quantity": 1,
                    }
                ],
                metadata={"user_id": str(user_id), "purpose": "credit_topup"},
            )

        try:
            session = _with_retries(_call, "checkout.session.create")
        except Exception as exc:  # noqa: BLE001 - Preserve Stripe detail behind a clear checkout error.
            raise RuntimeError("Stripe Checkout session creation failed after retries") from exc
        session_id = getattr(session, "id", None) or _mapping_value(session, "id")
        session_url = getattr(session, "url", None) or _mapping_value(session, "url")
        if not session_id or not session_url:
            raise RuntimeError("Stripe Checkout did not return id and url")
        return CheckoutSessionResult(id=str(session_id), url=str(session_url))


def construct_stripe_event(payload: bytes, signature_header: str, webhook_secret: str, tolerance_seconds: int = 300) -> StripeEventEnvelope:
    """Verify a Stripe webhook signature and parse the event envelope."""
    timestamp = _signature_timestamp(signature_header)
    if abs(int(time.time()) - timestamp) > tolerance_seconds:
        raise StripeSignatureError("Stripe webhook timestamp outside tolerance")
    expected = _compute_signature(webhook_secret, timestamp, payload)
    signatures = _signature_values(signature_header)
    if not signatures or not any(hmac.compare_digest(expected, signature) for signature in signatures):
        raise StripeSignatureError("Stripe webhook signature mismatch")
    try:
        raw_event = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise StripeSignatureError("Stripe webhook body is not valid JSON") from exc
    return StripeEventEnvelope.model_validate(raw_event)


def get_stripe_service() -> StripeGateway:
    """FastAPI dependency returning the configured Stripe client.

    Mode (TEST or LIVE) follows ``settings.stripe_mode`` and the bound
    restricted key. See ``app.config.validate_startup_settings``.
    """
    return StripeClient(get_settings())


def _stripe_module() -> object:
    """Import Stripe lazily so tests can use fakes without the SDK."""
    try:
        import stripe  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("Install the stripe package before using Stripe API calls") from exc
    return stripe


def _with_retries(call: Callable[[], T], operation: str) -> T:
    """Run a Stripe call with exponential backoff and no secret logging."""
    last_error: Exception | None = None
    for index, delay in enumerate(STRIPE_RETRY_DELAYS_SECONDS):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - Stripe SDK raises several transient subclasses.
            last_error = exc
            if index == len(STRIPE_RETRY_DELAYS_SECONDS) - 1:
                break
            logger.warning("Stripe operation failed; retrying operation=%s attempt=%s", operation, index + 1)
            time.sleep(delay)
    assert last_error is not None
    raise last_error


def _mapping_value(value: object, key: str) -> object | None:
    """Read a key from StripeObject-like mappings used by SDK and tests."""
    if isinstance(value, dict):
        return value.get(key)
    get = getattr(value, "get", None)
    if callable(get):
        return get(key)
    return None


def _signature_timestamp(header: str) -> int:
    """Extract the timestamp from a Stripe-Signature header."""
    for part in header.split(","):
        name, _, value = part.partition("=")
        if name == "t" and value:
            return int(value)
    raise StripeSignatureError("Stripe webhook signature missing timestamp")


def _signature_values(header: str) -> list[str]:
    """Extract all v1 signatures from a Stripe-Signature header."""
    values: list[str] = []
    for part in header.split(","):
        name, _, value = part.partition("=")
        if name == "v1" and value:
            values.append(value)
    return values


def _compute_signature(secret: str, timestamp: int, payload: bytes) -> str:
    """Compute Stripe's HMAC SHA-256 payload signature."""
    signed_payload = f"{timestamp}.".encode("utf-8") + payload
    return hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()

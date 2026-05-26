"""Transactional email helpers for Resemblio account events."""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Protocol, TypeVar

from app.config import Settings, get_settings
from app.constants import RESEND_RETRY_DELAYS_SECONDS

logger = logging.getLogger(__name__)
T = TypeVar("T")


class EmailSender(Protocol):
    """Protocol implemented by real and fake transactional email senders."""

    def send_topup_cleared(self, to_email: str, amount_cents: int, balance_cents: int) -> None:
        """Send a credit top-up cleared email."""
        ...


EmailSenderFactory = Callable[[], EmailSender]


class ResendEmailSender:
    """Resend API sender for transactional emails."""

    def __init__(self, settings: Settings) -> None:
        """Store Resend settings without making a network call."""
        if not settings.resend_api_key:
            raise RuntimeError("RESEND_API_KEY is required to send transactional emails")
        self._api_key = settings.resend_api_key
        self._from_email = settings.resend_from_email

    def send_topup_cleared(self, to_email: str, amount_cents: int, balance_cents: int) -> None:
        """Send the Stripe top-up success message through Resend."""
        amount = _format_usd(amount_cents)
        balance = _format_usd(balance_cents)
        subject = f"Your Resemblio top-up of {amount} has cleared"
        text = f"Your top-up of {amount} has cleared; new balance {balance}."
        payload = {
            "from": self._from_email,
            "to": [to_email],
            "subject": subject,
            "text": text,
        }
        body = json.dumps(payload).encode("utf-8")

        def _call() -> None:
            request = urllib.request.Request(
                "https://api.resend.com/emails",
                data=body,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.getcode()
                if status >= 400:
                    raise RuntimeError(f"Resend returned status {status}")

        _with_retries(_call, "topup_cleared")


def get_email_sender() -> EmailSender:
    """FastAPI dependency returning the transactional email sender."""
    return ResendEmailSender(get_settings())


def get_email_sender_factory() -> EmailSenderFactory:
    """FastAPI dependency returning a lazy sender factory."""
    return get_email_sender


def _with_retries(call: Callable[[], T], operation: str) -> T:
    """Run a Resend call with exponential backoff and no secret logging."""
    last_error: Exception | None = None
    for index, delay in enumerate(RESEND_RETRY_DELAYS_SECONDS):
        try:
            return call()
        except (urllib.error.URLError, RuntimeError) as exc:
            last_error = exc
            if index == len(RESEND_RETRY_DELAYS_SECONDS) - 1:
                break
            logger.warning("Resend operation failed; retrying operation=%s attempt=%s", operation, index + 1)
            time.sleep(delay)
    assert last_error is not None
    raise last_error


def _format_usd(amount_cents: int) -> str:
    """Format cents as plain USD for transactional copy."""
    return f"${amount_cents / 100:.2f}"

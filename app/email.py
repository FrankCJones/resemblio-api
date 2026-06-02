"""Transactional email helpers for Resemblio account events."""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Protocol, TypedDict, TypeVar

from app.config import Settings, get_settings
from app.constants import (
    AUTO_REFUND_EMAIL_BODY_TEMPLATE,
    AUTO_REFUND_EMAIL_SUBJECT,
    AUTO_REFUND_SUPPORT_EMAIL,
    MAGIC_LINK_EMAIL_BODY_TEMPLATE,
    MAGIC_LINK_EMAIL_SUBJECT,
    MAGIC_LINK_EXPIRY_MINUTES,
    RESEND_RETRY_DELAYS_SECONDS,
)


class AutoRefundEmailPayload(TypedDict):
    """Shape of the rendered low-quality auto-refund email payload.

    Mirrors the JSON body posted to Resend's /emails endpoint. Kept as a
    TypedDict (not a dataclass) so the dict can be passed straight to
    ``json.dumps`` without an intermediate conversion step.
    """

    from_: str
    to: list[str]
    subject: str
    text: str

logger = logging.getLogger(__name__)
T = TypeVar("T")

# Cloudflare sits in front of api.resend.com and rejects User-Agent strings
# matching automation-bot heuristics (Python-urllib/3.x triggers error 1010,
# a 403 with no body forwarded by Resend itself). Setting an explicit
# product-branded User-Agent satisfies the gate. Verified 2026-06-02 during
# Wave 2c Step 3 magic-link smoke (Frank's signup email never arrived;
# direct curl from the same box returned 200 because curl's UA passes the
# gate). Workspace lock-in candidate: any urllib.request call hitting a
# Cloudflare-fronted API needs an explicit User-Agent.
RESEND_USER_AGENT = "Resemblio/1.0 (+https://resemblio.com; transactional-email)"


class EmailSender(Protocol):
    """Protocol implemented by real and fake transactional email senders."""

    def send_topup_cleared(self, to_email: str, amount_cents: int, balance_cents: int) -> None:
        """Send a credit top-up cleared email."""
        ...

    def send_low_quality_auto_refund(
        self,
        to_email: str,
        amount_cents: int,
        source_url: str,
    ) -> None:
        """Send the S20 R4 auto-refund-on-low-quality customer notification."""
        ...

    def send_magic_link(self, to_email: str, link: str) -> None:
        """Send the passwordless sign-in magic link to ``to_email``."""
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
                    "User-Agent": RESEND_USER_AGENT,
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.getcode()
                if status >= 400:
                    raise RuntimeError(f"Resend returned status {status}")

        _with_retries(_call, "topup_cleared")

    def send_low_quality_auto_refund(
        self,
        to_email: str,
        amount_cents: int,
        source_url: str,
    ) -> None:
        """Send the S20 R4 auto-refund notification through Resend.

        Edge case: the route handler is responsible for catching any exception
        this method raises and recording ``email_status="failed"`` on the
        audit row. The refund itself must not be blocked by an email send
        failure (Resend outage cannot strand a customer's credit).
        """
        amount = _format_usd(amount_cents)
        text = AUTO_REFUND_EMAIL_BODY_TEMPLATE.format(
            source_url=source_url,
            amount=amount,
            support_email=AUTO_REFUND_SUPPORT_EMAIL,
        )
        payload: AutoRefundEmailPayload = {
            "from_": self._from_email,
            "to": [to_email],
            "subject": AUTO_REFUND_EMAIL_SUBJECT,
            "text": text,
        }
        # Resend's API uses ``from`` not ``from_``; we keep the TypedDict key
        # as ``from_`` because ``from`` is a Python keyword, then rewrite at
        # the boundary.
        wire = {"from": payload["from_"], "to": payload["to"], "subject": payload["subject"], "text": payload["text"]}
        body = json.dumps(wire).encode("utf-8")

        def _call() -> None:
            request = urllib.request.Request(
                "https://api.resend.com/emails",
                data=body,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": RESEND_USER_AGENT,
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.getcode()
                if status >= 400:
                    raise RuntimeError(f"Resend returned status {status}")

        _with_retries(_call, "low_quality_auto_refund")

    def send_magic_link(self, to_email: str, link: str) -> None:
        """Send a passwordless sign-in magic link through Resend.

        The link is the fully-formed click target the recipient lands on;
        the API does NOT log it (the link IS the credential). Retries use
        the same exponential-backoff helper as other Resend calls.
        """
        text = MAGIC_LINK_EMAIL_BODY_TEMPLATE.format(
            link=link, minutes=MAGIC_LINK_EXPIRY_MINUTES
        )
        payload = {
            "from": self._from_email,
            "to": [to_email],
            "subject": MAGIC_LINK_EMAIL_SUBJECT,
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
                    "User-Agent": RESEND_USER_AGENT,
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.getcode()
                if status >= 400:
                    raise RuntimeError(f"Resend returned status {status}")

        _with_retries(_call, "magic_link")


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

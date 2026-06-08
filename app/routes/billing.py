"""Internal billing routes called by the Next.js BFF (S3b Wave 2c).

Surface
-------
``POST /v1/internal/billing/create_checkout_session`` - feature-flag-gated
creation of a Stripe Checkout session for a credit top-up on behalf of a
logged-in dashboard user. Mirrors the public ``POST /v1/credit/topup`` but
authenticates via the internal-secret + user_id pattern rather than the
user's Bearer key. The BFF holds the user's identity (the session cookie ->
in-memory session record -> user id mapping), not the user's API key with
billing scope; this internal surface is what bridges that gap without
exposing a billing-capable key to the browser.

Feature flag
------------
``RESEMBLIO_BILLING_UI_ENABLED`` is read at request time (NOT at boot) so a
flip via systemd ``Environment=`` + restart takes effect immediately on the
next request, without code change. When unset or anything other than the
literal string ``"true"`` (case-insensitive), the route returns 503 with
``error="billing_ui_disabled"``. The flag ships without a manual card test;
the first real customer transaction serves as the live test. Structured logs
at every failure path are the monitoring instrument for first-transaction
issues (see ``Resemblio_BUILD_LOG.md`` 2026-06-07 unvalidated-path entry).

Stripe Checkout success URL convention
--------------------------------------
The webhook handler at ``app/routes/webhooks.py`` is unchanged. On
``checkout.session.completed`` the existing handler claims the
``TopupSession`` row (created by this route) and credits the user's ledger.
This route only creates the Checkout session and the matching server-side
``TopupSession`` row; it does NOT credit anything.
"""
from __future__ import annotations

import logging
import os
import secrets

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app.config import Settings, get_settings
from app.constants import (
    BILLING_UI_FLAG_ENV_VAR,
    SCHEMA_V1,
    TOPUP_BUNDLE_ACCEPTED_PAID_CENTS,
)
from app.db import get_db
from app.models import TopupSession, User
from app.payments import StripeCustomerModeError, StripeGateway, get_stripe_service
from app.schemas import InternalCheckoutSessionRequest, InternalCheckoutSessionResponse

router = APIRouter()
logger = logging.getLogger(__name__)


def _billing_ui_enabled() -> bool:
    """Return True iff the ``RESEMBLIO_BILLING_UI_ENABLED`` env flag is set.

    Read at request time so a systemd env-flip + restart takes effect on the
    next request. The match is case-insensitive on the literal "true"; any
    other value (empty, absent, "false", "1", "yes") is treated as disabled.
    """
    raw = os.environ.get(BILLING_UI_FLAG_ENV_VAR, "")
    return raw.strip().lower() == "true"


def _internal_secret_ok(provided: str | None, settings: Settings) -> bool:
    """Constant-time compare the supplied internal-auth header to the configured secret.

    Mirrors the helper in ``app/routes/internal_auth.py`` rather than importing
    it to keep the routing modules decoupled. Fail-closed when the configured
    secret is unset.
    """
    expected = settings.internal_auth_secret
    if not expected or provided is None:
        return False
    return secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _mask_session_id(session_id: str) -> str:
    """Return a log-safe truncation of a Stripe Checkout session id.

    Stripe Checkout session ids are not by themselves credentials, but logging
    only the first eight characters is the convention used everywhere else in
    the codebase (see ``BFF_KEY_LOG_PREFIX_CHARS`` and the Stripe runbook
    Section 5). Matches the dispatch brief instruction to mask
    ``cs_live_<first-8>***``.
    """
    if not session_id:
        return "<empty>"
    return session_id[:12] + "***" if len(session_id) > 12 else session_id


def _error(status_code: int, code: str, extra: dict[str, object] | None = None) -> JSONResponse:
    """Build a contract-shaped JSON error response with schema_version=SCHEMA_V1."""
    body: dict[str, object] = {"error": code, "schema_version": SCHEMA_V1}
    if extra:
        body.update(extra)
    return JSONResponse(status_code=status_code, content=body)


@router.post(
    "/internal/billing/create_checkout_session",
    response_model=InternalCheckoutSessionResponse,
)
def create_checkout_session(
    payload: InternalCheckoutSessionRequest,
    session: Session = Depends(get_db),
    stripe_service: StripeGateway = Depends(get_stripe_service),
    x_internal_auth: str | None = Header(default=None, alias="X-Internal-Auth"),
) -> InternalCheckoutSessionResponse | JSONResponse:
    """Create a Stripe Checkout session for the given user + bundle amount.

    Args:
        payload: Bundle amount and user identifier. ``amount_cents`` is
            validated against the closed bundle-tier set; any other value
            returns 400.
        session: SQLAlchemy session for the ``TopupSession`` insert.
        stripe_service: Gateway dependency; the test suite replaces this
            with a fake (see ``test_internal_billing_checkout.py``).
        x_internal_auth: Shared-secret header. Must equal
            ``settings.internal_auth_secret`` (constant-time compare).

    Returns:
        Either ``InternalCheckoutSessionResponse`` (200) with the Stripe
        Checkout URL + session id, or a ``JSONResponse`` carrying the
        documented error code (503 / 401 / 400 / 404 / 409).

    Edge cases:
        - 503 ``billing_ui_disabled`` when ``RESEMBLIO_BILLING_UI_ENABLED``
          is not the literal string "true". The flag ships without a manual
          card test; first real transaction = live test.
        - 503 ``internal_auth_unconfigured`` when the API process has no
          ``RESEMBLIO_INTERNAL_AUTH_SECRET`` configured (fail-closed).
        - 401 ``internal_auth_invalid`` when the secret header is missing
          or wrong.
        - 400 ``amount_not_in_bundle_set`` when ``amount_cents`` is not in
          ``TOPUP_BUNDLE_ACCEPTED_PAID_CENTS``.
        - 404 ``user_not_found`` when the supplied user_id has no row.
        - 409 ``stripe_customer_missing`` when the user has no
          ``stripe_customer_id`` (signup is supposed to populate this).
        - 409 ``customer_mode_mismatch`` when the customer id was minted in
          the wrong Stripe mode (operator runs
          ``tools/resemblio_customer_reconcile.sh`` to fix).
    """
    if not _billing_ui_enabled():
        # No log: expected state when flag is off; would generate noise on
        # every request during normal pre-flip operations.
        return _error(503, "billing_ui_disabled")
    settings = get_settings()
    if settings.internal_auth_secret is None:
        # Operator config error; log at ERROR so it surfaces in the alert
        # channel even before the first real transaction attempts.
        logger.error("billing_checkout: internal_auth_unconfigured")
        return _error(503, "internal_auth_unconfigured")
    if not _internal_secret_ok(x_internal_auth, settings):
        # Security-relevant: log at WARNING but include NO header values to
        # avoid leaking the probing party's attempt token into the log stream.
        logger.warning("billing_checkout: internal_auth_invalid")
        return _error(401, "internal_auth_invalid")

    if payload.amount_cents not in TOPUP_BUNDLE_ACCEPTED_PAID_CENTS:
        # Tampered request or a BFF bug; log the attempted amount for forensics.
        logger.warning(
            "billing_checkout: amount_not_in_bundle_set amount=%s",
            payload.amount_cents,
        )
        return _error(
            400,
            "amount_not_in_bundle_set",
            extra={"allowed_cents": sorted(TOPUP_BUNDLE_ACCEPTED_PAID_CENTS)},
        )

    db_user = session.get(User, payload.user_id)
    if db_user is None:
        # Should not happen in normal BFF flow; log user_id for reconciliation.
        logger.warning("billing_checkout: user_not_found user_id=%s", payload.user_id)
        return _error(404, "user_not_found")
    if not db_user.stripe_customer_id:
        # Signup is supposed to populate this; log for reconciliation.
        logger.warning(
            "billing_checkout: stripe_customer_missing user_id=%s", db_user.id
        )
        return _error(409, "stripe_customer_missing")

    try:
        checkout = stripe_service.create_checkout_session(
            db_user.id, db_user.stripe_customer_id, payload.amount_cents
        )
    except StripeCustomerModeError:
        # Customer id is bound to the wrong Stripe mode. Operator runs the
        # reconciliation script (see the typed exception's docstring for
        # the incident reference) to fix; surface a 409 so the BFF can
        # render a stable copy line rather than a 500.
        logger.error(
            "billing_checkout: customer_mode_mismatch user_id=%s", db_user.id
        )
        return _error(409, "customer_mode_mismatch")

    # Bind the Stripe session id to (user_id, amount) via TopupSession BEFORE
    # returning. The webhook handler refuses to credit any session id that
    # lacks a matching row, so this insert is what keeps a hostile actor
    # from credit-stuffing via a fabricated session id. Mirrors the public
    # /v1/credit/topup path verbatim.
    session.add(
        TopupSession(
            id=checkout.id,
            user_id=db_user.id,
            amount_cents=payload.amount_cents,
            status="pending",
        )
    )
    session.commit()
    logger.info(
        "internal_checkout_created user_id=%s amount_cents=%s session_id=%s",
        db_user.id,
        payload.amount_cents,
        _mask_session_id(checkout.id),
    )
    return InternalCheckoutSessionResponse(
        checkout_url=checkout.url,
        session_id=checkout.id,
        schema_version=SCHEMA_V1,
    )

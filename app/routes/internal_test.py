"""Test-only internal endpoints that gate the O9 Playwright E2E suite.

Two routes live here:

* ``GET  /v1/internal/auth/test_get_latest_magic_link?email=<>`` returns the
  latest unconsumed plaintext magic-link token for the supplied email so
  the Playwright suite can synthesize a redeem click without scraping a
  real inbox.
* ``POST /v1/internal/test/teardown_user`` body ``{email}`` deletes the user
  and every child row a Playwright run could have left behind (magic-link
  tokens, web-session keys, anonymous-extraction registry rows pointing
  at extractions the user owned, owned extractions, api keys). Idempotent:
  a second call against an already-deleted email returns
  ``{ok: true, deleted_rows: 0}``.

Gate posture
------------
Both endpoints are DARK BY DEFAULT. They return 403 ``test_auth_disabled``
unless ``RESEMBLIO_TEST_AUTH_ENABLED`` is exactly the string ``"1"`` AND
``RESEMBLIO_TEST_AUTH_TOKEN`` is set. They return 401 ``test_auth_invalid``
when the ``X-Test-Auth`` header is absent or does not match the env token.

WARNING: enabling these on a prod box is a critical safety violation. The
combination of plaintext-token readback + a known email bypasses email-as-
second-factor for any account whose address is known to the caller; the
teardown surface is unconditional destructive delete. Both env vars must
remain unset on every prod box. The companion column
``magic_link_tokens.plaintext_token`` is only populated by
``request_magic_link`` when the flag is on; rows minted with the flag off
are NULL and the readback returns 404 even if the flag is later toggled.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app.config import Settings, get_settings
from app.db import get_db
from app.models import (
    AnonymousExtraction,
    ApiKey,
    ApiKeyEvent,
    AutoRefundAuditEvent,
    CreditLedger,
    Extraction,
    IdempotencyKey,
    MagicLinkToken,
    TopupSession,
    User,
    WebSessionKey,
)


# Bump together with the response dataclass shape. The Playwright harness
# branches on this integer so the E2E suite can survive an envelope change
# without a coordinated double-deploy.
TEST_MAGIC_LINK_SCHEMA_VERSION: int = 1
TEST_TEARDOWN_SCHEMA_VERSION: int = 1


router = APIRouter()
logger = logging.getLogger(__name__)


class TeardownUserBody(BaseModel):
    """Inbound payload for the teardown endpoint.

    Email is the only key; the deletion is keyed on the user row matching
    ``email`` after lowercasing. Unknown email is not an error (idempotent
    contract); the response just reports ``deleted_rows=0``.
    """

    email: str = Field(min_length=3, max_length=320)


@dataclass(frozen=True)
class TestMagicLinkResponse:
    """Plaintext-token readback response. ``schema_version`` is leading-1."""

    schema_version: int
    token: str
    expires_at: str
    email: str

    def to_dict(self) -> dict[str, str | int]:
        """Return the response body as a plain JSON-serializable dict."""
        return asdict(self)


@dataclass(frozen=True)
class TestTeardownResponse:
    """Teardown ack response. ``deleted_rows`` is the total across all tables."""

    schema_version: int
    ok: bool
    deleted_rows: int

    def to_dict(self) -> dict[str, int | bool]:
        """Return the response body as a plain JSON-serializable dict."""
        return asdict(self)


def _error(status_code: int, code: str) -> JSONResponse:
    """Build a contract-shaped JSON error response matching the auth surface."""
    return JSONResponse(status_code=status_code, content={"error": code})


def _enabled(settings: Settings) -> bool:
    """Return True only if BOTH env vars are set per the gate contract.

    ``RESEMBLIO_TEST_AUTH_ENABLED`` must be exactly the string ``"1"`` (any
    other truthy value is treated as off so a typo never opens the gate)
    and ``RESEMBLIO_TEST_AUTH_TOKEN`` must be a non-empty string.
    """
    return settings.test_auth_enabled == "1" and bool(settings.test_auth_token)


def _header_ok(provided: str | None, settings: Settings) -> bool:
    """Constant-time compare of the supplied ``X-Test-Auth`` against the env token."""
    expected = settings.test_auth_token
    if not expected or provided is None:
        return False
    return secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _format_datetime(value: datetime) -> str:
    """Render a datetime as ISO-8601 for the JSON response.

    Returns naive datetimes unchanged (SQLite fixture path); aware values
    are rendered with their tz suffix. The Playwright harness only treats
    this as an opaque debug string.
    """
    return value.isoformat()


@router.get("/internal/auth/test_get_latest_magic_link")
def test_get_latest_magic_link(
    email: str = Query(..., min_length=3, max_length=320),
    session: Session = Depends(get_db),
    x_test_auth: str | None = Header(default=None, alias="X-Test-Auth"),
) -> JSONResponse:
    """Return the latest unconsumed plaintext magic-link token for ``email``.

    Gated by the test-auth env flags + ``X-Test-Auth`` header (see module
    docstring). Returns 404 ``no_unconsumed_token`` when no row matches;
    the lookup excludes rows with ``plaintext_token IS NULL`` so prod-mode
    rows minted before the flag was toggled cannot leak via a later flag
    flip.
    """
    settings = get_settings()
    if not _enabled(settings):
        return _error(403, "test_auth_disabled")
    if not _header_ok(x_test_auth, settings):
        return _error(401, "test_auth_invalid")

    normalized_email = email.lower()
    row = session.execute(
        select(MagicLinkToken)
        .where(MagicLinkToken.email == normalized_email)
        .where(MagicLinkToken.consumed_at.is_(None))
        .where(MagicLinkToken.plaintext_token.is_not(None))
        .order_by(MagicLinkToken.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None or row.plaintext_token is None:
        return _error(404, "no_unconsumed_token")

    # Log without the token value; the token IS the credential. The email
    # is logged because the test-auth surface is dark on prod by env-flag
    # gate, so any hit here is a developer or CI run.
    logger.info(
        "test_magic_link_readback email=%s token_id=%s",
        normalized_email,
        row.id,
    )
    response = TestMagicLinkResponse(
        schema_version=TEST_MAGIC_LINK_SCHEMA_VERSION,
        token=row.plaintext_token,
        expires_at=_format_datetime(row.expires_at),
        email=normalized_email,
    )
    return JSONResponse(status_code=200, content=response.to_dict())


@router.post("/internal/test/teardown_user")
def test_teardown_user(
    payload: TeardownUserBody,
    session: Session = Depends(get_db),
    x_test_auth: str | None = Header(default=None, alias="X-Test-Auth"),
) -> JSONResponse:
    """Delete a user and every child row a Playwright run could have created.

    Idempotent: an unknown email returns ``{ok: true, deleted_rows: 0}``.
    The delete fan-out covers:

    * ``magic_link_tokens`` by email (these are not FK'd to ``users``;
      they may exist for an email that never completed signup).
    * ``api_keys`` owned by the user (and their ``api_key_events`` rows,
      which are FK-cascaded by deleting the key rows via ORM session so
      the events row referencing it goes too; we delete events explicitly
      to keep the contract dialect-agnostic since the FK lacks an ON
      DELETE CASCADE clause).
    * ``web_session_keys`` owned by the user.
    * ``anonymous_extractions`` whose ``extraction_id`` points at an
      extraction owned by the user. The registry row carries no direct
      user FK; we walk through extractions.
    * ``extractions`` owned by the user (and their ``credit_ledger`` rows,
      same FK-without-cascade dance as api_key_events).
    * ``users`` row itself.

    Returns the total row count flipped to deleted state. The count is
    informational for the Playwright harness; the contract is that a
    subsequent call against the same email returns 0.
    """
    settings = get_settings()
    if not _enabled(settings):
        return _error(403, "test_auth_disabled")
    if not _header_ok(x_test_auth, settings):
        return _error(401, "test_auth_invalid")

    normalized_email = payload.email.lower()
    total_deleted = 0

    # Magic-link tokens are looked up by email so we can reap them even when
    # the user row was never created (e.g. signup abandoned after request).
    ml_result = session.execute(
        delete(MagicLinkToken).where(MagicLinkToken.email == normalized_email)
    )
    total_deleted += ml_result.rowcount or 0

    user = session.execute(
        select(User).where(User.email == normalized_email)
    ).scalar_one_or_none()
    if user is None:
        session.commit()
        response = TestTeardownResponse(
            schema_version=TEST_TEARDOWN_SCHEMA_VERSION,
            ok=True,
            deleted_rows=total_deleted,
        )
        return JSONResponse(status_code=200, content=response.to_dict())

    user_id = user.id

    # Pull api_key ids first so we can fan out to ApiKeyEvent.
    api_key_ids = [
        row[0]
        for row in session.execute(
            select(ApiKey.id).where(ApiKey.user_id == user_id)
        ).all()
    ]
    if api_key_ids:
        ev_result = session.execute(
            delete(ApiKeyEvent).where(ApiKeyEvent.api_key_id.in_(api_key_ids))
        )
        total_deleted += ev_result.rowcount or 0

    # WebSessionKey FK points at api_keys; delete it before the api_keys row.
    ws_result = session.execute(
        delete(WebSessionKey).where(WebSessionKey.user_id == user_id)
    )
    total_deleted += ws_result.rowcount or 0

    # Pull extraction ids so we can delete anonymous_extractions and the
    # credit_ledger fan-out before the extractions rows themselves.
    extraction_ids = [
        row[0]
        for row in session.execute(
            select(Extraction.id).where(Extraction.user_id == user_id)
        ).all()
    ]
    if extraction_ids:
        ae_result = session.execute(
            delete(AnonymousExtraction).where(
                AnonymousExtraction.extraction_id.in_(extraction_ids)
            )
        )
        total_deleted += ae_result.rowcount or 0
        cl_result = session.execute(
            delete(CreditLedger).where(CreditLedger.extraction_id.in_(extraction_ids))
        )
        total_deleted += cl_result.rowcount or 0
        ar_result = session.execute(
            delete(AutoRefundAuditEvent).where(
                AutoRefundAuditEvent.extraction_id.in_(extraction_ids)
            )
        )
        total_deleted += ar_result.rowcount or 0
        ex_result = session.execute(
            delete(Extraction).where(Extraction.id.in_(extraction_ids))
        )
        total_deleted += ex_result.rowcount or 0

    # Idempotency keys + topup sessions reference users.id directly; sweep
    # them before the user row goes so the FK does not strand.
    ik_result = session.execute(
        delete(IdempotencyKey).where(IdempotencyKey.user_id == user_id)
    )
    total_deleted += ik_result.rowcount or 0
    ts_result = session.execute(
        delete(TopupSession).where(TopupSession.user_id == user_id)
    )
    total_deleted += ts_result.rowcount or 0

    # Credit ledger may also carry user-scoped non-extraction rows (the
    # onboarding grant is one such). Sweep them after the extraction-scoped
    # rows so the FK on extraction_id no longer dangles for any of them.
    cl_user_result = session.execute(
        delete(CreditLedger).where(CreditLedger.user_id == user_id)
    )
    total_deleted += cl_user_result.rowcount or 0

    if api_key_ids:
        ak_result = session.execute(
            delete(ApiKey).where(ApiKey.id.in_(api_key_ids))
        )
        total_deleted += ak_result.rowcount or 0

    user_result = session.execute(delete(User).where(User.id == user_id))
    total_deleted += user_result.rowcount or 0

    session.commit()
    logger.info(
        "test_teardown_user email=%s user_id=%s deleted_rows=%s",
        normalized_email,
        user_id,
        total_deleted,
    )
    response = TestTeardownResponse(
        schema_version=TEST_TEARDOWN_SCHEMA_VERSION,
        ok=True,
        deleted_rows=total_deleted,
    )
    return JSONResponse(status_code=200, content=response.to_dict())

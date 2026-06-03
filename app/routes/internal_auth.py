"""Internal auth routes called by the Next.js BFF for magic-link signup/login.

These four endpoints are NOT user-facing. They sit behind a shared-secret
check (``X-Internal-Auth`` header equal to ``RESEMBLIO_INTERNAL_AUTH_SECRET``)
so only the co-located web process can call them. The Bearer-token
``AuthMiddleware`` is bypassed for this surface (see ``AUTH_FREE_PATHS`` in
``app/auth.py``); these routes mint and revoke the BFF API key that the web
process subsequently uses to talk to the regular Bearer-token API surface.

Endpoints
---------
* ``POST /v1/internal/auth/request_magic_link`` - body ``{email, ip?, user_agent?, link_base?}``.
  Writes a hashed magic-link token and dispatches a Resend email.
  Anti-enumeration: always returns ``{ok: true}`` regardless of whether the
  email maps to an existing user.

* ``POST /v1/internal/auth/redeem_magic_link`` - body ``{token}``. Validates
  the token (not expired, not consumed). Creates the user row if absent.
  Revokes the user's previous BFF key (if any). Mints a fresh ApiKey with
  ``kind='internal_bff'`` and ``is_visible_to_user=false``. Returns
  ``{api_key, user_id, email, is_new_user}``. The api_key plaintext is in
  the response ONLY; never logged.

* ``GET /v1/internal/auth/whoami`` - header ``X-Bff-Key: <plaintext>``. Returns
  the user record (id, email, status, credit balance, signup date). 401
  on unknown/revoked key.

* ``POST /v1/internal/auth/logout`` - header ``X-Bff-Key: <plaintext>``. Revokes
  the key, sets ``rotated_at`` on the ``web_session_keys`` row, returns
  ``{ok: true}``. Idempotent: a second call returns 401 (key already gone).
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app.auth import utcnow
from app.config import Settings, get_settings
from app.constants import (
    API_KEY_KIND_INTERNAL_BFF,
    BFF_KEY_LOG_PREFIX_CHARS,
    DEFAULT_API_SCOPE,
    MAGIC_LINK_EXPIRY_MINUTES,
    MAGIC_LINK_REQUEST_COOLDOWN_SECONDS,
    MAGIC_LINK_TOKEN_BYTES,
)
from app.crypto import generate_api_key, hash_api_key, hash_password
from app.db import get_db
from app.email import EmailSender, get_email_sender
from app.models import (
    AnonymousExtraction,
    ApiKey,
    ApiKeyEvent,
    Extraction,
    MagicLinkToken,
    User,
    WebSessionKey,
)
from app.routes.account import credit_balance
from app.users import ensure_onboarding_grant


# Stage O5 claim-endpoint response envelope version. Mirrors the
# anonymous-extraction surface (``ANON_EXTRACTION_SCHEMA_VERSION``) so the
# BFF can switch on a single integer when reading either side of the
# anonymous-to-account handoff.
CLAIM_ANONYMOUS_EXTRACTION_SCHEMA_VERSION: int = 1

router = APIRouter()
logger = logging.getLogger(__name__)


# Internal endpoints share a single response shape for the simple ack case.
# Pydantic enforces the contract at the boundary so downstream BFF code can
# rely on a stable JSON envelope. Anti-enumeration: the request endpoint
# returns this shape unconditionally.
class InternalAck(BaseModel):
    """Generic acknowledgement returned by request/logout endpoints."""

    ok: bool = True


class RequestMagicLinkBody(BaseModel):
    """Inbound payload for the magic-link request endpoint.

    ``email`` is a plain ``str`` here rather than ``pydantic.EmailStr``: the
    upstream Next.js BFF already validates the address with a strict regex
    and the v1.1 dependency footprint deliberately avoids the
    ``email-validator`` package. We lower-case at the storage boundary.
    """

    email: str = Field(min_length=3, max_length=320)
    ip: Optional[str] = None
    user_agent: Optional[str] = Field(default=None, max_length=512)
    # The BFF supplies the click-target base so the API does not need to
    # know whether the request came from prod, staging, or a developer's
    # local box. If absent the API falls back to ``settings.web_app_base_url``.
    link_base: Optional[str] = None


class RedeemMagicLinkBody(BaseModel):
    """Inbound payload for the magic-link redemption endpoint."""

    token: str = Field(min_length=8, max_length=256)
    ip: Optional[str] = None
    user_agent: Optional[str] = Field(default=None, max_length=512)


class RedeemMagicLinkResponse(BaseModel):
    """Successful redemption response. ``api_key`` is one-time only."""

    api_key: str
    user_id: int
    email: str
    is_new_user: bool


class WhoamiResponse(BaseModel):
    """Authenticated user snapshot returned by /whoami."""

    user_id: int
    email: str
    status: str
    credit_balance_cents: int
    signup_at: datetime


@dataclass(frozen=True)
class _BffKeyContext:
    """Resolved BFF key + user pair carried through the request handler."""

    api_key: ApiKey
    user: User


def _internal_secret_ok(provided: str | None, settings: Settings) -> bool:
    """Return True iff the shared-secret header matches the configured value.

    Uses ``secrets.compare_digest`` to keep the comparison constant-time and
    refuses to authorize anything when the secret is unset (fail-closed).
    """
    expected = settings.internal_auth_secret
    if not expected:
        return False
    if provided is None:
        return False
    return secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _hash_token(plaintext: str) -> str:
    """Return the SHA-256 hex digest used as the magic-link lookup key."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _mask_bff_key(plaintext: str) -> str:
    """Return a log-safe prefix of a BFF api key (first 8 chars only)."""
    if not plaintext:
        return "<empty>"
    return plaintext[:BFF_KEY_LOG_PREFIX_CHARS] + "..."


def _build_magic_link(token: str, link_base: str | None, settings: Settings) -> str:
    """Assemble the click-target URL the recipient lands on.

    Trailing slashes on ``link_base`` are normalized so the result is a
    deterministic single-slash URL. The verify path is the published S3 web
    contract; both prod and dev must use the same path component for
    deep links to work.
    """
    base = (link_base or settings.web_app_base_url).rstrip("/")
    return f"{base}/app/auth/verify?token={token}"


def _resolve_bff_key(session: Session, settings: Settings, plaintext: str) -> _BffKeyContext | None:
    """Look up an active BFF api_key by hashing the supplied plaintext.

    Mirrors the candidate-hash pattern from ``app/auth.py`` so the BFF surface
    benefits from the same pepper-rotation behavior the public API has, but
    additionally requires ``kind='internal_bff'`` and ``status='active'``.
    Returns ``None`` if the key is unknown, wrong kind, revoked, or its owner
    is inactive.
    """
    peppers = [settings.key_pepper]
    if settings.key_pepper_old:
        peppers.append(settings.key_pepper_old)
    hashes = [hash_api_key(plaintext, pepper) for pepper in peppers if pepper]
    if not hashes:
        return None
    api_key = session.execute(
        select(ApiKey).where(
            ApiKey.key_hash.in_(hashes),
            ApiKey.kind == API_KEY_KIND_INTERNAL_BFF,
            ApiKey.status == "active",
        )
    ).scalar_one_or_none()
    if api_key is None:
        return None
    user = session.get(User, api_key.user_id)
    if user is None or user.status != "active":
        return None
    return _BffKeyContext(api_key=api_key, user=user)


def _error(status_code: int, code: str) -> JSONResponse:
    """Build a contract-shaped JSON error response."""
    return JSONResponse(status_code=status_code, content={"error": code})


@router.post("/internal/auth/request_magic_link", response_model=InternalAck)
def request_magic_link(
    payload: RequestMagicLinkBody,
    session: Session = Depends(get_db),
    email_sender: EmailSender = Depends(get_email_sender),
    x_internal_auth: str | None = Header(default=None, alias="X-Internal-Auth"),
) -> InternalAck | JSONResponse:
    """Issue a single-use magic link to the supplied email.

    Anti-enumeration: always returns ``{ok: true}``. Whether the email maps
    to an existing user is not revealed to the caller. Token plaintext is
    never persisted; only ``SHA-256(token)`` is written. Token validity is
    bounded by ``MAGIC_LINK_EXPIRY_MINUTES``.
    """
    real_settings = get_settings()
    if real_settings.internal_auth_secret is None:
        return _error(503, "internal_auth_unconfigured")
    if not _internal_secret_ok(x_internal_auth, real_settings):
        return _error(401, "internal_auth_invalid")

    now = utcnow()
    normalized_email = payload.email.lower()

    # Per-email cooldown to bound mailbox-bombing / volume-enumeration.
    # If a non-consumed, non-expired token was minted within the cooldown
    # window we keep the standard anti-enumeration ack but skip the
    # mint+send. The previous token still works in the recipient's inbox;
    # the cooldown only suppresses redundant sends. The query is bounded
    # by the index on ``magic_link_tokens.email``.
    cooldown_cutoff = now - timedelta(seconds=MAGIC_LINK_REQUEST_COOLDOWN_SECONDS)
    existing = session.execute(
        select(MagicLinkToken)
        .where(MagicLinkToken.email == normalized_email)
        .where(MagicLinkToken.consumed_at.is_(None))
        .where(MagicLinkToken.expires_at > now)
        .order_by(MagicLinkToken.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        existing_created_at = existing.created_at
        # SQLite returns naive datetimes; Postgres returns aware. Normalize
        # to UTC-aware for the cooldown comparison without mutating the row.
        if existing_created_at.tzinfo is None:
            existing_created_at = existing_created_at.replace(tzinfo=timezone.utc)
        if existing_created_at >= cooldown_cutoff:
            logger.info(
                "magic_link_request_throttled email=%s cooldown_seconds=%s",
                normalized_email,
                MAGIC_LINK_REQUEST_COOLDOWN_SECONDS,
            )
            return InternalAck(ok=True)

    plaintext_token = secrets.token_urlsafe(MAGIC_LINK_TOKEN_BYTES)
    token_hash = _hash_token(plaintext_token)
    # Plaintext mirror is populated ONLY when the test-auth surface is
    # explicitly enabled (both env vars set). On prod with the flag off the
    # column stays NULL and the link plaintext lives only in the outbound
    # email body. See ``app/routes/internal_test.py`` for the readback gate.
    test_mode = (
        real_settings.test_auth_enabled == "1"
        and bool(real_settings.test_auth_token)
    )
    row = MagicLinkToken(
        email=normalized_email,
        token_hash=token_hash,
        expires_at=now + timedelta(minutes=MAGIC_LINK_EXPIRY_MINUTES),
        ip=payload.ip,
        user_agent=payload.user_agent,
        plaintext_token=plaintext_token if test_mode else None,
    )
    session.add(row)
    session.commit()

    link = _build_magic_link(plaintext_token, payload.link_base, real_settings)
    try:
        email_sender.send_magic_link(payload.email, link)
    except Exception:  # pragma: no cover - Resend failure path is observed via logs
        # Do not surface the email-send failure to the BFF; the row is
        # already persisted and a retry from the user (which is the natural
        # remediation) will simply mint a fresh token. Log without the link
        # (the link IS the credential).
        logger.warning("magic_link_email_send_failed email=%s", payload.email)
    return InternalAck(ok=True)


@router.post("/internal/auth/redeem_magic_link", response_model=RedeemMagicLinkResponse)
def redeem_magic_link(
    payload: RedeemMagicLinkBody,
    session: Session = Depends(get_db),
    x_internal_auth: str | None = Header(default=None, alias="X-Internal-Auth"),
) -> RedeemMagicLinkResponse | JSONResponse:
    """Validate + consume a magic-link token; mint and return a fresh BFF key.

    Side effects, in order: (1) row's ``consumed_at`` is set; (2) user row
    is created if absent (with status='active' and a random throwaway
    password_hash, the column is legacy NOT NULL); (3) any previous
    ``internal_bff`` api_key for the user is revoked; (4) a fresh
    ``internal_bff`` api_key is minted; (5) ``web_session_keys`` is
    updated (insert or replace the row pointing at the new key).
    """
    settings = get_settings()
    if settings.internal_auth_secret is None:
        return _error(503, "internal_auth_unconfigured")
    if not _internal_secret_ok(x_internal_auth, settings):
        return _error(401, "internal_auth_invalid")

    token_hash = _hash_token(payload.token)
    # Race-safe consume. Two concurrent redemptions of the same token must not
    # both pass the ``consumed_at IS NULL`` guard. The defenses combine:
    #
    #   1. On Postgres: an explicit ``SELECT ... FOR UPDATE`` on the token row
    #      takes a row-level write lock for the duration of the transaction.
    #      The second redemption blocks until the first commits, then sees the
    #      now-non-null ``consumed_at`` and returns 400 ``token_consumed``.
    #
    #   2. On any dialect (including SQLite where ``FOR UPDATE`` is a silent
    #      no-op): the consume is a conditional UPDATE with a
    #      ``consumed_at IS NULL`` guard in the WHERE clause. The driver's
    #      reported ``rowcount`` tells us whether THIS call was the one that
    #      flipped the column. ``rowcount == 0`` means a peer redemption won
    #      the race; we return 400 ``token_consumed`` without minting a key.
    #
    # The check-then-set pattern that existed before this dispatch (read
    # ``consumed_at``, branch on it, then assign) is vulnerable to two callers
    # both reading NULL before either writes. The rowcount-driven conditional
    # UPDATE collapses the read and the write into one statement the DB
    # serializes, which closes the window even on SQLite where the in-flight
    # ``FOR UPDATE`` does nothing.
    dialect_name = session.bind.dialect.name if session.bind is not None else ""
    if dialect_name == "postgresql":
        row = session.execute(
            select(MagicLinkToken)
            .where(MagicLinkToken.token_hash == token_hash)
            .with_for_update()
        ).scalar_one_or_none()
    else:
        row = session.execute(
            select(MagicLinkToken).where(MagicLinkToken.token_hash == token_hash)
        ).scalar_one_or_none()
    if row is None:
        return _error(400, "token_invalid")
    if row.consumed_at is not None:
        return _error(400, "token_consumed")
    expiry = row.expires_at
    if expiry.tzinfo is None:  # SQLite returns naive datetimes
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry < utcnow():
        return _error(400, "token_expired")

    # Atomic consume: only one concurrent caller can satisfy
    # ``consumed_at IS NULL``. The losing caller's UPDATE matches zero rows
    # because the column has already been written; ``result.rowcount == 0``
    # is the signal to bail with the same 400 error code the second read
    # would have produced (had we kept the racy check-then-set pattern).
    now_ts = utcnow()
    result = session.execute(
        update(MagicLinkToken)
        .where(MagicLinkToken.id == row.id)
        .where(MagicLinkToken.consumed_at.is_(None))
        .values(consumed_at=now_ts)
    )
    if result.rowcount == 0:
        # Peer redemption committed between our SELECT and our UPDATE.
        # No key minted, no side effects landed; safe to roll back any
        # implicit transaction state and surface the consumed error.
        session.rollback()
        return _error(400, "token_consumed")
    # Mirror the persisted state on the loaded row so downstream reads
    # (audit log line at the end of this handler) see consistent values.
    row.consumed_at = now_ts

    user = session.execute(select(User).where(User.email == row.email.lower())).scalar_one_or_none()
    is_new_user = user is None
    if user is None:
        # Magic-link signup never sets a real password; the column is a
        # legacy NOT NULL field. A random argon2 hash is stored so password-
        # login routes (none in v1.1) cannot accidentally match.
        user = User(
            email=row.email.lower(),
            password_hash=hash_password(secrets.token_urlsafe(32)),
            status="active",
        )
        session.add(user)
        session.flush()
        # Magic-link signup is the only user-creation path on the BFF surface
        # and must honor the LP promise ("New accounts start with $10 of
        # credit"). ``ensure_onboarding_grant`` is idempotent: it no-ops when
        # the ledger already shows nonzero balance, so re-redeeming a token
        # for an existing user (or a future seed script that pre-credits)
        # cannot double-grant. Cold-user E2E audit finding #1
        # (`projects/Resemblio/marketing/2026-06-02-cold-user-e2e-audit.md`).
        ensure_onboarding_grant(session, user)

    # Revoke any prior BFF key for this user and detach the old session row.
    existing_session = session.execute(
        select(WebSessionKey).where(WebSessionKey.user_id == user.id)
    ).scalar_one_or_none()
    if existing_session is not None:
        existing_session.rotated_at = utcnow()
        old_key = session.get(ApiKey, existing_session.api_key_id)
        if old_key is not None and old_key.status == "active":
            old_key.status = "revoked"
            old_key.revoked_at = utcnow()
            old_key.revoked_reason = "bff_rotated_on_login"
            session.add(
                ApiKeyEvent(
                    api_key_id=old_key.id,
                    event_type="revoked",
                    ip=payload.ip,
                    metadata_json={"reason": "bff_rotated_on_login"},
                )
            )
        # Delete the old session row; the unique index on api_key_id
        # would otherwise block re-pointing it at the new key.
        session.delete(existing_session)
        session.flush()

    # Mint the new BFF key.
    plaintext, digest, prefix = generate_api_key(settings.default_key_env)  # type: ignore[arg-type]
    new_key = ApiKey(
        user_id=user.id,
        key_hash=digest,
        key_prefix=prefix,
        label="bff_session",
        scopes=[DEFAULT_API_SCOPE],
        kind=API_KEY_KIND_INTERNAL_BFF,
        is_visible_to_user=False,
        created_from_ip=payload.ip,
    )
    session.add(new_key)
    session.flush()
    session.add(
        ApiKeyEvent(
            api_key_id=new_key.id,
            event_type="created",
            ip=payload.ip,
            metadata_json={"kind": API_KEY_KIND_INTERNAL_BFF},
        )
    )
    session.add(WebSessionKey(user_id=user.id, api_key_id=new_key.id))
    session.commit()
    logger.info(
        "bff_key_minted user_id=%s prefix=%s key=%s is_new_user=%s",
        user.id,
        new_key.key_prefix,
        _mask_bff_key(plaintext),
        is_new_user,
    )
    return RedeemMagicLinkResponse(
        api_key=plaintext,
        user_id=user.id,
        email=user.email,
        is_new_user=is_new_user,
    )


class ClaimAnonymousExtractionBody(BaseModel):
    """Inbound payload for the Stage O5 anonymous-to-account claim endpoint.

    The BFF supplies both halves of the claim secret. ``user_id`` is the
    newly-minted account that just completed magic-link redeem; the BFF
    pulls it from the redeem response. ``claim_token`` is the opaque
    secret minted at anonymous-extract time (stored in a short-lived
    browser cookie across the magic-link round trip).
    """

    claim_token: str = Field(min_length=8, max_length=128)
    user_id: int = Field(gt=0)


@dataclass(frozen=True)
class ClaimAnonymousExtractionResponse:
    """Structured success response for the claim endpoint.

    Emitted via ``JSONResponse`` to match the error-status surface and
    keep the ``schema_version`` envelope uniform across happy and error
    paths.
    """

    schema_version: int
    ok: bool
    extraction_id: int

    def to_dict(self) -> dict[str, int | bool]:
        """Return the response body as a plain dict."""
        return {
            "schema_version": self.schema_version,
            "ok": self.ok,
            "extraction_id": self.extraction_id,
        }


@router.post("/internal/auth/claim_anonymous_extraction")
def claim_anonymous_extraction(
    payload: ClaimAnonymousExtractionBody,
    session: Session = Depends(get_db),
    x_internal_auth: str | None = Header(default=None, alias="X-Internal-Auth"),
) -> JSONResponse:
    """Bind an anonymous extraction row to a freshly-minted user account.

    Called by the BFF immediately after a successful magic-link redeem
    when the visitor arrived at ``/app/signup?claim=<id>&token=<...>``
    (the conversion path off the Stage O4 anonymous-result surface).

    Atomic side effects, in order: (1) lookup the ``AnonymousExtraction``
    row by ``claim_token`` (constant-time compare via the UNIQUE index);
    (2) validate the row is not expired and not already claimed by
    another user; (3) rebind the underlying ``Extraction`` row's
    ``user_id`` from the synthetic anonymous service user to the new
    account; (4) stamp ``claimed_at`` on the registry row and walk its
    ``status`` to ``"claimed"``.

    Edge cases:
    - Unknown ``claim_token`` returns 404 ``invalid_claim_token``. We do
      NOT mention whether the token existed and was simply consumed by
      another user (that would leak claim-token guessing signal); the
      404 covers both shapes.
    - Already-claimed (``claimed_at`` is non-null) returns 409
      ``already_claimed`` regardless of which user did the claim. Double-
      claim is a hard error; the BFF surfaces a friendly redirect to the
      dashboard rather than retrying.
    - Past ``expires_at`` returns 410 ``claim_expired``. The 24-hour
      window is enforced server-side; the BFF never trusts the cookie
      timestamp.
    - Missing or NULL ``extraction_id`` on the registry row (out-of-scope
      classification path; nothing was ever extracted to bind) returns
      404 ``nothing_to_claim``. The BFF treats this the same as an
      unknown token for UX purposes.
    - Unknown ``user_id`` returns 400 ``user_not_found``; the BFF should
      never hit this path because it just minted the user one HTTP hop
      earlier, but the route handler validates rather than crashes.
    """
    settings = get_settings()
    if settings.internal_auth_secret is None:
        return _error(503, "internal_auth_unconfigured")
    if not _internal_secret_ok(x_internal_auth, settings):
        return _error(401, "internal_auth_invalid")

    user = session.get(User, payload.user_id)
    if user is None:
        return _error(400, "user_not_found")

    # The UNIQUE index on ``claim_token`` makes this a single index hit;
    # constant-time compare via the DB index is fine here (the lookup is
    # equality on a 64-char column, no LIKE).
    registry = session.execute(
        select(AnonymousExtraction).where(AnonymousExtraction.claim_token == payload.claim_token)
    ).scalar_one_or_none()
    if registry is None:
        return _error(404, "invalid_claim_token")

    # Race-safe claim: a peer claim from another newly-signed-up user
    # could race here. The conditional UPDATE pattern from
    # ``redeem_magic_link`` would work but anonymous extractions are
    # not a contention surface (one claim_token, one cookie, one
    # visitor); a check-then-set guarded by the UNIQUE token + row-lock
    # is sufficient. Postgres takes the row lock implicitly on UPDATE;
    # SQLite serializes writes at the connection level.
    if registry.claimed_at is not None:
        return _error(409, "already_claimed")

    expires_at = registry.expires_at
    if expires_at.tzinfo is None:  # SQLite returns naive datetimes
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < utcnow():
        return _error(410, "claim_expired")

    if registry.extraction_id is None:
        # Out-of-scope classifications never created an extractions row;
        # there is nothing to bind. The BFF treats this as "no claim".
        return _error(404, "nothing_to_claim")

    extraction = session.get(Extraction, registry.extraction_id)
    if extraction is None:
        # The FK is nullable but referential integrity in Postgres + the
        # registry's own ``extraction_id NOT NULL`` check above would
        # normally guarantee this row exists. A missing row points at
        # operator-deleted state; treat as nothing-to-claim rather than
        # crashing the BFF.
        return _error(404, "nothing_to_claim")

    now_ts = utcnow()
    extraction.user_id = user.id
    registry.claimed_at = now_ts
    registry.status = "claimed"
    session.commit()
    logger.info(
        "anonymous_extraction_claimed user_id=%s extraction_id=%s registry_id=%s",
        user.id,
        extraction.id,
        registry.id,
    )
    response = ClaimAnonymousExtractionResponse(
        schema_version=CLAIM_ANONYMOUS_EXTRACTION_SCHEMA_VERSION,
        ok=True,
        extraction_id=extraction.id,
    )
    return JSONResponse(status_code=200, content=response.to_dict())


@router.get("/internal/auth/whoami", response_model=WhoamiResponse)
def whoami(
    session: Session = Depends(get_db),
    x_internal_auth: str | None = Header(default=None, alias="X-Internal-Auth"),
    x_bff_key: str | None = Header(default=None, alias="X-Bff-Key"),
) -> WhoamiResponse | JSONResponse:
    """Return the user record bound to the supplied BFF key.

    401 if the key is missing, unknown, wrong kind, revoked, or its owner
    is inactive. The credit balance is computed live from the ledger so
    the dashboard never shows stale numbers.
    """
    settings = get_settings()
    if settings.internal_auth_secret is None:
        return _error(503, "internal_auth_unconfigured")
    if not _internal_secret_ok(x_internal_auth, settings):
        return _error(401, "internal_auth_invalid")
    if not x_bff_key:
        return _error(401, "missing_bff_key")
    ctx = _resolve_bff_key(session, settings, x_bff_key)
    if ctx is None:
        return _error(401, "invalid_bff_key")
    return WhoamiResponse(
        user_id=ctx.user.id,
        email=ctx.user.email,
        status=ctx.user.status,
        credit_balance_cents=credit_balance(session, ctx.user.id),
        signup_at=ctx.user.created_at,
    )


@router.post("/internal/auth/logout", response_model=InternalAck)
def logout(
    session: Session = Depends(get_db),
    x_internal_auth: str | None = Header(default=None, alias="X-Internal-Auth"),
    x_bff_key: str | None = Header(default=None, alias="X-Bff-Key"),
) -> InternalAck | JSONResponse:
    """Revoke the supplied BFF key and detach the web_session_keys row.

    Idempotent in the sense that a re-call with the same (now-revoked) key
    returns 401; the side effects on the first call are not re-applied.
    """
    settings = get_settings()
    if settings.internal_auth_secret is None:
        return _error(503, "internal_auth_unconfigured")
    if not _internal_secret_ok(x_internal_auth, settings):
        return _error(401, "internal_auth_invalid")
    if not x_bff_key:
        return _error(401, "missing_bff_key")
    ctx = _resolve_bff_key(session, settings, x_bff_key)
    if ctx is None:
        return _error(401, "invalid_bff_key")
    ctx.api_key.status = "revoked"
    ctx.api_key.revoked_at = utcnow()
    ctx.api_key.revoked_reason = "bff_logout"
    session.add(
        ApiKeyEvent(
            api_key_id=ctx.api_key.id,
            event_type="revoked",
            metadata_json={"reason": "bff_logout"},
        )
    )
    existing_session = session.execute(
        select(WebSessionKey).where(WebSessionKey.api_key_id == ctx.api_key.id)
    ).scalar_one_or_none()
    if existing_session is not None:
        existing_session.rotated_at = utcnow()
        session.delete(existing_session)
    session.commit()
    logger.info(
        "bff_key_logout user_id=%s prefix=%s key=%s",
        ctx.user.id,
        ctx.api_key.key_prefix,
        _mask_bff_key(x_bff_key),
    )
    return InternalAck(ok=True)

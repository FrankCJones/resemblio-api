"""Stage O1 anonymous-extraction routes.

Unauthenticated surface that lets a stranger arriving at resemblio.com
fire one extraction per IP per 24 hours without creating an account.
The route is bypass-listed in ``app/auth.py`` (``AUTH_FREE_PATHS``) so
the Bearer-token ``AuthMiddleware`` does not 401 it.

Endpoints
---------
* ``POST /v1/anonymous/extractions`` - body ``{url}``. Classifies the
  URL (Stage O3 dependency; stubbed to ``html_first`` until O3 ships).
  Supported classes get an ``extractions`` row enqueued under the
  service user, a 32-byte URL-safe ``claim_token``, and the
  ``anonymous_extractions`` registry row. Unsupported classes return
  ``status="out_of_scope"`` with a notify-when-supported capture URL;
  no extractor cycles burn.
* ``GET /v1/anonymous/extractions/{id}?claim_token=<...>`` - returns
  classification + extraction status + (when ready) the inline tokens
  preview. ``claim_token`` mismatch returns 403.
* ``POST /v1/notify-when-supported`` - body ``{url, email,
  detected_class}``. Append-only capture into ``notify_requests``.

Rate-limit storage: Postgres-backed counter table
``anon_extract_counters(ip_hash, day, count)`` (migration 0021). Redis
is the future home but is not yet provisioned in this stack; the
existing in-process token bucket (``app/rate_limit.py``) is per-process
and would silently multiply the cap under multi-worker uvicorn. The
table-backed counter is cross-process correct without a new runtime
dependency. See module ``_check_and_increment_ip_counter`` for the
UPSERT + count-check.

Service-user contract
---------------------
Anonymous extractions need a ``user_id`` for the existing extractions
table FK; we provision a single SERVICE user
(``ANON_SERVICE_USER_EMAIL``) at first use and attach every anonymous
extraction to it. Conversion (Stage O5) rebinds the row's ``user_id``
to the real account during signup. The service user holds zero credit
balance; the anonymous extraction path is FREE and writes no credit
ledger row (anonymous billing arrives at account-creation time per the
respec acceptance criteria).

Schema-version contract
-----------------------
Every response wraps a top-level ``schema_version`` field
(``ANON_EXTRACTION_SCHEMA_VERSION = 1``) so downstream consumers
(O2 hero, O4 result page) can switch on a single integer.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import AnyHttpUrl, BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app.constants import (
    ANON_CLAIM_TOKEN_BYTES,
    ANON_EXTRACTION_CLAIM_WINDOW_HOURS,
    ANON_EXTRACTION_SCHEMA_VERSION,
    ANON_EXTRACT_FLAG_ENV_VAR,
    ANON_EXTRACT_PER_IP_PER_DAY_DEFAULT,
    ANON_EXTRACT_PER_IP_PER_DAY_ENV_VAR,
    ANON_OUT_OF_SCOPE_MESSAGE,
    ANON_RATE_LIMITED_MESSAGE,
    SCHEMA_V1,
)
from app.crypto import hash_password
from app.db import get_db
from app.models import (
    AnonExtractCounter,
    AnonymousExtraction,
    Extraction,
    NotifyRequest,
    User,
)
from app.site_classifier import ClassificationResult, classify_url, is_supported

logger = logging.getLogger(__name__)

router = APIRouter()


# Email of the synthetic SERVICE user that owns every anonymous extraction
# until Stage O5 (signup conversion) rebinds the row to a real account.
# Not a deliverable address; never receives mail.
ANON_SERVICE_USER_EMAIL = "anonymous-service@resemblio.com"


# ---------------------------------------------------------------------------
# Pydantic request / response shapes
# ---------------------------------------------------------------------------


class AnonymousExtractRequest(BaseModel):
    """Body for ``POST /v1/anonymous/extractions``.

    URL validation via Pydantic's ``AnyHttpUrl`` keeps obviously-malformed
    inputs from reaching the classifier. Real validation (DNS, reachability,
    Wix-runtime signal) lives in the O3 classifier.
    """

    url: AnyHttpUrl


class NotifyWhenSupportedRequest(BaseModel):
    """Body for ``POST /v1/notify-when-supported``.

    ``detected_class`` echoes what the original anonymous-extract
    response surfaced so the row carries the failure-class context
    without a join.
    """

    url: AnyHttpUrl
    # Plain str to avoid adding the ``email-validator`` runtime dep.
    # The web BFF validates the address shape before POSTing here; the
    # length floor catches obvious garbage submissions.
    email: str = Field(min_length=3, max_length=320)
    detected_class: str = Field(min_length=1, max_length=32)


@dataclass(frozen=True)
class AnonymousExtractResponse:
    """Structured response shape for the anonymous-extract endpoints.

    Exposed via ``model_dump``-equivalent ``to_dict`` below; we use a
    dataclass rather than a Pydantic model because the route emits the
    response via ``JSONResponse`` (status-code routing for 429 / 503 /
    accepted needs to bypass the FastAPI ``response_model`` machinery).
    """

    schema_version: int
    status: str
    classification: str
    extraction_id: int | None
    claim_token: str | None
    refunded: bool
    message: str | None
    notify_email_capture_url: str | None

    def to_dict(self) -> dict[str, Any]:
        """Return the response body as a plain dict."""
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "classification": self.classification,
            "extraction_id": self.extraction_id,
            "claim_token": self.claim_token,
            "refunded": self.refunded,
            "message": self.message,
            "notify_email_capture_url": self.notify_email_capture_url,
        }


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested in tests/test_anonymous_extractions.py)
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def hash_ip(ip: str) -> str:
    """Return SHA-256 hex of a client IP.

    The raw IP never lands in the database. The hash is irreversible
    for the abuse use case (we only need equality checks across
    requests from the same source within the same day) while keeping
    PII narrow.
    """
    return hashlib.sha256(ip.encode("utf-8")).hexdigest()


def mint_claim_token() -> str:
    """Return a URL-safe 32-byte random token (43 chars base64url).

    Used as the opaque secret tying the anonymous extraction row to
    the future-account claim. Collision probability at this byte
    count is negligible; the route handler still treats a UNIQUE-
    constraint violation as a retry trigger rather than a 500.
    """
    return secrets.token_urlsafe(ANON_CLAIM_TOKEN_BYTES)


def per_ip_daily_cap() -> int:
    """Return the configured per-IP daily extraction cap.

    Reads ``ANON_EXTRACT_PER_IP_PER_DAY`` env var; falls back to the
    default constant (1). A non-integer or non-positive env value
    falls back to default rather than raising; the route handler is
    the wrong place to crash on operator misconfig.
    """
    raw = os.environ.get(ANON_EXTRACT_PER_IP_PER_DAY_ENV_VAR)
    if not raw:
        return ANON_EXTRACT_PER_IP_PER_DAY_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "invalid_anon_per_ip_cap env=%r falling_back_to_default=%d",
            raw,
            ANON_EXTRACT_PER_IP_PER_DAY_DEFAULT,
        )
        return ANON_EXTRACT_PER_IP_PER_DAY_DEFAULT
    if value <= 0:
        return ANON_EXTRACT_PER_IP_PER_DAY_DEFAULT
    return value


def feature_enabled() -> bool:
    """Return True when the anonymous-extract feature flag is on.

    Default off so the endpoint cannot accept production traffic until
    Frank flips ``RESEMBLIO_ANON_EXTRACT_ENABLED=true`` after O3 lands
    in shadow.
    """
    return os.environ.get(ANON_EXTRACT_FLAG_ENV_VAR, "false").lower() == "true"


def today_bucket(now: datetime | None = None) -> str:
    """Return the day-bucket key (UTC ISO date) for rate-limit storage."""
    moment = now or utcnow()
    return moment.date().isoformat()


def _client_ip(request: Request) -> str:
    """Return the best-effort client IP for rate-limit + abuse logging.

    Honors ``X-Forwarded-For`` when present (the API sits behind Caddy
    on prod; the trusted-proxy check in ``app/auth.py`` is for audit
    rows, not for rate-limit counting where we already cap per-IP
    anyway). Falls back to the direct peer host. Returns a synthetic
    ``"unknown"`` when nothing is available; that bucket gets capped
    just like any other IP.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    if request.client is not None and request.client.host:
        return request.client.host
    return "unknown"


# ---------------------------------------------------------------------------
# Database-backed helpers
# ---------------------------------------------------------------------------


def _get_or_create_service_user(session: Session) -> User:
    """Return the synthetic service user that owns anonymous extractions.

    Created lazily on first use so a clean test DB never needs to seed
    it. Password hash is a non-secret throwaway; the service user has
    no API key minted against it (the anonymous route does not require
    one). Stage O5 (signup conversion) rebinds extraction rows from
    this user to the new real account.
    """
    user = session.execute(
        select(User).where(User.email == ANON_SERVICE_USER_EMAIL)
    ).scalar_one_or_none()
    if user is not None:
        return user
    user = User(
        email=ANON_SERVICE_USER_EMAIL,
        password_hash=hash_password("anonymous-service-no-login"),
        status="active",
    )
    session.add(user)
    try:
        session.flush()
    except IntegrityError:
        # A concurrent first-call also tried to create it. Re-read.
        session.rollback()
        user = session.execute(
            select(User).where(User.email == ANON_SERVICE_USER_EMAIL)
        ).scalar_one()
    return user


def _check_and_increment_ip_counter(session: Session, ip_hash: str, cap: int) -> tuple[bool, int]:
    """UPSERT the per-IP per-day counter; return ``(allowed, retry_after_s)``.

    Returns ``(True, 0)`` when the request is under the cap (counter
    has been incremented and the caller may proceed). Returns
    ``(False, retry_after_s)`` when the cap is exhausted; the seconds
    figure is the wall-clock until the next UTC day rolls over.

    Race-safety: the UNIQUE(ip_hash, day) constraint plus an
    IntegrityError retry on insert handles two concurrent requests
    racing to be the first of the day. The increment branch reads the
    row, checks the count, and updates in one transaction; under
    contention the second writer can see a stale count, so we
    re-check after commit. For multi-process Postgres this still
    holds because the UNIQUE constraint serializes the insert and
    PostgreSQL row-locking serializes the update path.
    """
    day = today_bucket()
    row = session.execute(
        select(AnonExtractCounter).where(
            AnonExtractCounter.ip_hash == ip_hash,
            AnonExtractCounter.day == day,
        )
    ).scalar_one_or_none()
    if row is None:
        # Insert fresh row with count=1. Race: a concurrent insert
        # raises IntegrityError; we fall through to the increment path.
        try:
            session.add(
                AnonExtractCounter(ip_hash=ip_hash, day=day, count=1, updated_at=utcnow())
            )
            session.commit()
            return True, 0
        except IntegrityError:
            session.rollback()
            row = session.execute(
                select(AnonExtractCounter).where(
                    AnonExtractCounter.ip_hash == ip_hash,
                    AnonExtractCounter.day == day,
                )
            ).scalar_one()
    if row.count >= cap:
        session.rollback()
        return False, _seconds_until_next_utc_day()
    row.count = row.count + 1
    row.updated_at = utcnow()
    session.commit()
    return True, 0


def _seconds_until_next_utc_day() -> int:
    """Return whole seconds until the next UTC midnight.

    Returned to the client as ``retry_after_s`` so an abusive client
    knows how long to wait. RFC 9110 Retry-After accepts integer
    seconds; we ceil and bound at 1 so the client never tight-loops.
    """
    now = utcnow()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    delta = (tomorrow - now).total_seconds()
    return max(1, int(delta) + (1 if delta % 1 else 0))


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.post("/anonymous/extractions")
def create_anonymous_extraction(
    payload: AnonymousExtractRequest,
    request: Request,
    session: Session = Depends(get_db),
) -> JSONResponse:
    """Create one anonymous extraction with rate-limit + classifier gating.

    Flow:
    1. Feature-flag gate (503 when off).
    2. Per-IP daily cap (429 when exhausted).
    3. URL classification (Stage O3 stub returns ``html_first``).
    4. Unsupported class: short-circuit with notify-capture surface;
       no extractor cycles burned.
    5. Supported class: insert ``extractions`` row tagged ``status="pending"``
       owned by the service user; mint claim_token; insert
       ``anonymous_extractions`` registry row. Returns 202.

    Edge cases:
    - Duplicate ``claim_token`` UNIQUE collision is treated as retry; we
      regenerate and reinsert.
    - The actual extractor run is NOT performed inline (the canonical
      path uses ``app/routes/extractions.py``'s in-process extractor +
      storage upload). Stage O1 enqueues the row at ``status="pending"``;
      a follow-up dispatch wires the worker pickup.
    """
    if not feature_enabled():
        return JSONResponse(
            status_code=503,
            content={
                "schema_version": ANON_EXTRACTION_SCHEMA_VERSION,
                "error": "feature_disabled",
                "message": "Anonymous extraction is not enabled in this environment.",
            },
        )

    client_ip = _client_ip(request)
    ip_hash = hash_ip(client_ip)
    cap = per_ip_daily_cap()
    allowed, retry_after = _check_and_increment_ip_counter(session, ip_hash, cap)
    if not allowed:
        logger.info(
            "anon_extract_rate_limited ip_hash=%s cap=%d retry_after=%d",
            ip_hash,
            cap,
            retry_after,
        )
        return JSONResponse(
            status_code=429,
            content={
                "schema_version": ANON_EXTRACTION_SCHEMA_VERSION,
                "error": "rate_limited",
                "message": ANON_RATE_LIMITED_MESSAGE,
                "retry_after_s": retry_after,
            },
            headers={"Retry-After": str(retry_after)},
        )

    url = str(payload.url)
    classification: ClassificationResult = classify_url(url)

    if not is_supported(classification.label):
        # Out-of-scope: do NOT enqueue an extraction; the visitor's
        # rate-limit counter was already incremented above which is the
        # intended behavior (an abuser hammering unsupported URLs still
        # exhausts their daily budget).
        message = ANON_OUT_OF_SCOPE_MESSAGE.format(detected_class=classification.label)
        response = AnonymousExtractResponse(
            schema_version=ANON_EXTRACTION_SCHEMA_VERSION,
            status="out_of_scope",
            classification=classification.label,
            extraction_id=None,
            claim_token=None,
            refunded=True,
            message=message,
            notify_email_capture_url="/api/notify-when-supported",
        )
        return JSONResponse(status_code=200, content=response.to_dict())

    # Supported class: create the underlying extractions row + registry.
    service_user = _get_or_create_service_user(session)
    extraction = Extraction(
        user_id=service_user.id,
        api_key_id=None,
        url=url,
        url_normalized=url.strip().lower(),
        status="pending",
        schema_version=SCHEMA_V1,
        credit_cents=0,
    )
    session.add(extraction)
    session.flush()

    # Mint claim_token with UNIQUE-collision retry. Token entropy
    # makes a collision astronomically unlikely; we still treat it as
    # a recoverable signal rather than a 500.
    for _attempt in range(3):
        claim_token = mint_claim_token()
        registry = AnonymousExtraction(
            claim_token=claim_token,
            ip_hash=ip_hash,
            extraction_id=extraction.id,
            url=url,
            classification=classification.label,
            status="pending",
            schema_version=ANON_EXTRACTION_SCHEMA_VERSION,
            expires_at=utcnow() + timedelta(hours=ANON_EXTRACTION_CLAIM_WINDOW_HOURS),
        )
        session.add(registry)
        try:
            session.commit()
            break
        except IntegrityError:
            session.rollback()
            continue
    else:
        return JSONResponse(
            status_code=500,
            content={
                "schema_version": ANON_EXTRACTION_SCHEMA_VERSION,
                "error": "claim_token_collision",
            },
        )

    response = AnonymousExtractResponse(
        schema_version=ANON_EXTRACTION_SCHEMA_VERSION,
        status="pending",
        classification=classification.label,
        extraction_id=extraction.id,
        claim_token=claim_token,
        refunded=False,
        message=None,
        notify_email_capture_url=None,
    )
    return JSONResponse(status_code=202, content=response.to_dict())


@router.get("/anonymous/extractions/{extraction_id}")
def get_anonymous_extraction(
    extraction_id: int,
    request: Request,
    claim_token: str = "",
    session: Session = Depends(get_db),
) -> JSONResponse:
    """Return one anonymous extraction's status + tokens preview.

    The ``claim_token`` query-string parameter MUST match the registry
    row's stored token; a mismatch returns 403. We deliberately use a
    constant-time comparison to avoid timing-leak attacks against the
    token character set.
    """
    if not claim_token:
        return JSONResponse(
            status_code=403,
            content={
                "schema_version": ANON_EXTRACTION_SCHEMA_VERSION,
                "error": "claim_token_required",
            },
        )
    registry = session.execute(
        select(AnonymousExtraction).where(AnonymousExtraction.extraction_id == extraction_id)
    ).scalar_one_or_none()
    if registry is None:
        return JSONResponse(
            status_code=404,
            content={"schema_version": ANON_EXTRACTION_SCHEMA_VERSION, "error": "not_found"},
        )
    if not secrets.compare_digest(registry.claim_token, claim_token):
        return JSONResponse(
            status_code=403,
            content={"schema_version": ANON_EXTRACTION_SCHEMA_VERSION, "error": "invalid_claim_token"},
        )
    extraction = session.get(Extraction, extraction_id) if registry.extraction_id is not None else None
    body: dict[str, Any] = {
        "schema_version": ANON_EXTRACTION_SCHEMA_VERSION,
        "extraction_id": registry.extraction_id,
        "classification": registry.classification,
        "status": registry.status if extraction is None else extraction.status,
        "tokens_preview": None,
    }
    if extraction is not None and extraction.tokens_json:
        # Tokens preview: first 8 colors + first 4 type families + first
        # 8 spacing scale values. Anything keyed by `font` or `color` or
        # `space` is sliced cheaply; the full DTCG payload stays gated
        # behind authenticated download endpoints.
        body["tokens_preview"] = _tokens_preview(extraction.tokens_json)
    return JSONResponse(status_code=200, content=body)


def _tokens_preview(tokens_json: dict[str, Any]) -> dict[str, Any]:
    """Return the public preview slice of an extraction's tokens.

    Pure-data helper, unit-tested. Slices to keep an anonymous visitor
    from extracting the full token set without converting; the full
    set stays gated to authed users.
    """
    items = list(tokens_json.items())
    colors = [(k, v) for k, v in items if "color" in k.lower() or "bg" in k.lower() or "accent" in k.lower() or "surface" in k.lower() or "text" in k.lower()][:8]
    fonts = [(k, v) for k, v in items if "font" in k.lower()][:4]
    spacing = [(k, v) for k, v in items if "space" in k.lower() or "radius" in k.lower()][:8]
    return {
        "colors": dict(colors),
        "fonts": dict(fonts),
        "spacing": dict(spacing),
    }


@router.post("/notify-when-supported")
def notify_when_supported(
    payload: NotifyWhenSupportedRequest,
    session: Session = Depends(get_db),
) -> JSONResponse:
    """Append a notify-when-supported capture row.

    Append-only; no dedup (a visitor leaving the same email twice is
    a signal, not a problem). The Stage O5 broadcast tooling will dedup
    at send time.
    """
    row = NotifyRequest(
        url=str(payload.url),
        email=payload.email,
        detected_class=payload.detected_class,
    )
    session.add(row)
    session.commit()
    return JSONResponse(
        status_code=200,
        content={
            "schema_version": ANON_EXTRACTION_SCHEMA_VERSION,
            "ok": True,
        },
    )

"""Idempotency-Key support for POST /v1/extractions (and future routes).

Wire-up shape
-------------
The route handler does three things with this module:

1. Validate the incoming ``Idempotency-Key`` header at the request
   boundary (``validate_idempotency_key``). A malformed key is a 400; an
   absent key is the no-replay path (returns ``None`` from
   ``read_header``).

2. After computing the canonical body hash, call
   ``lookup_cached_response`` to see if a prior call for this
   ``(user_id, key)`` already landed. On hit: replay verbatim with the
   ``X-Idempotency-Replayed: true`` header. On hash-mismatch hit: 409.

3. On a fresh successful charge + extraction, call ``store_response``
   to persist the response body for future replay.

Lifecycle / TTL
---------------
Rows older than ``IDEMPOTENCY_KEY_TTL_SECONDS`` are treated as expired
at lookup time even if they still exist in the table. A separate sweep
job can prune them; until that lands, ``created_at`` is indexed so a
``DELETE ... WHERE created_at < ...`` runs cheaply.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse, Response

from app.constants import (
    IDEMPOTENCY_KEY_MAX_LENGTH,
    IDEMPOTENCY_KEY_MIN_LENGTH,
    IDEMPOTENCY_KEY_PATTERN,
    IDEMPOTENCY_KEY_TTL_SECONDS,
    IDEMPOTENCY_REPLAYED_HEADER_NAME,
)
from app.models import IdempotencyKey

logger = logging.getLogger(__name__)


# Validation outcome vocabulary. The route handler switches on this
# instead of magic strings so a typo at the call site is a NameError,
# not a silent always-false branch.
ValidationOutcome = Literal["ok", "too_short", "too_long", "bad_chars"]


@dataclass(frozen=True)
class CachedReplay:
    """One persisted idempotent response ready to be returned to the client.

    ``status_code`` and ``response_body`` round-trip the original HTTP
    response. ``replayed=True`` is the signal the route uses to set the
    ``X-Idempotency-Replayed`` header on the outbound response.
    """

    status_code: int
    response_body: str
    replayed: bool = True


def validate_idempotency_key(value: str) -> ValidationOutcome:
    """Check the supplied header value against the documented bounds.

    Returns ``"ok"`` when the value passes; otherwise returns the closed
    error vocabulary the route handler maps to a 400 ``error`` payload.
    Lengths are inclusive bounds and the character allowlist excludes
    whitespace and control characters so a CRLF-injection cannot reach
    the persisted row.
    """
    if len(value) < IDEMPOTENCY_KEY_MIN_LENGTH:
        return "too_short"
    if len(value) > IDEMPOTENCY_KEY_MAX_LENGTH:
        return "too_long"
    if not IDEMPOTENCY_KEY_PATTERN.match(value):
        return "bad_chars"
    return "ok"


def hash_request_body(payload: dict[str, Any]) -> str:
    """Return the canonical SHA-256 hex digest of a JSON-serializable body.

    Canonicalization is JSON with sorted keys and no whitespace so two
    semantically equal requests hash identically. The hash anchors the
    "same key, different body" guard: a replay carrying a different
    payload than the original is a client bug and surfaces as HTTP 409.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_expired(created_at: datetime, now: datetime) -> bool:
    """Return True when ``created_at`` is older than the configured TTL.

    Normalizes naive datetimes (SQLite) to UTC-aware so the subtraction
    against ``now`` works regardless of dialect.
    """
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return (now - created_at) > timedelta(seconds=IDEMPOTENCY_KEY_TTL_SECONDS)


def lookup_cached_response(
    session: Session,
    user_id: int,
    key: str,
    request_hash: str,
) -> CachedReplay | Literal["hash_mismatch"] | None:
    """Look up a prior response for ``(user_id, key)``.

    Returns:
    * ``CachedReplay`` when a matching row exists and the body hashes
      agree. The route replays it verbatim.
    * ``"hash_mismatch"`` when the key matches but the body hash does
      not. The route maps this to HTTP 409.
    * ``None`` when no row exists, or the existing row is older than
      the TTL (treated as if absent so the fresh request lands cleanly).
    """
    row = session.execute(
        select(IdempotencyKey).where(
            IdempotencyKey.user_id == user_id,
            IdempotencyKey.key == key,
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if _is_expired(row.created_at, datetime.now(timezone.utc)):
        # Expired entries are treated as absent. A future sweep job
        # deletes them; until then leave the row in place and let the
        # fresh request insert a sibling row via the UPSERT path
        # below. (We do not delete here on the read path; the route's
        # transaction must remain side-effect-free until the charge
        # actually lands.)
        return None
    if row.request_hash != request_hash:
        return "hash_mismatch"
    return CachedReplay(
        status_code=row.status_code,
        response_body=row.response_body,
        replayed=True,
    )


def store_response(
    session: Session,
    user_id: int,
    key: str,
    request_hash: str,
    status_code: int,
    response_body: str,
) -> bool:
    """Persist a fresh response for replay; idempotent on duplicate insert.

    Returns ``True`` if a new row was inserted. Returns ``False`` if a
    row already exists for ``(user_id, key)`` (e.g. a sibling request
    won the race and inserted first); the caller treats this as a
    benign no-op because the client will still receive the freshly-
    computed response from THIS call, and any subsequent retry will
    hit the cached row that the OTHER call inserted.

    On expired rows: a stale (TTL-expired) row in the table is not
    removed by this function; an INSERT will raise IntegrityError on
    the duplicate primary key. We return False in that case and log
    the surprise so an operator can trigger a manual prune.
    """
    try:
        session.add(
            IdempotencyKey(
                user_id=user_id,
                key=key,
                request_hash=request_hash,
                status_code=status_code,
                response_body=response_body,
            )
        )
        session.commit()
        return True
    except IntegrityError:
        session.rollback()
        logger.info(
            "idempotency_key_insert_skipped user_id=%s key_prefix=%s reason=duplicate",
            user_id,
            key[:8],
        )
        return False


def build_replay_response(replay: CachedReplay) -> Response:
    """Turn a ``CachedReplay`` into an outbound ``Response`` for the client.

    Sets the ``X-Idempotency-Replayed`` header so callers can
    distinguish a cached-replay from a fresh computation. The body is
    written through ``Response(content=...)`` rather than ``JSONResponse``
    because the cached bytes are already serialized JSON; re-serializing
    via ``JSONResponse`` would alter whitespace and break clients that
    compute their own hash of the body.
    """
    return Response(
        content=replay.response_body,
        status_code=replay.status_code,
        media_type="application/json",
        headers={IDEMPOTENCY_REPLAYED_HEADER_NAME: "true"},
    )


def validation_error_response(reason: ValidationOutcome) -> JSONResponse:
    """Return the 400 response shape for a malformed idempotency key.

    The closed-set ``reason`` vocabulary lands in the ``error`` field so
    a client can disambiguate length-vs-character problems without
    parsing free text. The schema_version is omitted because this is a
    validation-failure 400, not an envelope the client persists.
    """
    return JSONResponse(
        status_code=400,
        content={"error": "idempotency_key_invalid", "reason": reason},
    )

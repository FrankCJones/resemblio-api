"""API key lifecycle routes.

S3b Wave 3 hardening: every read- and write-endpoint here filters on
``ApiKey.kind == 'user'`` AND ``ApiKey.is_visible_to_user == True``. The BFF
session key (``kind='internal_bff'``) and any future service keys
(``kind='service'``) are NEVER returned by, mutated by, or referenceable from
this surface. This closes the leak where the dashboard's "your API keys"
page would otherwise expose or let the user revoke the BFF key that powers
the dashboard itself.
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app.auth import current_user, utcnow
from app.config import get_settings
from app.constants import (
    API_KEY_KIND_USER,
    DEFAULT_API_SCOPE,
    ROTATION_GRACE_HOURS,
    SCHEMA_V1,
)
from app.crypto import generate_api_key
from app.db import get_db
from app.models import ApiKey, ApiKeyEvent, User
from app.schemas import (
    ApiKeyAuditEvent,
    ApiKeyAuditResponse,
    ApiKeyCreateRequest,
    ApiKeyCreatedResponse,
    ApiKeyListItem,
    ApiKeyListResponse,
    ApiKeyRevokeRequest,
    ApiKeySpendCapRequest,
    ApiKeySpendCapResponse,
    ApiKeyStatusResponse,
)

router = APIRouter()

# Max audit rows returned per /audit call. Audit logs grow monotonically with
# usage; capping the page bounds the response size and keeps the dashboard
# drawer rendering fast. Customers needing the full history can paginate via
# the ``before`` cursor.
AUDIT_DEFAULT_LIMIT = 20
AUDIT_MAX_LIMIT = 100


def _client_ip(request: Request) -> str | None:
    """Return a best-effort client IP for key audit events."""
    return request.client.host if request.client else None


def _owned_user_key(session: Session, user_id: int, key_id: int) -> ApiKey | None:
    """Fetch a USER-kind, user-visible key owned by the current user.

    Returns None for BFF/service keys even when they belong to ``user_id``;
    callers translate the None into 404 so the existence of an internal_bff
    key is not leaked across the dashboard surface.
    """
    return session.execute(
        select(ApiKey).where(
            ApiKey.id == key_id,
            ApiKey.user_id == user_id,
            ApiKey.kind == API_KEY_KIND_USER,
            ApiKey.is_visible_to_user.is_(True),
        )
    ).scalar_one_or_none()


@router.post("/api_keys", response_model=ApiKeyCreatedResponse)
def create_api_key(
    payload: ApiKeyCreateRequest,
    request: Request,
    session: Session = Depends(get_db),
) -> ApiKeyCreatedResponse:
    """Create a new user-visible API key and return plaintext once.

    Always mints with ``kind='user'`` and ``is_visible_to_user=True``. The BFF
    key minted at signup uses a different code path (``internal_auth.py``);
    this surface never produces an internal_bff key.
    """
    user: User = current_user(request)
    plaintext, digest, prefix = generate_api_key(get_settings().default_key_env)  # type: ignore[arg-type]
    api_key = ApiKey(
        user_id=user.id,
        key_hash=digest,
        key_prefix=prefix,
        label=payload.label.strip(),
        scopes=[DEFAULT_API_SCOPE],
        kind=API_KEY_KIND_USER,
        is_visible_to_user=True,
        created_from_ip=_client_ip(request),
    )
    session.add(api_key)
    session.flush()
    session.add(ApiKeyEvent(api_key_id=api_key.id, event_type="created", ip=_client_ip(request), metadata_json={"label": api_key.label}))
    session.commit()
    session.refresh(api_key)
    return ApiKeyCreatedResponse(id=api_key.id, api_key=plaintext, key_prefix=api_key.key_prefix, label=api_key.label, schema_version=SCHEMA_V1)


@router.get("/api_keys", response_model=ApiKeyListResponse)
def list_api_keys(request: Request, session: Session = Depends(get_db)) -> ApiKeyListResponse:
    """List display-safe metadata for the user's USER-kind, visible keys only.

    Excludes ``kind='internal_bff'`` (the dashboard's own session key) and
    ``kind='service'`` rows. The dashboard's "your API keys" surface must
    never expose either category.
    """
    user: User = current_user(request)
    keys = session.execute(
        select(ApiKey).where(
            ApiKey.user_id == user.id,
            ApiKey.kind == API_KEY_KIND_USER,
            ApiKey.is_visible_to_user.is_(True),
        ).order_by(ApiKey.created_at.desc())
    ).scalars().all()
    return ApiKeyListResponse(items=[ApiKeyListItem.model_validate(key) for key in keys], schema_version=SCHEMA_V1)


@router.post("/api_keys/{key_id}/rotate", response_model=ApiKeyCreatedResponse)
def rotate_api_key(key_id: int, request: Request, session: Session = Depends(get_db)) -> ApiKeyCreatedResponse | JSONResponse:
    """Rotate an owned user-visible key. Old key works during 48h grace.

    Refuses to rotate non-user-kind rows; the dashboard never sees them and
    must not be able to address them by id either.
    """
    user: User = current_user(request)
    old_key = _owned_user_key(session, user.id, key_id)
    if old_key is None:
        return JSONResponse(status_code=404, content={"error": "not_found"})
    plaintext, digest, prefix = generate_api_key(get_settings().default_key_env)  # type: ignore[arg-type]
    old_key.status = "rotated_out"
    old_key.grace_expires_at = utcnow() + timedelta(hours=ROTATION_GRACE_HOURS)
    new_key = ApiKey(
        user_id=user.id,
        key_hash=digest,
        key_prefix=prefix,
        label=old_key.label,
        scopes=list(old_key.scopes),
        kind=API_KEY_KIND_USER,
        is_visible_to_user=True,
        created_from_ip=_client_ip(request),
        spend_cap_cents=old_key.spend_cap_cents,
    )
    session.add(new_key)
    session.flush()
    session.add(ApiKeyEvent(api_key_id=old_key.id, event_type="rotated_out", ip=_client_ip(request), metadata_json={"rotated_to": new_key.id}))
    session.add(ApiKeyEvent(api_key_id=new_key.id, event_type="rotated_in", ip=_client_ip(request), metadata_json={"rotated_from": old_key.id}))
    session.commit()
    session.refresh(new_key)
    return ApiKeyCreatedResponse(id=new_key.id, api_key=plaintext, key_prefix=new_key.key_prefix, label=new_key.label, schema_version=SCHEMA_V1)


@router.post("/api_keys/{key_id}/revoke", response_model=ApiKeyStatusResponse)
def revoke_api_key(
    key_id: int,
    payload: ApiKeyRevokeRequest,
    request: Request,
    session: Session = Depends(get_db),
) -> ApiKeyStatusResponse | JSONResponse:
    """Revoke an owned user-visible key. Effective immediately.

    Refuses to revoke non-user-kind rows; the BFF session key cannot be
    revoked through this surface (logout is the only path).
    """
    user: User = current_user(request)
    api_key = _owned_user_key(session, user.id, key_id)
    if api_key is None:
        return JSONResponse(status_code=404, content={"error": "not_found"})
    api_key.status = "revoked"
    api_key.revoked_at = utcnow()
    api_key.revoked_reason = payload.reason
    session.add(ApiKeyEvent(api_key_id=api_key.id, event_type="revoked", ip=_client_ip(request), metadata_json={"reason": payload.reason}))
    session.commit()
    session.refresh(api_key)
    return ApiKeyStatusResponse(id=api_key.id, status=api_key.status, key_prefix=api_key.key_prefix, schema_version=SCHEMA_V1)


@router.patch("/api_keys/{key_id}/spend_cap", response_model=ApiKeySpendCapResponse)
def update_spend_cap(
    key_id: int,
    payload: ApiKeySpendCapRequest,
    request: Request,
    session: Session = Depends(get_db),
) -> ApiKeySpendCapResponse | JSONResponse:
    """Set or clear the per-key trailing 30-day spend cap."""
    user: User = current_user(request)
    api_key = _owned_user_key(session, user.id, key_id)
    if api_key is None:
        return JSONResponse(status_code=404, content={"error": "not_found"})
    old_cap = api_key.spend_cap_cents
    api_key.spend_cap_cents = payload.cap_cents
    session.add(
        ApiKeyEvent(
            api_key_id=api_key.id,
            event_type="spend_cap_changed",
            ip=_client_ip(request),
            metadata_json={"old_cap_cents": old_cap, "new_cap_cents": payload.cap_cents},
        )
    )
    session.commit()
    session.refresh(api_key)
    return ApiKeySpendCapResponse(id=api_key.id, spend_cap_cents=api_key.spend_cap_cents, schema_version=SCHEMA_V1)


@router.get("/api_keys/{key_id}/audit", response_model=ApiKeyAuditResponse)
def get_api_key_audit(
    key_id: int,
    request: Request,
    session: Session = Depends(get_db),
    limit: int = Query(AUDIT_DEFAULT_LIMIT, ge=1, le=AUDIT_MAX_LIMIT),
    before: int | None = Query(default=None, ge=1),
) -> ApiKeyAuditResponse | JSONResponse:
    """Return the audit-event history for a user-visible key.

    Cursor-paginated newest-first by ``ApiKeyEvent.id``. Pass the smallest id
    from the current page as ``before`` to fetch the next page. Refuses to
    return events for non-user-kind rows so a probing client cannot discover
    that an internal_bff key exists.
    """
    user: User = current_user(request)
    api_key = _owned_user_key(session, user.id, key_id)
    if api_key is None:
        return JSONResponse(status_code=404, content={"error": "not_found"})
    stmt = select(ApiKeyEvent).where(ApiKeyEvent.api_key_id == api_key.id)
    if before is not None:
        stmt = stmt.where(ApiKeyEvent.id < before)
    stmt = stmt.order_by(ApiKeyEvent.id.desc()).limit(limit)
    rows = session.execute(stmt).scalars().all()
    items = [
        ApiKeyAuditEvent(
            id=row.id,
            event_type=row.event_type,
            occurred_at=row.occurred_at,
            ip=row.ip,
            metadata=row.metadata_json,
        )
        for row in rows
    ]
    return ApiKeyAuditResponse(items=items, schema_version=SCHEMA_V1)

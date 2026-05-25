"""API key lifecycle routes."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app.auth import current_user, utcnow
from app.config import get_settings
from app.constants import DEFAULT_API_SCOPE, ROTATION_GRACE_HOURS, SCHEMA_V1
from app.crypto import generate_api_key
from app.db import get_db
from app.models import ApiKey, ApiKeyEvent, User
from app.schemas import (
    ApiKeyCreateRequest,
    ApiKeyCreatedResponse,
    ApiKeyListItem,
    ApiKeyListResponse,
    ApiKeyRevokeRequest,
    ApiKeyStatusResponse,
)

router = APIRouter()


def _client_ip(request: Request) -> str | None:
    """Return a best-effort client IP for key audit events."""
    return request.client.host if request.client else None


def _owned_key(session: Session, user_id: int, key_id: int) -> ApiKey | None:
    """Fetch a key owned by the current user."""
    return session.execute(select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user_id)).scalar_one_or_none()


@router.post("/api_keys", response_model=ApiKeyCreatedResponse)
def create_api_key(
    payload: ApiKeyCreateRequest,
    request: Request,
    session: Session = Depends(get_db),
) -> ApiKeyCreatedResponse:
    """Create a new API key and return plaintext once."""
    user: User = current_user(request)
    plaintext, digest, prefix = generate_api_key(get_settings().default_key_env)  # type: ignore[arg-type]
    api_key = ApiKey(
        user_id=user.id,
        key_hash=digest,
        key_prefix=prefix,
        label=payload.label.strip(),
        scopes=[DEFAULT_API_SCOPE],
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
    """List display-safe key metadata for the current user."""
    user: User = current_user(request)
    keys = session.execute(select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc())).scalars().all()
    return ApiKeyListResponse(items=[ApiKeyListItem.model_validate(key) for key in keys], schema_version=SCHEMA_V1)


@router.post("/api_keys/{key_id}/rotate", response_model=ApiKeyCreatedResponse)
def rotate_api_key(key_id: int, request: Request, session: Session = Depends(get_db)) -> ApiKeyCreatedResponse | JSONResponse:
    """Rotate an owned key and keep the old key valid for 48 hours."""
    user: User = current_user(request)
    old_key = _owned_key(session, user.id, key_id)
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
        created_from_ip=_client_ip(request),
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
    """Revoke an owned key and append an audit event."""
    user: User = current_user(request)
    api_key = _owned_key(session, user.id, key_id)
    if api_key is None:
        return JSONResponse(status_code=404, content={"error": "not_found"})
    api_key.status = "revoked"
    api_key.revoked_at = utcnow()
    api_key.revoked_reason = payload.reason
    session.add(ApiKeyEvent(api_key_id=api_key.id, event_type="revoked", ip=_client_ip(request), metadata_json={"reason": payload.reason}))
    session.commit()
    session.refresh(api_key)
    return ApiKeyStatusResponse(id=api_key.id, status=api_key.status, key_prefix=api_key.key_prefix, schema_version=SCHEMA_V1)


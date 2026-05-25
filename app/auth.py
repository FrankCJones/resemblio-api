"""API-key authentication middleware."""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

from app.config import get_settings
from app.crypto import hash_api_key
from app.db import SessionLocal
from app.models import ApiKey, ApiKeyEvent, User
from app.rate_limit import rate_limiter

TOKEN_RE = re.compile(r"^rsmb_(live|test)_[A-Za-z0-9_-]{43}$")
AUTH_FREE_PATHS = frozenset({"/v1/healthz", "/v1/webhooks/stripe", "/docs", "/redoc", "/openapi.json"})


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _client_ip(request: Request) -> str | None:
    """Return the best-effort client IP for audit events."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else None


def _error(status_code: int, code: str, detail: str | None = None) -> JSONResponse:
    """Build a contract-shaped JSON error response."""
    body: dict[str, str] = {"error": code}
    if detail is not None:
        body["detail"] = detail
    return JSONResponse(status_code=status_code, content=body)


def _append_event(api_key: ApiKey, event_type: str, ip: str | None, metadata: dict[str, Any] | None = None) -> ApiKeyEvent:
    """Create an API key audit event without committing."""
    return ApiKeyEvent(api_key_id=api_key.id, event_type=event_type, ip=ip, metadata_json=metadata)


def _lookup_key(token: str, peppers: Iterable[str]) -> ApiKey | None:
    """Find an API key by hashing against active and old peppers."""
    hashes = [hash_api_key(token, pepper) for pepper in peppers if pepper]
    if not hashes:
        return None
    with SessionLocal() as session:
        return session.query(ApiKey).filter(ApiKey.key_hash.in_(hashes)).first()


class AuthMiddleware(BaseHTTPMiddleware):
    """Authenticate all protected routes with bearer API keys."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Validate credentials, attach auth state, and record usage events."""
        if request.url.path in AUTH_FREE_PATHS or request.url.path.startswith("/docs"):
            return await call_next(request)

        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            return _error(401, "missing_credentials")
        token = header.removeprefix("Bearer ").strip()
        if not TOKEN_RE.match(token):
            return _error(401, "invalid_credentials")

        settings = get_settings()
        hashes = [hash_api_key(token, settings.key_pepper)]
        if settings.key_pepper_old:
            hashes.append(hash_api_key(token, settings.key_pepper_old))

        ip = _client_ip(request)
        with SessionLocal() as session:
            api_key = session.query(ApiKey).filter(ApiKey.key_hash.in_(hashes)).first()
            if api_key is None:
                return _error(401, "invalid_credentials")
            user = session.get(User, api_key.user_id)
            if user is None or user.status != "active":
                return _error(401, "account_inactive")

            if api_key.status in {"revoked", "suspended", "expired"}:
                session.add(_append_event(api_key, "attempted_after_revocation", ip, {"ip": ip}))
                session.commit()
                return _error(401, f"key_{api_key.status}")

            rotation_warning: str | None = None
            if api_key.status == "rotated_out":
                grace = api_key.grace_expires_at
                if grace is None or _as_aware(grace) < utcnow():
                    api_key.status = "expired"
                    session.add(_append_event(api_key, "expired", ip, {"route": request.url.path}))
                    session.commit()
                    return _error(401, "key_expired", "This key was rotated; see dashboard")
                rotation_warning = f"This key was rotated. Replace with new key by {grace.isoformat()}"

            limit = rate_limiter.check(api_key.key_hash, user.id)
            if not limit.allowed:
                return _error(429, limit.error or "rate_limited")

            request.state.current_user = user
            request.state.current_api_key = api_key
            response = await call_next(request)

            api_key.last_used_at = utcnow()
            session.add(_append_event(api_key, "used", ip, {"route": request.url.path, "status_code": response.status_code}))
            session.commit()
            if rotation_warning:
                response.headers["X-API-Key-Rotation-Warning"] = rotation_warning
            return response


def _as_aware(value: datetime) -> datetime:
    """Treat SQLite naive timestamps as UTC for lifecycle comparisons."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def current_user(request: Request) -> User:
    """Return the authenticated user attached by middleware."""
    return request.state.current_user


def current_api_key(request: Request) -> ApiKey:
    """Return the authenticated API key attached by middleware."""
    return request.state.current_api_key


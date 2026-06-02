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
AUTH_FREE_PATHS = frozenset({
    "/healthz",
    "/v1/healthz",
    "/readyz",
    "/v1/readyz",
    "/v1/webhooks/stripe",
    "/docs",
    "/redoc",
    "/openapi.json",
    # Internal BFF auth surface. These routes are gated by a separate shared-
    # secret middleware (see ``app/routes/internal_auth.py``) rather than the
    # Bearer-token AuthMiddleware, because the requesting actor is the Next.js
    # web process not an end-user holding an API key.
    "/v1/internal/auth/request_magic_link",
    "/v1/internal/auth/redeem_magic_link",
    "/v1/internal/auth/whoami",
    "/v1/internal/auth/logout",
    # S3b Wave 2c internal billing surface. Same posture as the auth surface
    # above: gated by the X-Internal-Auth shared secret on the route handler,
    # not by Bearer-token AuthMiddleware. The BFF holds the user's identity
    # via cookie + session-store, not via a billing-scoped API key.
    "/v1/internal/billing/create_checkout_session",
})

# Constant-shape pepper used when the operator has not configured an old pepper
# (i.e. no rotation is in flight). The lookup still hashes the presented token
# twice and runs an IN-list of two values against the index, so the query shape
# is identical regardless of whether pepper rotation is active. Without this,
# the query degenerates to a one-element IN-list during steady state and a
# two-element IN-list during the 48-hour rotation grace window; the timing
# difference is a (small) side-channel that leaks "rotation in progress."
# Closes audit finding M-API-2 (security-audits/2026-05-26-initial.md).
# The string is constant, well outside the token-hash output space, and is not
# secret; its only job is to occupy the second slot in the IN-list.
_DUMMY_PEPPER = "RESEMBLIO_DUMMY_PEPPER_NEVER_MATCHES_REAL_KEY_HASHES"

# Trusted reverse proxies. The API service sits behind Caddy on localhost; only
# requests whose immediate peer is in this allowlist are permitted to declare a
# different client IP via X-Forwarded-For. Spoofed forwarded headers from any
# other source are ignored so audit events in api_key_events cannot be poisoned
# with attacker-chosen IPs. Keep this list narrow; widen only when an additional
# proxy is added in front of the service and documented in Resemblio_INFRA.md.
TRUSTED_PROXY_IPS = frozenset({"127.0.0.1", "::1"})


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _client_ip(request: Request) -> str | None:
    """Return the best-effort client IP for audit events.

    Honors ``X-Forwarded-For`` only when the immediate peer is a trusted proxy
    (see ``TRUSTED_PROXY_IPS``). Untrusted peers cannot forge the recorded IP.
    When trusted, the right-most untrusted entry of the forwarded chain is
    chosen: the chain is ``client, proxy1, proxy2, ...`` so we walk from the
    left and skip any leading entries that are themselves trusted-proxy IPs,
    returning the first non-trusted hop. Falls back to the direct peer when no
    forwarded header is present or the peer is untrusted.
    """
    peer_host = request.client.host if request.client else None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and peer_host in TRUSTED_PROXY_IPS:
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        for hop in hops:
            if hop not in TRUSTED_PROXY_IPS:
                return hop
        # Entire chain was trusted proxies; fall through to peer host.
    return peer_host


def _error(
    status_code: int,
    code: str,
    detail: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build a contract-shaped JSON error response.

    ``headers`` is optional and used for protocol-level hints such as
    ``Retry-After`` on 429 responses (see RFC 9110 § 15.5.7). The body shape
    is unchanged for backward-compat; headers are additive.
    """
    body: dict[str, str] = {"error": code}
    if detail is not None:
        body["detail"] = detail
    return JSONResponse(status_code=status_code, content=body, headers=headers or None)


def _append_event(api_key: ApiKey, event_type: str, ip: str | None, metadata: dict[str, Any] | None = None) -> ApiKeyEvent:
    """Create an API key audit event without committing."""
    return ApiKeyEvent(api_key_id=api_key.id, event_type=event_type, ip=ip, metadata_json=metadata)


def _candidate_hashes(token: str, active_pepper: str, old_pepper: str | None) -> list[str]:
    """Return exactly two key-hash candidates regardless of rotation state.

    Always emits two hashes: the active-pepper hash plus either the old-pepper
    hash (when rotation is in flight) or a dummy-pepper hash (steady state).
    The dummy hash never matches a real stored key. Keeping the candidate count
    constant makes the downstream SQL ``IN (?, ?)`` lookup constant-shape, which
    closes the timing side-channel called out in audit finding M-API-2.
    """
    second_pepper = old_pepper if old_pepper else _DUMMY_PEPPER
    return [hash_api_key(token, active_pepper), hash_api_key(token, second_pepper)]


def _lookup_key(token: str, peppers: Iterable[str]) -> ApiKey | None:
    """Find an API key by hashing against active and old peppers.

    Retained for callers that pass an explicit iterable of peppers. Production
    auth uses ``_candidate_hashes`` via ``AuthMiddleware.dispatch`` to keep the
    SQL query shape constant; this helper preserves the older calling pattern
    for direct script use and tests.
    """
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
        hashes = _candidate_hashes(token, settings.key_pepper, settings.key_pepper_old)

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
                rate_headers: dict[str, str] | None = None
                if limit.retry_after_seconds is not None:
                    # RFC 9110 § 10.2.3: Retry-After accepts a delta-seconds
                    # integer. Clients (curl, requests, browsers) honor this
                    # automatically; without it they have to guess a backoff.
                    rate_headers = {"Retry-After": str(limit.retry_after_seconds)}
                return _error(429, limit.error or "rate_limited", headers=rate_headers)

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


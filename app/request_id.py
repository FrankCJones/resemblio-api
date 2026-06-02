"""Request-ID correlation middleware.

Generates (or honors an incoming) ``X-Request-Id`` header for every HTTP
request, attaches it to ``request.state.request_id`` so downstream handlers
can include it in log lines, and echoes it on the response. The same value
is surfaced on JSON error bodies via ``error_response_with_request_id`` so a
customer support ticket can be grep'd against ``journalctl`` output.

The audit chain that surfaced this need: the 2026-06-02 audit-IP 500 took a
``journalctl -u resemblio-api`` dig to root-cause. Per-request correlation
shortens that loop because the response a customer sends with their bug
report carries the request_id; the operator greps the unit log once.

Incoming ``X-Request-Id`` values are accepted only if they look like a
sensible identifier (printable ASCII, 8-128 chars). Anything else is
replaced with a server-minted uuid4 to keep log-injection vectors closed.
"""
from __future__ import annotations

import re
import uuid

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

#: Header name used on both ingress and egress.
REQUEST_ID_HEADER = "X-Request-Id"

#: Bounds on accepted incoming request-id values. Narrow on purpose:
#: production log lines must remain greppable and length-bounded so a
#: hostile caller cannot inflate log size by submitting megabytes in the
#: header. uuid4 hex (32) + dashes (4) = 36, so 128 leaves headroom for
#: client-side prefixed schemes (``svc-<uuid>``).
_MIN_LEN = 8
_MAX_LEN = 128
_ALLOWED = re.compile(r"^[A-Za-z0-9._\-]+$")


def _accept_incoming(value: str | None) -> str | None:
    """Return ``value`` if it matches the accepted shape, else None.

    Refuses empty strings, anything outside ``[A-Za-z0-9._-]``, and anything
    outside the length bounds. Returning None signals "mint a fresh id".
    """
    if value is None:
        return None
    value = value.strip()
    if not (_MIN_LEN <= len(value) <= _MAX_LEN):
        return None
    if not _ALLOWED.match(value):
        return None
    return value


def new_request_id() -> str:
    """Return a fresh server-minted request id (uuid4 hex, no dashes).

    Hex form keeps log lines compact and avoids whitespace quoting concerns
    when the id appears unquoted in journalctl output.
    """
    return uuid.uuid4().hex


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Attach a correlation id to every request and echo it on the response.

    Honors a well-formed incoming ``X-Request-Id`` header so an upstream
    proxy (Caddy, a load balancer, an API gateway) can stitch its own
    correlation id through. Falls back to a server-minted uuid4 hex.
    """

    async def dispatch(  # type: ignore[override]
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Resolve the request id, expose it, and stamp the response."""
        incoming = _accept_incoming(request.headers.get(REQUEST_ID_HEADER))
        request_id = incoming or new_request_id()
        request.state.request_id = request_id
        response = await call_next(request)
        # Always echo, even if the route's handler also wrote the header
        # (last-write-wins keeps the response source of truth at this layer).
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

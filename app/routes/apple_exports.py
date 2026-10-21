"""Authenticated, zero-credit delivery for the canonical Apple DTCG document."""
from __future__ import annotations

import hashlib

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, Response

from app.apple_export_product import TOKEN_MEDIA_TYPE, canonical_complete_token_bytes
from app.auth import current_user
from app.constants import LIBRARY_EXPORT_ENTITLED_TIERS
from app.models import User

router = APIRouter()
_CACHE_CONTROL = "private, max-age=0, must-revalidate"
_CONTENT_DISPOSITION = 'attachment; filename="design.tokens.json"'


def _etag_matches(if_none_match: str | None, current_etag: str) -> bool:
    """Apply weak comparison for a GET request's If-None-Match validators."""
    if if_none_match is None:
        return False
    for raw_candidate in if_none_match.split(","):
        candidate = raw_candidate.strip()
        if candidate == "*" or candidate == current_etag:
            return True
        if candidate.startswith("W/") and candidate[2:] == current_etag:
            return True
    return False


@router.get("/apple/exports/dtcg")
def get_apple_dtcg_export(request: Request) -> Response:
    """Return the paid-tier complete DTCG document without charging credits.

    Bearer authentication is enforced by ``AuthMiddleware`` before this
    handler runs. Entitlement is checked before artifact construction or
    conditional delivery. Legacy query fields are ignored because the public
    contract has no selectors. Projection integrity and public scrub failures
    return one opaque 503 response so invalid material cannot cross the route
    boundary.
    """
    user: User = current_user(request)
    if user.subscription_tier not in LIBRARY_EXPORT_ENTITLED_TIERS:
        return JSONResponse(status_code=403, content={"error": "library_export_not_entitled"})
    try:
        token_bytes = canonical_complete_token_bytes()
    except ValueError:
        return JSONResponse(status_code=503, content={"error": "library_export_unavailable"})
    etag = f'"{hashlib.sha256(token_bytes).hexdigest()}"'
    headers = {
        "Cache-Control": _CACHE_CONTROL,
        "Content-Disposition": _CONTENT_DISPOSITION,
        "Content-Type": TOKEN_MEDIA_TYPE,
        "ETag": etag,
    }
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(content=token_bytes, status_code=200, headers=headers)

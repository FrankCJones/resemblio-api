"""Authenticated, zero-credit delivery for public Apple DTCG artifacts."""
from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from app.apple_export_product import AppleSelectorParseError, build_export, parse_selection
from app.auth import current_user
from app.constants import LIBRARY_EXPORT_ENTITLED_TIERS, SCHEMA_V1_1
from app.models import User

router = APIRouter()


@router.get("/apple/exports/dtcg")
def get_apple_dtcg_export(request: Request) -> JSONResponse:
    """Return a paid-tier DTCG artifact without charging credits.

    Bearer authentication is enforced by ``AuthMiddleware`` before this
    handler runs. Entitlement is checked before any selector is interpreted,
    so free users receive the same stable denial for malformed query strings.
    Projection integrity and public scrub failures return one opaque 503
    response so invalid export material cannot cross the route boundary.
    """
    user: User = current_user(request)
    if user.subscription_tier not in LIBRARY_EXPORT_ENTITLED_TIERS:
        return JSONResponse(status_code=403, content={"error": "library_export_not_entitled"})
    values: defaultdict[str, list[str]] = defaultdict(list)
    for key, value in request.query_params.multi_items():
        values[key].append(value)
    try:
        selection = parse_selection(values)
    except AppleSelectorParseError as exc:
        return JSONResponse(status_code=400, content={"error": exc.code, "message": exc.message})
    try:
        artifact = build_export(selection)
    except ValueError:
        return JSONResponse(status_code=503, content={"error": "library_export_unavailable"})
    return JSONResponse(content={"schema_version": SCHEMA_V1_1, "data": artifact})

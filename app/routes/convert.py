"""Conversion routes: DTCG manifest -> shadcn theme / Figma Variables payload.

Endpoints:
    POST /v1/convert/shadcn/{extraction_id}
    POST /v1/convert/figma/{extraction_id}

Both endpoints load the persisted DTCG manifest off the extraction row, run
the pure-data converter (no network, no I/O), and return the converted
payload plus any render artifacts (shadcn emits a ``globals.css`` block and a
``tailwind.config.js`` excerpt; figma emits no render artifacts).

Auth: the existing Bearer-token AuthMiddleware gates every route under
``/v1/*`` (see ``app/main.py``). A missing or invalid token short-circuits
with 401 before this module runs; ownership is verified in-route by scoping
the SELECT to ``user_id == current_user(request).id`` and returning 404 when
the row is missing OR belongs to another user (no enumeration leak).

Pricing: conversion is FREE in v1. The extraction itself was already charged
at creation time; conversion is value-add on top, per the pricing ladder in
``projects/Resemblio/CLAUDE.md``. No ledger debit is appended.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app.auth import current_user
from app.constants import (
    CONVERT_RESPONSE_SCHEMA_VERSION,
    CONVERT_TARGET_FIGMA,
    CONVERT_TARGET_SHADCN,
)
from app.converter_bridge import (
    dtcg_to_figma_variables,
    dtcg_to_shadcn,
    render_globals_css,
    render_tailwind_config,
)
from app.db import get_db
from app.models import Extraction, User
from app.schemas import ConvertRenderedArtifacts, ConvertResponse

logger = logging.getLogger(__name__)

router = APIRouter()


def _load_owned_extraction(
    session: Session, user_id: int, extraction_id: int
) -> Extraction | None:
    """Return the extraction row iff it exists AND belongs to ``user_id``.

    Returns ``None`` when the row is missing OR owned by a different user;
    callers must surface a single 404 (``not_found``) for both branches to
    avoid leaking the existence of an extraction id across user boundaries.
    """
    return session.execute(
        select(Extraction).where(
            Extraction.id == extraction_id,
            Extraction.user_id == user_id,
        )
    ).scalar_one_or_none()


def _not_found_response() -> JSONResponse:
    """Standard 404 body for the convert endpoints.

    Matches the shape used by ``GET /v1/extractions/{id}`` so a client
    polling for status against either surface sees identical 404 bodies.
    """
    return JSONResponse(status_code=404, content={"error": "not_found"})


def _missing_dtcg_response(extraction_id: int) -> JSONResponse:
    """409 returned when the row exists but has no persisted DTCG manifest.

    Failed or pending extractions have a null ``dtcg_json`` column. Returning
    a 409 (not 404) signals to the client that the resource exists but is
    not yet in a convertible state, which is actionable: the right retry is
    "wait for status=ok", not "stop polling".
    """
    return JSONResponse(
        status_code=409,
        content={
            "error": "extraction_not_ready",
            "extraction_id": extraction_id,
        },
    )


@router.post("/convert/shadcn/{extraction_id}", response_model=ConvertResponse)
def convert_shadcn(
    extraction_id: int,
    request: Request,
    session: Session = Depends(get_db),
) -> ConvertResponse | JSONResponse:
    """Convert one extraction's DTCG manifest into a shadcn/ui theme.

    Args:
        extraction_id: The extraction row to convert. Must belong to the
            authenticated user; otherwise 404.
        request: The FastAPI request (used to resolve the current user via
            the AuthMiddleware-populated ``request.state``).
        session: Database session injected by FastAPI.

    Returns:
        ``ConvertResponse`` carrying the shadcn theme as ``payload`` plus
        rendered ``globals_css`` and ``tailwind_config_excerpt`` strings in
        the ``rendered`` block. ``schema_version=2``.

    Pricing:
        FREE in v1. The extraction was already charged at creation time;
        conversion is value-add on top per the pricing ladder in
        ``projects/Resemblio/CLAUDE.md``. No ledger debit.
    """
    user: User = current_user(request)
    extraction = _load_owned_extraction(session, user.id, extraction_id)
    if extraction is None:
        return _not_found_response()
    if extraction.dtcg_json is None:
        return _missing_dtcg_response(extraction_id)

    theme = dtcg_to_shadcn(extraction.dtcg_json, source_url=extraction.url)
    payload: dict[str, Any] = theme.model_dump(by_alias=True)
    rendered = ConvertRenderedArtifacts(
        globals_css=render_globals_css(theme),
        tailwind_config_excerpt=render_tailwind_config(theme),
    )
    return ConvertResponse(
        schema_version=CONVERT_RESPONSE_SCHEMA_VERSION,
        extraction_id=extraction.id,
        target=CONVERT_TARGET_SHADCN,
        payload=payload,
        rendered=rendered,
    )


@router.post("/convert/figma/{extraction_id}", response_model=ConvertResponse)
def convert_figma(
    extraction_id: int,
    request: Request,
    session: Session = Depends(get_db),
) -> ConvertResponse | JSONResponse:
    """Convert one extraction's DTCG manifest into a Figma Variables payload.

    Args:
        extraction_id: The extraction row to convert. Must belong to the
            authenticated user; otherwise 404.
        request: The FastAPI request (used to resolve the current user via
            the AuthMiddleware-populated ``request.state``).
        session: Database session injected by FastAPI.

    Returns:
        ``ConvertResponse`` carrying the Figma Variables payload (collections
        + variables, shaped to match Figma's REST import format) as
        ``payload``. No ``rendered`` block: the payload IS the importable
        artifact. ``schema_version=2``.

    Pricing:
        FREE in v1. Same rationale as ``convert_shadcn``.
    """
    user: User = current_user(request)
    extraction = _load_owned_extraction(session, user.id, extraction_id)
    if extraction is None:
        return _not_found_response()
    if extraction.dtcg_json is None:
        return _missing_dtcg_response(extraction_id)

    figma_payload = dtcg_to_figma_variables(
        extraction.dtcg_json, source_url=extraction.url
    )
    payload: dict[str, Any] = figma_payload.model_dump(by_alias=True)
    return ConvertResponse(
        schema_version=CONVERT_RESPONSE_SCHEMA_VERSION,
        extraction_id=extraction.id,
        target=CONVERT_TARGET_FIGMA,
        payload=payload,
        rendered=None,
    )

"""Stage O7 export-format download routes.

Two parallel endpoints:

* ``GET /v1/extractions/{extraction_id}/export/{format}`` - authed; the
  extraction must belong to ``current_user``.
* ``GET /v1/anonymous/extractions/{extraction_id}/export/{format}`` -
  unauthenticated (path bypass-listed in ``app/auth.py``); a
  ``claim_token`` query-string param must match the registry row.

Both endpoints resolve the DTCG payload from ``asset_versions`` via
``dtcg_for_extraction`` and route through the ``app/exporters/``
subsystem. Conversion is FREE in v1 (the extraction was already
charged at creation time); no ledger debit.

``format`` is one of ``dtcg``, ``css``, ``tailwind``, ``zip``.
Anything else returns 400 with a list of supported formats so a client
can self-correct.
"""
from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse, Response

from app.asset_versions import dtcg_for_extraction
from app.auth import current_user
from app.db import get_db
from app.exporters import EXPORTER_SCHEMA_VERSION
from app.exporters.artifact import (
    FORMAT_CSS,
    FORMAT_DTCG,
    FORMAT_TAILWIND,
    FORMAT_ZIP,
    SUPPORTED_FORMATS,
    ExporterArtifact,
)
from app.exporters.css import css_artifact
from app.exporters.dtcg import dtcg_artifact
from app.exporters.tailwind import tailwind_artifact
from app.exporters.zip_bundle import ZipBundleInputs, zip_artifact
from app.models import AnonymousExtraction, Extraction, User

logger = logging.getLogger(__name__)

router = APIRouter()


def _build_artifact(
    fmt: str, extraction: Extraction, dtcg: dict[str, Any]
) -> ExporterArtifact:
    """Dispatch to the right exporter based on the format slug.

    ``fmt`` is pre-validated by the caller; if a future format is added
    without updating this dispatch, we raise rather than silently
    serving an empty body. The KeyError surfaces fast in tests.
    """
    if fmt == FORMAT_DTCG:
        return dtcg_artifact(extraction.id, dtcg)
    if fmt == FORMAT_CSS:
        return css_artifact(extraction.id, dtcg)
    if fmt == FORMAT_TAILWIND:
        return tailwind_artifact(extraction.id, dtcg)
    if fmt == FORMAT_ZIP:
        # The screenshot bytes pipeline is not yet wired into the
        # extractor bundle (Phase 4 wiring lives on a separate dispatch).
        # Until then the ZIP ships without a screenshot; the README in the
        # bundle reflects that branch.
        inputs = ZipBundleInputs(
            extraction_id=extraction.id,
            source_url=extraction.url,
            screenshot_bytes=None,
        )
        return zip_artifact(dtcg, inputs)
    raise KeyError(f"unhandled export format slug: {fmt!r}")


def _artifact_response(artifact: ExporterArtifact) -> Response:
    """Wrap an ExporterArtifact in a Starlette Response with download headers."""
    return Response(
        content=artifact.bytes,
        media_type=artifact.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{artifact.filename}"',
            "X-Exporter-Schema-Version": str(artifact.schema_version),
        },
    )


def _unsupported_format_response(fmt: str) -> JSONResponse:
    """Return 400 listing the formats the endpoint accepts.

    Self-correcting: a client that hits the endpoint with a typo or a
    legacy format slug gets a usable list back rather than a generic
    404 from FastAPI's path-param mismatch handling.
    """
    return JSONResponse(
        status_code=400,
        content={
            "error": "unsupported_format",
            "requested_format": fmt,
            "supported_formats": sorted(SUPPORTED_FORMATS),
            "schema_version": EXPORTER_SCHEMA_VERSION,
        },
    )


def _not_ready_response(extraction_id: int) -> JSONResponse:
    """Return 409 when the row exists but has no DTCG payload yet."""
    return JSONResponse(
        status_code=409,
        content={
            "error": "extraction_not_ready",
            "extraction_id": extraction_id,
            "schema_version": EXPORTER_SCHEMA_VERSION,
        },
    )


@router.get("/extractions/{extraction_id}/export/{fmt}", response_model=None)
def export_extraction(
    extraction_id: int,
    fmt: str,
    request: Request,
    session: Session = Depends(get_db),
) -> Response | JSONResponse:
    """Download one extraction's tokens in the requested format.

    Args:
        extraction_id: The extraction primary key. Must belong to the
            authenticated user; otherwise 404 (no enumeration leak).
        fmt: One of ``dtcg``, ``css``, ``tailwind``, ``zip``. Anything
            else returns 400.

    Pricing: FREE in v1. The extraction was already charged at creation
    time; format conversion is value-add on top per the pricing ladder.
    """
    if fmt not in SUPPORTED_FORMATS:
        return _unsupported_format_response(fmt)
    user: User = current_user(request)
    extraction = session.execute(
        select(Extraction).where(
            Extraction.id == extraction_id,
            Extraction.user_id == user.id,
        )
    ).scalar_one_or_none()
    if extraction is None:
        return JSONResponse(status_code=404, content={"error": "not_found"})
    dtcg = dtcg_for_extraction(extraction)
    if dtcg is None:
        return _not_ready_response(extraction_id)
    artifact = _build_artifact(fmt, extraction, dtcg)
    return _artifact_response(artifact)


@router.get(
    "/anonymous/extractions/{extraction_id}/export/{fmt}", response_model=None
)
def export_anonymous_extraction(
    extraction_id: int,
    fmt: str,
    claim_token: str = "",
    session: Session = Depends(get_db),
) -> Response | JSONResponse:
    """Download an anonymous extraction's tokens by claim_token.

    Lets a stranger preview the full export bundle before signing up
    (per the URL-first flow). The ``claim_token`` query-string param
    must match the registry row; comparison is constant-time to avoid
    leaking the token character set via timing.
    """
    if fmt not in SUPPORTED_FORMATS:
        return _unsupported_format_response(fmt)
    if not claim_token:
        return JSONResponse(
            status_code=403,
            content={
                "error": "claim_token_required",
                "schema_version": EXPORTER_SCHEMA_VERSION,
            },
        )
    registry = session.execute(
        select(AnonymousExtraction).where(
            AnonymousExtraction.extraction_id == extraction_id
        )
    ).scalar_one_or_none()
    if registry is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "not_found",
                "schema_version": EXPORTER_SCHEMA_VERSION,
            },
        )
    if not secrets.compare_digest(registry.claim_token, claim_token):
        return JSONResponse(
            status_code=403,
            content={
                "error": "invalid_claim_token",
                "schema_version": EXPORTER_SCHEMA_VERSION,
            },
        )
    extraction = session.get(Extraction, extraction_id)
    if extraction is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "not_found",
                "schema_version": EXPORTER_SCHEMA_VERSION,
            },
        )
    dtcg = dtcg_for_extraction(extraction)
    if dtcg is None:
        return _not_ready_response(extraction_id)
    artifact = _build_artifact(fmt, extraction, dtcg)
    return _artifact_response(artifact)

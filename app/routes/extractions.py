"""Extraction creation and retrieval routes."""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app.auth import current_api_key, current_user
from app.constants import DEFAULT_EXTRACTION_CENTS, SCHEMA_V1
from app.db import get_db
from app.extractor_bridge import ExtractionBridgeError, ExtractionBundle, extract_design_tokens
from app.models import ApiKey, CreditLedger, Extraction, User
from app.routes.account import credit_balance
from app.schemas import ExtractionCreateRequest, ExtractionListItem, ExtractionListResponse, ExtractionResponse
from app.storage import R2Storage, get_storage

router = APIRouter()


class ExtractorCallable(Protocol):
    """Callable dependency shape for the extractor bridge."""

    def __call__(self, url: str) -> ExtractionBundle:
        """Return a successful extraction bundle or raise an extractor error."""
        ...


def get_extractor() -> ExtractorCallable:
    """FastAPI dependency returning the production extractor bridge."""
    return extract_design_tokens


def normalize_url(url: str) -> str:
    """Normalize URL for dedup and lookup without losing page identity."""
    return url.strip().lower()


def _charge(session: Session, user_id: int, api_key_id: int, extraction_id: int, balance_before: int) -> None:
    """Append an extraction debit to the user's credit ledger."""
    session.add(
        CreditLedger(
            user_id=user_id,
            entry_type="extraction_charge",
            amount_cents=-DEFAULT_EXTRACTION_CENTS,
            balance_after_cents=balance_before - DEFAULT_EXTRACTION_CENTS,
            extraction_id=extraction_id,
            api_key_id=api_key_id,
            note="Public extraction",
        )
    )


def _refund(session: Session, user_id: int, api_key_id: int, extraction_id: int) -> None:
    """Append a refund after extractor or storage failure."""
    balance_after = credit_balance(session, user_id) + DEFAULT_EXTRACTION_CENTS
    session.add(
        CreditLedger(
            user_id=user_id,
            entry_type="refund",
            amount_cents=DEFAULT_EXTRACTION_CENTS,
            balance_after_cents=balance_after,
            extraction_id=extraction_id,
            api_key_id=api_key_id,
            note="Extraction failed",
        )
    )


def _response_for(extraction: Extraction, storage: R2Storage) -> ExtractionResponse:
    """Convert an extraction row to the public response shape."""
    download_url = storage.sign_download_url(extraction.r2_zip_key) if extraction.r2_zip_key else None
    return ExtractionResponse(
        id=extraction.id,
        status=extraction.status,
        tokens=extraction.tokens_json,
        dtcg=extraction.dtcg_json,
        download_url=download_url,
        schema_version=extraction.schema_version,
        error_log=extraction.error_log,
    )


@router.post("/extractions", response_model=ExtractionResponse)
def create_extraction(
    payload: ExtractionCreateRequest,
    request: Request,
    session: Session = Depends(get_db),
    storage: R2Storage = Depends(get_storage),
    extractor: ExtractorCallable = Depends(get_extractor),
) -> ExtractionResponse | JSONResponse:
    """Create a charged extraction, persist it, and upload the ZIP bundle."""
    user: User = current_user(request)
    api_key: ApiKey = current_api_key(request)
    balance_before = credit_balance(session, user.id)
    if balance_before < DEFAULT_EXTRACTION_CENTS:
        return JSONResponse(status_code=402, content={"error": "insufficient_credit"})

    url = str(payload.url)
    extraction = Extraction(
        user_id=user.id,
        api_key_id=api_key.id,
        url=url,
        url_normalized=normalize_url(url),
        status="pending",
        schema_version=SCHEMA_V1,
        credit_cents=DEFAULT_EXTRACTION_CENTS,
    )
    session.add(extraction)
    session.flush()
    _charge(session, user.id, api_key.id, extraction.id, balance_before)
    session.commit()
    session.refresh(extraction)

    try:
        bundle = extractor(url)
        object_key, zip_sha256 = storage.put_extraction_zip(extraction.id, user.id, bundle.zip_bytes)
    except ExtractionBridgeError as exc:
        extraction.status = "failed"
        extraction.error_log = str(exc)
        _refund(session, user.id, api_key.id, extraction.id)
        session.commit()
        return JSONResponse(status_code=502, content={"error": "extractor_failed", "error_log": str(exc)})
    except Exception as exc:
        extraction.status = "failed"
        extraction.error_log = str(exc)
        _refund(session, user.id, api_key.id, extraction.id)
        session.commit()
        return JSONResponse(status_code=502, content={"error": "storage_failed", "error_log": str(exc)})

    extraction.status = "ok"
    extraction.tokens_json = bundle.tokens_json
    extraction.dtcg_json = bundle.dtcg_json
    extraction.r2_zip_key = object_key
    extraction.zip_sha256 = zip_sha256
    extraction.extracted_at = bundle.extracted_at
    extraction.schema_version = bundle.schema_version
    session.commit()
    session.refresh(extraction)
    return _response_for(extraction, storage)


@router.get("/extractions", response_model=ExtractionListResponse)
def list_extractions(
    request: Request,
    session: Session = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
    before: int | None = None,
) -> ExtractionListResponse:
    """Return newest-first paginated extraction history for the user."""
    user: User = current_user(request)
    stmt = select(Extraction).where(Extraction.user_id == user.id).order_by(Extraction.id.desc()).limit(limit)
    if before is not None:
        stmt = select(Extraction).where(Extraction.user_id == user.id, Extraction.id < before).order_by(Extraction.id.desc()).limit(limit)
    rows = session.execute(stmt).scalars().all()
    return ExtractionListResponse(items=[ExtractionListItem.model_validate(row) for row in rows], schema_version=SCHEMA_V1)


@router.get("/extractions/{extraction_id}", response_model=ExtractionResponse)
def get_extraction(
    extraction_id: int,
    request: Request,
    session: Session = Depends(get_db),
    storage: R2Storage = Depends(get_storage),
) -> ExtractionResponse | JSONResponse:
    """Return one cached extraction without charging credits again."""
    user: User = current_user(request)
    extraction = session.execute(
        select(Extraction).where(Extraction.id == extraction_id, Extraction.user_id == user.id)
    ).scalar_one_or_none()
    if extraction is None:
        return JSONResponse(status_code=404, content={"error": "not_found"})
    return _response_for(extraction, storage)


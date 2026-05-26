"""Extraction creation and retrieval routes."""
from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Protocol

import threading

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, insert, literal, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app.auth import current_api_key, current_user, utcnow
from app.constants import (
    CHARGE_MAX_RETRIES,
    EXTRACTION_PRIVATE_CENTS,
    EXTRACTION_PUBLIC_CENTS,
    SCHEMA_V1,
    SPEND_CAP_WINDOW_DAYS,
)
from app.db import get_db
from app.extractor_bridge import ExtractionBridgeError, ExtractionBundle, extract_design_tokens
from app.models import ApiKey, CreditLedger, Extraction, User
from app.routes.account import credit_balance
from app.schemas import ExtractionCreateRequest, ExtractionListItem, ExtractionListResponse, ExtractionResponse
from app.storage import R2Storage, get_storage

router = APIRouter()

# Per-user in-process serialization for the credit-charge critical section. On
# SQLite (test path) this is the only practical way to serialize concurrent
# read-then-insert against the ledger, because SQLite's snapshot isolation lets
# two writers both see a stale balance before either commits, and SELECT FOR
# UPDATE is a silent no-op on SQLite. On Postgres (production) the lock is
# additionally redundant with SELECT ... FOR UPDATE on the user row inside the
# transaction; the Python lock costs nothing meaningful (uncontended fast path)
# and guarantees the invariant in any single-process deployment.
#
# Limit: a multi-process production deployment (gunicorn -w N) MUST rely on
# Postgres FOR UPDATE for cross-process correctness. The Python lock is a
# within-process safety net, not the primary defense. The primary defense is
# (a) FOR UPDATE on Postgres, and (b) the SQL-computed balance_after_cents in
# _charge() combined with the non-negative CHECK constraint.
_user_charge_locks: dict[int, threading.Lock] = {}
_user_charge_locks_guard = threading.Lock()


def _lock_for_user(user_id: int) -> threading.Lock:
    """Return (creating if needed) the per-user charge lock."""
    with _user_charge_locks_guard:
        lock = _user_charge_locks.get(user_id)
        if lock is None:
            lock = threading.Lock()
            _user_charge_locks[user_id] = lock
        return lock


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


def extraction_price_cents(private: bool) -> int:
    """Return cents charged for public or private extraction requests."""
    return EXTRACTION_PRIVATE_CENTS if private else EXTRACTION_PUBLIC_CENTS


def spend_cap_spent_cents(session: Session, api_key_id: int) -> int:
    """Return absolute trailing-window spend for one API key."""
    window_start = utcnow() - timedelta(days=SPEND_CAP_WINDOW_DAYS)
    value = session.execute(
        select(func.coalesce(func.sum(CreditLedger.amount_cents), 0)).where(
            CreditLedger.api_key_id == api_key_id,
            CreditLedger.amount_cents < 0,
            CreditLedger.created_at >= window_start,
        )
    ).scalar_one()
    return abs(int(value))


def _charge(session: Session, user_id: int, api_key_id: int, extraction_id: int, balance_before: int, amount_cents: int) -> None:
    """Append an extraction debit to the user's credit ledger.

    Intent: race-safe debit. Two concurrent extraction POSTs from the same user
    can both read `balance_before` as the same stale value (e.g. both see $5
    when only one $5 charge can clear). If we wrote `balance_after_cents` from
    that stale arithmetic (`balance_before - amount`), BOTH inserts would land
    at 0, the CHECK constraint `balance_after_cents >= 0` would rubber-stamp
    both, and the true balance (sum of amounts) would silently go negative.

    Edge case: `balance_after_cents` is therefore computed in SQL at INSERT
    time as `COALESCE(SUM(amount_cents), 0) + (-amount)` against the live
    ledger. On Postgres the row-level write lock during INSERT plus the CHECK
    serialize the two writers; the loser's subselect sees the winner's
    committed row, yields `0 + (-amount) = -amount`, fails CHECK, raises
    IntegrityError. On SQLite (test path) writes serialize through the
    database-level lock with the same outcome. `balance_before` is retained
    only as a sanity input from the caller; the authoritative value is the
    SQL subselect below.
    """
    note = "Private extraction" if amount_cents == EXTRACTION_PRIVATE_CENTS else "Public extraction"
    live_sum = (
        select(func.coalesce(func.sum(CreditLedger.amount_cents), 0))
        .where(CreditLedger.user_id == user_id)
        .scalar_subquery()
    )
    session.execute(
        insert(CreditLedger).values(
            user_id=user_id,
            entry_type="extraction_charge",
            amount_cents=-amount_cents,
            balance_after_cents=live_sum + literal(-amount_cents),
            extraction_id=extraction_id,
            api_key_id=api_key_id,
            note=note,
        )
    )


def _refund(session: Session, user_id: int, api_key_id: int, extraction_id: int, amount_cents: int) -> None:
    """Append a refund after extractor or storage failure."""
    balance_after = credit_balance(session, user_id) + amount_cents
    session.add(
        CreditLedger(
            user_id=user_id,
            entry_type="refund",
            amount_cents=amount_cents,
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
    required_cents = extraction_price_cents(payload.private)
    balance_before = credit_balance(session, user.id)
    if balance_before < required_cents:
        return JSONResponse(
            status_code=402,
            content={
                "error": "insufficient_credit",
                "balance_cents": balance_before,
                "required_cents": required_cents,
            },
        )
    if api_key.spend_cap_cents is not None:
        spent_cents = spend_cap_spent_cents(session, api_key.id)
        if spent_cents + required_cents > api_key.spend_cap_cents:
            return JSONResponse(
                status_code=402,
                content={
                    "error": "spend_cap_exceeded",
                    "cap_cents": api_key.spend_cap_cents,
                    "spent_cents": spent_cents,
                    "window_days": SPEND_CAP_WINDOW_DAYS,
                },
            )
    # NB: this pre-lock spend-cap check is a fast-path early-fail only. It is
    # subject to the same race as the pre-lock balance check (two concurrent
    # requests can both see spent=0, both pass, then serialize at the lock and
    # both clear the cap). The authoritative re-check happens inside the
    # locked transaction below; do NOT remove it from there.

    url = str(payload.url)
    # Race-safe charge. The critical section is read-balance -> insert-charge.
    # Two defenses combine to keep the ledger honest under concurrent POSTs from
    # the same user:
    #
    #   1. Per-user lock (this `with` block) serializes the critical section
    #      within the process. See `_user_charge_locks` for the SQLite-vs-
    #      Postgres rationale.
    #
    #   2. _charge() computes balance_after_cents in SQL at INSERT time from the
    #      live ledger sum, not from the caller's possibly-stale balance read.
    #      Combined with the non-negative CHECK constraint on
    #      credit_ledger.balance_after_cents, any race that does slip past the
    #      lock (e.g. multi-process Postgres deployment without FOR UPDATE)
    #      still fails with IntegrityError rather than silently corrupting the
    #      balance. The retry loop catches that, re-reads, and 402s or retries.
    #
    # On Postgres production, additionally take a row-level lock on the user
    # row inside the transaction (SELECT ... FOR UPDATE). That gives us cross-
    # process serialization that the in-process Python lock cannot. On SQLite
    # FOR UPDATE is a silent no-op, so the Python lock IS the serialization.
    extraction: Extraction | None = None
    dialect_name = session.bind.dialect.name if session.bind is not None else ""
    with _lock_for_user(user.id):
        # Close any open snapshot from the pre-lock balance read so the next
        # SELECT inside the lock starts a fresh transaction and sees writes
        # committed by a peer thread that just released the lock.
        session.commit()
        for _attempt in range(CHARGE_MAX_RETRIES):
            if dialect_name == "postgresql":
                session.execute(
                    select(User.id).where(User.id == user.id).with_for_update()
                ).scalar_one()
            current_balance = credit_balance(session, user.id)
            if current_balance < required_cents:
                session.rollback()
                return JSONResponse(
                    status_code=402,
                    content={
                        "error": "insufficient_credit",
                        "balance_cents": current_balance,
                        "required_cents": required_cents,
                    },
                )
            # Authoritative spend-cap re-check inside the critical section. The
            # pre-lock check above is racy: two concurrent requests can both see
            # spent=0, both pass, then serialize here. Recomputing under the
            # per-user lock guarantees that at most one of N concurrent requests
            # can cross the cap. Without this, the cap is a soft hint that any
            # contention silently breaches.
            if api_key.spend_cap_cents is not None:
                current_spent = spend_cap_spent_cents(session, api_key.id)
                if current_spent + required_cents > api_key.spend_cap_cents:
                    session.rollback()
                    return JSONResponse(
                        status_code=402,
                        content={
                            "error": "spend_cap_exceeded",
                            "cap_cents": api_key.spend_cap_cents,
                            "spent_cents": current_spent,
                            "window_days": SPEND_CAP_WINDOW_DAYS,
                        },
                    )
            extraction = Extraction(
                user_id=user.id,
                api_key_id=api_key.id,
                url=url,
                url_normalized=normalize_url(url),
                status="pending",
                schema_version=SCHEMA_V1,
                credit_cents=required_cents,
            )
            session.add(extraction)
            try:
                session.flush()
                _charge(session, user.id, api_key.id, extraction.id, current_balance, required_cents)
                session.commit()
            except IntegrityError:
                session.rollback()
                extraction = None
                continue
            session.refresh(extraction)
            break
    if extraction is None:
        return JSONResponse(
            status_code=409,
            content={"error": "charge_contention", "schema_version": SCHEMA_V1},
        )

    try:
        bundle = extractor(url)
        object_key, zip_sha256 = storage.put_extraction_zip(extraction.id, user.id, bundle.zip_bytes)
    except ExtractionBridgeError as exc:
        extraction.status = "failed"
        extraction.error_log = str(exc)
        _refund(session, user.id, api_key.id, extraction.id, required_cents)
        session.commit()
        return JSONResponse(status_code=502, content={"error": "extractor_failed", "error_log": str(exc)})
    except Exception as exc:
        extraction.status = "failed"
        extraction.error_log = str(exc)
        _refund(session, user.id, api_key.id, extraction.id, required_cents)
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

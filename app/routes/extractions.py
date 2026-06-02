"""Extraction creation and retrieval routes."""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Protocol

import threading

from fastapi import APIRouter, Depends, Header, Query, Request
from sqlalchemy import func, insert, literal, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse, Response

from app.auth import current_api_key, current_user, utcnow
from app.constants import (
    AUTO_REFUND_AUDIT_SCHEMA_VERSION,
    CHARGE_MAX_RETRIES,
    EXTRACTION_PRIVATE_CENTS,
    EXTRACTION_PUBLIC_CENTS,
    SCHEMA_V1,
    SCHEMA_V1_1,
    SPEND_CAP_WINDOW_DAYS,
)
from app.db import get_db
from app.idempotency import (
    build_replay_response,
    hash_request_body,
    lookup_cached_response,
    store_response,
    validate_idempotency_key,
    validation_error_response,
)
from app.constants import IDEMPOTENCY_HEADER_NAME
from app.email import EmailSender, EmailSenderFactory, get_email_sender_factory
from app.extractor_bridge import ExtractionBridgeError, ExtractionBundle, extract_design_tokens
from app.failure_modes import (
    FailureCode,
    http_status_for,
    is_refundable,
    redact_secrets,
)
from app.models import ApiKey, AutoRefundAuditEvent, CreditLedger, Extraction, User
from app.quality_heuristics import HeuristicPenaltyResult, apply_heuristic_penalties
from app.quality_scoring import QualityScoreResult, compute_quality_score
from app.scoring_weights import DEFAULT_THRESHOLD_V1_1_X
from app.routes.account import credit_balance
from app.schemas import (
    ExtractionCreateRequest,
    ExtractionListItem,
    ExtractionListResponse,
    ExtractionManifest,
    ExtractionResponse,
    QualityScoreComponents,
)
from app.storage import R2Storage, get_storage

logger = logging.getLogger(__name__)

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


def _refund(
    session: Session,
    user_id: int,
    api_key_id: int | None,
    extraction_id: int,
    amount_cents: int,
    note: str = "Extraction failed",
) -> bool:
    """Append a refund row for one extraction; idempotent on extraction_id.

    Returns True if a new refund row was inserted, False if an existing refund
    for this extraction already exists (no-op, idempotent short-circuit per
    S20 ADR section 7). The idempotency guarantee matters because S20 wires a
    second refund pathway (quality-scoring) and we must not double-credit a
    customer whose extraction touches both paths under any race.
    """
    existing = session.execute(
        select(CreditLedger.id).where(
            CreditLedger.extraction_id == extraction_id,
            CreditLedger.entry_type == "refund",
        )
    ).scalar_one_or_none()
    if existing is not None:
        return False
    balance_after = credit_balance(session, user_id) + amount_cents
    session.add(
        CreditLedger(
            user_id=user_id,
            entry_type="refund",
            amount_cents=amount_cents,
            balance_after_cents=balance_after,
            extraction_id=extraction_id,
            api_key_id=api_key_id,
            note=note,
        )
    )
    return True


def _record_auto_refund_audit_and_notify(
    session: Session,
    extraction: Extraction,
    user: User,
    refund_amount_cents: int,
    penalty_result: HeuristicPenaltyResult,
    base_threshold: float,
    email_sender_factory: EmailSenderFactory | None,
) -> None:
    """Persist an auto-refund audit row and send the customer notification.

    Intent: the caller has just performed a successful auto-refund via
    ``_refund(...)`` (return value True). This helper records the
    customer-comms side of the event: a row in ``auto_refund_audit_events``
    plus a transactional email through Resend.

    Idempotency: the audit table has a UNIQUE constraint on ``extraction_id``.
    A duplicate INSERT raises IntegrityError inside the SAVEPOINT and only
    the audit row rolls back; the surrounding transaction (extraction-row
    update + refund-ledger insert) commits normally. The primary
    customer-comms guard against double-emailing lives in the caller (the
    ``refunded_now`` gate around this helper); this table's UNIQUE
    constraint is the second-line defense against the audit row itself.

    Edge case: email failures DO NOT block the refund. A Resend outage that
    raises during ``send_low_quality_auto_refund`` is caught, logged, and
    persisted as ``email_status="failed"`` on the audit row. The refund is
    already in the ledger before this function runs.
    """
    email_status = "skipped_no_sender"
    email_error: str | None = None

    if email_sender_factory is not None:
        try:
            sender: EmailSender = email_sender_factory()
            sender.send_low_quality_auto_refund(
                user.email,
                refund_amount_cents,
                extraction.url,
            )
            email_status = "sent"
        except Exception as send_exc:  # noqa: BLE001 - email is best-effort; never block refund
            email_status = "failed"
            email_error = repr(send_exc)
            logger.warning(
                "auto-refund email send failed extraction_id=%s error=%s",
                extraction.id,
                email_error,
            )

    audit = AutoRefundAuditEvent(
        schema_version=AUTO_REFUND_AUDIT_SCHEMA_VERSION,
        extraction_id=extraction.id,
        user_id=user.id,
        refund_amount_cents=refund_amount_cents,
        penalized_score=penalty_result.penalized_score,
        raw_score=penalty_result.original_score,
        threshold=base_threshold,
        penalties_applied=list(penalty_result.penalties_applied),
        source_url=extraction.url,
        email_status=email_status,
        email_error=email_error,
    )
    # SAVEPOINT-scoped insert. A UNIQUE-violation on (extraction_id) must
    # ONLY roll back the audit insert, not the surrounding transaction (which
    # also carries the extraction-row update and the refund-ledger insert
    # from _refund). Without `begin_nested`, a top-level rollback here would
    # discard the refund itself; with it, only this insert rolls back and
    # the surrounding work continues to commit downstream.
    try:
        with session.begin_nested():
            session.add(audit)
    except IntegrityError:
        # Duplicate audit row for the same extraction_id. The financial
        # refund has already landed via _refund; the duplicate audit attempt
        # is the no-op the unique constraint enforces.
        logger.info(
            "auto-refund audit duplicate skipped extraction_id=%s",
            extraction.id,
        )


def _tokens_url_for(extraction: Extraction, storage: R2Storage) -> str | None:
    """Mint a signed tokens.json URL for an extraction row, if uploadable.

    Returns None when the row has no `tokens_json` payload (failed or pending
    extractions) so callers do not leak signed URLs to objects that may not
    exist in R2. For rows persisted before the v1.1 tokens-upload path
    landed, the route handler will not have written a tokens.json object; the
    presigned URL would 404 if used. We tolerate this on the read path
    because pre-v1.1 clients already consume `tokens` inline; the signed URL
    is an additive convenience for v1.1+ integrators who write fresh rows.
    """
    if extraction.r2_zip_key is None:
        return None
    return storage.sign_tokens_url(storage.tokens_object_key(extraction.id, extraction.user_id))


def _manifest_for(
    extraction: Extraction,
    schema_version: int,
    tokens_url: str | None,
    download_url: str | None,
) -> ExtractionManifest:
    """Build the v1.1 manifest envelope from a persisted extraction row.

    The manifest is derived purely from existing columns plus the two
    request-scoped signed URLs; nothing about it requires a schema migration.
    `schema_version` echoes the parent response so a client persisting only
    the manifest still knows the contract it was minted against.
    """
    return ExtractionManifest(
        id=extraction.id,
        status=extraction.status,
        source_url=extraction.url,
        created_at_utc=extraction.extracted_at,
        schema_version=schema_version,
        quality_score=extraction.quality_score,
        tokens_url=tokens_url,
        download_url=download_url,
    )


def _components_for(extraction: Extraction) -> QualityScoreComponents | None:
    """Rebuild the quality-score component breakdown for a persisted row.

    Returns None when scoring did not run for this row (seed rows, pre-S20
    historical rows, failed extractions). The reconstruction is deterministic
    against persisted ``tokens_json`` and the dimension scores, so cached
    fetches produce the same diagnostic the original POST returned. We
    rerun the heuristic instead of persisting the diagnostic string because
    the diagnostic embeds observed font/color values and recomputing keeps
    the response identical without storing a denormalized blob.
    """
    if extraction.raw_quality_score is None and extraction.quality_score is None:
        return None
    # Synthesize a QualityScoreResult-like object for the heuristic. The
    # heuristic only reads `composite_score` off `base_result`, so a minimal
    # stand-in is sufficient and avoids a dependency on rebuilding the full
    # dimension-score arithmetic from persisted state.
    raw_value = extraction.raw_quality_score
    if raw_value is None:
        raw_value = extraction.quality_score or 0.0
    minimal_base = QualityScoreResult(
        schema_version="quality_score_v1@row_replay",
        composite_score=float(raw_value),
        dimension_scores=dict(extraction.quality_dimension_scores or {}),
        threshold=DEFAULT_THRESHOLD_V1_1_X,
        is_low_quality=float(raw_value) < DEFAULT_THRESHOLD_V1_1_X,
        suggestion="",
        weights_used={},
    )
    penalty = apply_heuristic_penalties(extraction.tokens_json, minimal_base)
    return QualityScoreComponents(
        schema_version=penalty.schema_version,
        raw=extraction.raw_quality_score,
        penalized=extraction.quality_score,
        threshold=DEFAULT_THRESHOLD_V1_1_X,
        penalties_applied=list(penalty.penalties_applied),
        diagnostic=penalty.diagnostic,
    )


def _response_for(extraction: Extraction, storage: R2Storage) -> ExtractionResponse:
    """Convert an extraction row to the public response shape.

    Includes S20 quality-scoring fields when the row has been scored. For
    `status="low_quality"` rows, `refunded=True` is surfaced so the customer
    sees the credit restoration without polling the ledger.

    v1.1 (R2 dispatch) additions: every successful response now carries a
    top-level `manifest` envelope plus a signed `tokens_url`. The
    `schema_version` on the response is bumped to `SCHEMA_V1_1` regardless of
    the row's own `schema_version` column (which tracks the extractor output
    contract, not the API response contract). Old fields stay populated.
    """
    download_url = storage.sign_download_url(extraction.r2_zip_key) if extraction.r2_zip_key else None
    tokens_url = _tokens_url_for(extraction, storage)
    manifest = _manifest_for(extraction, SCHEMA_V1_1, tokens_url, download_url)
    refunded: bool | None = None
    error_code: str | None = None
    error_log_field: Any = extraction.error_log
    quality_score = extraction.quality_score
    dimension_scores = extraction.quality_dimension_scores
    if extraction.status == "low_quality":
        from app.failure_modes import FailureCode  # local import: avoid cycle
        error_code = FailureCode.LOW_QUALITY_OUTPUT.value
        refunded = True
        # Inline the structured score payload as the error_log so a single
        # field carries the full S20 contract (per ADR section 6 example).
        error_log_field = {
            "schema_version": "quality_score_v1@1.0",
            "score": quality_score,
            "threshold": DEFAULT_THRESHOLD_V1_1_X,
            "dimension_scores": dimension_scores or {},
            "suggestion": _suggestion_string_from_row(dimension_scores),
        }
    return ExtractionResponse(
        id=extraction.id,
        status=extraction.status,
        tokens=extraction.tokens_json,
        dtcg=extraction.dtcg_json,
        download_url=download_url,
        # Response-shape contract version, not the extractor-output version.
        # See `ExtractionResponse` docstring for the v1 -> v1.1 bump rationale.
        schema_version=SCHEMA_V1_1,
        tokens_url=tokens_url,
        manifest=manifest,
        error_log=error_log_field,
        error_code=error_code,
        quality_score=quality_score,
        quality_dimension_scores=dimension_scores,
        raw_quality_score=extraction.raw_quality_score,
        quality_score_components=_components_for(extraction),
        refunded=refunded,
    )


def _suggestion_string_from_row(dimension_scores: dict[str, float] | None) -> str:
    """Pick the suggestion string for a stored dimension-scores dict.

    Mirrors `quality_scoring._suggestion_for` but reads from a possibly-JSON-
    deserialized dict (so values can be floats or ints). Used by `_response_for`
    when serving a cached low-quality row.
    """
    if not dimension_scores:
        return ""
    from app.quality_scoring import _DIMENSION_SCORERS  # local import: avoid cycle at module load
    from app.scoring_weights import SUGGESTIONS_BY_DIMENSION

    floats = {k: float(v) for k, v in dimension_scores.items()}
    min_score = min(floats.values())
    for name in _DIMENSION_SCORERS:
        if name in floats and floats[name] == min_score:
            return SUGGESTIONS_BY_DIMENSION.get(name, "")
    return ""


def _serialize_response_for_cache(result: ExtractionResponse | JSONResponse) -> tuple[int, str]:
    """Reduce a route return value to (status_code, body_string) for caching.

    The idempotency cache stores the body bytes verbatim so a replay
    returns the same hash a client computed against the original
    response. For a Pydantic model we dump with ``mode='json'`` and the
    same separators FastAPI would otherwise use; for a raw
    ``JSONResponse`` we decode the already-serialized body.
    """
    if isinstance(result, JSONResponse):
        return result.status_code, result.body.decode("utf-8")
    # ExtractionResponse (pydantic v2 BaseModel). FastAPI's default JSON
    # encoder respects model_dump(mode="json") for datetime serialization;
    # mirror that so the cached body matches what FastAPI would have
    # rendered on a fresh call.
    return 200, json.dumps(result.model_dump(mode="json"), separators=(",", ":"))


@router.post("/extractions", response_model=ExtractionResponse)
def create_extraction(
    payload: ExtractionCreateRequest,
    request: Request,
    session: Session = Depends(get_db),
    storage: R2Storage = Depends(get_storage),
    extractor: ExtractorCallable = Depends(get_extractor),
    email_sender_factory: EmailSenderFactory = Depends(get_email_sender_factory),
    idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER_NAME),
) -> ExtractionResponse | JSONResponse | Response:
    """Create a charged extraction, persist it, and upload the ZIP bundle.

    Optional ``Idempotency-Key`` header bounds replay safety: a retry
    within ``IDEMPOTENCY_KEY_TTL_SECONDS`` carrying the same key + same
    request body replays the original response with
    ``X-Idempotency-Replayed: true`` and does NOT re-charge credits. A
    replay with the same key but a different body is a client bug and
    returns HTTP 409. See ``app/idempotency.py``.
    """
    user_id_for_idem: int = current_user(request).id
    if idempotency_key is not None:
        validation = validate_idempotency_key(idempotency_key)
        if validation != "ok":
            return validation_error_response(validation)
        request_hash = hash_request_body(payload.model_dump(mode="json"))
        cached = lookup_cached_response(session, user_id_for_idem, idempotency_key, request_hash)
        if cached == "hash_mismatch":
            return JSONResponse(
                status_code=409,
                content={"error": "idempotency_key_reused_with_different_body"},
            )
        if cached is not None:
            return build_replay_response(cached)
    result = _create_extraction_inner(
        payload=payload,
        request=request,
        session=session,
        storage=storage,
        extractor=extractor,
        email_sender_factory=email_sender_factory,
    )
    if idempotency_key is not None:
        status_code, body_str = _serialize_response_for_cache(result)
        store_response(
            session,
            user_id_for_idem,
            idempotency_key,
            request_hash,
            status_code,
            body_str,
        )
    return result


def _create_extraction_inner(
    *,
    payload: ExtractionCreateRequest,
    request: Request,
    session: Session,
    storage: R2Storage,
    extractor: ExtractorCallable,
    email_sender_factory: EmailSenderFactory,
) -> ExtractionResponse | JSONResponse:
    """Charge + run + persist one extraction. Idempotency-free core path.

    Split out from ``create_extraction`` so the idempotency wrapper can
    cache the resulting response without duplicating the charge logic.
    All replay safety lives in the caller; this function performs the
    one-shot work and returns whatever a fresh request would.
    """
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
        # Upload tokens.json as a sibling R2 object so the v1.1 `tokens_url`
        # field on the response can sign a stable, browser-fetchable URL.
        # Failure of THIS upload is non-fatal: the ZIP already carries the
        # canonical bytes and `tokens` is also inline in the response. We
        # log via `error_log` (additive; does not flip status) and let the
        # signed URL 404 on the client. The alternative (refund and fail)
        # would punish customers for a non-critical convenience write.
        tokens_bytes = json.dumps(bundle.tokens_json, sort_keys=True).encode("utf-8")
        try:
            storage.put_extraction_tokens(extraction.id, user.id, tokens_bytes)
        except Exception as tokens_exc:  # noqa: BLE001 - convenience write; do not refund
            extraction.error_log = f"tokens.json upload failed: {tokens_exc!r}"
    except ExtractionBridgeError as exc:
        # Extractor (or bridge) failed. The bridge has already classified the
        # free-text error into a FailureCode (S15 ADR). Redact any credential-
        # shaped substrings before the message touches the HTTP response or DB.
        code: FailureCode = exc.code
        safe_log = redact_secrets(str(exc))
        extraction.status = "failed"
        extraction.error_log = safe_log
        if is_refundable(code):
            _refund(session, user.id, api_key.id, extraction.id, required_cents)
        session.commit()
        return JSONResponse(
            status_code=http_status_for(code),
            content={
                "error": "extractor_failed",
                "error_code": code.value,
                "error_log": safe_log,
                "schema_version": SCHEMA_V1,
            },
        )
    except Exception as exc:
        # Storage upload or anything else past the extractor. Treat as
        # Resemblio-attributable (PERSIST_ERROR). Refund credit.
        safe_log = redact_secrets(str(exc))
        code = FailureCode.PERSIST_ERROR
        extraction.status = "failed"
        extraction.error_log = safe_log
        _refund(session, user.id, api_key.id, extraction.id, required_cents)
        session.commit()
        return JSONResponse(
            status_code=http_status_for(code),
            content={
                "error": "storage_failed",
                "error_code": code.value,
                "error_log": safe_log,
                "schema_version": SCHEMA_V1,
            },
        )

    extraction.status = "ok"
    extraction.tokens_json = bundle.tokens_json
    extraction.dtcg_json = bundle.dtcg_json
    extraction.r2_zip_key = object_key
    extraction.zip_sha256 = zip_sha256
    extraction.extracted_at = bundle.extracted_at
    extraction.schema_version = bundle.schema_version
    session.commit()
    session.refresh(extraction)

    # S20 output-quality scoring + heuristic penalties. Runs synchronously on
    # the success path; cost is <10ms per the ADR plus a few microseconds for
    # the pure-Python heuristic pass. Seeded rows (DRL bulk-seed) skip the
    # gate so we never refund a hypothetical credit on a row that was never
    # charged. Scorer or heuristic exceptions are caught and logged into
    # `error_log`; the extraction itself stays at `status="ok"` so a scorer
    # bug cannot invalidate a real extraction. Provenance: S20 ADR sections
    # 4 + 7 + 8, plus heuristic-penalty dispatch 2026-05-31.
    #
    # Penalty wiring intent: the raw composite from `compute_quality_score`
    # measures token RICHNESS but cannot tell that a fully populated palette
    # is a generic light-mode default or that the fonts are a 100% system
    # stack (Susann extraction-fidelity finding, same date). The penalty
    # pass deducts from the composite when default-detection fires; the
    # PENALIZED score then drives both (a) what we persist as
    # `quality_score` (customer-facing) and (b) the gate that flips the
    # status to `low_quality` and triggers the refund. The raw value is
    # retained in `raw_quality_score` for audit and calibration tracking.
    if extraction.seed_source is None:
        try:
            result = compute_quality_score(extraction.tokens_json)
            penalty_result = apply_heuristic_penalties(extraction.tokens_json, result)
        except Exception as score_exc:  # noqa: BLE001 - we want any scorer crash to be non-fatal
            extraction.error_log = f"quality scoring failed: {score_exc!r}"
            session.commit()
            session.refresh(extraction)
            return _response_for(extraction, storage)
        extraction.raw_quality_score = result.composite_score
        extraction.quality_score = penalty_result.penalized_score
        extraction.quality_dimension_scores = result.dimension_scores
        # Threshold gate uses the PENALIZED score so heuristic-penalty hits
        # (e.g. all-default colors plus system-font stack on the Susann
        # extraction) correctly fall below `DEFAULT_THRESHOLD_V1_1_X` and
        # auto-refund. A row whose raw score cleared threshold but whose
        # penalized score did not is exactly the case the heuristic exists
        # to catch.
        if penalty_result.penalized_score < result.threshold:
            extraction.status = "low_quality"
            extraction.low_quality_review_pending = True
            # Idempotent refund. If an upstream code path already refunded
            # this extraction (cannot happen on the canonical success branch
            # but defended against here per ADR section 7), the helper is a
            # no-op and the customer is not double-credited.
            refunded_now = _refund(
                session,
                user.id,
                api_key.id,
                extraction.id,
                required_cents,
                note="Low-quality output auto-refund (S20)",
            )
            # R4 customer-comms: only fire the audit + email when this call
            # was the one that actually inserted the refund row. A retried or
            # duplicate path that finds an existing refund (refunded_now=False)
            # must not send a second email to the customer. The audit table's
            # UNIQUE(extraction_id) constraint is the second-line defense for
            # the same invariant.
            if refunded_now:
                _record_auto_refund_audit_and_notify(
                    session=session,
                    extraction=extraction,
                    user=user,
                    refund_amount_cents=required_cents,
                    penalty_result=penalty_result,
                    base_threshold=result.threshold,
                    email_sender_factory=email_sender_factory,
                )
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
    # Response-shape contract version, not the extractor-output version.
    # Per v1.1 brief: list endpoint must advertise schema_version=SCHEMA_V1_1
    # on both the wrapper and each per-item row so clients can switch on a
    # single version field. The canonical full envelope (manifest, signed
    # tokens_url, download_url) lives on the DETAIL endpoint by design:
    # list items stay narrow (id/url/status/extracted_at/schema_version) so
    # browse requests do not pay the cost of minting N signed R2 URLs.
    items = [
        ExtractionListItem(
            id=row.id,
            url=row.url,
            status=row.status,
            extracted_at=row.extracted_at,
            schema_version=SCHEMA_V1_1,
        )
        for row in rows
    ]
    return ExtractionListResponse(items=items, schema_version=SCHEMA_V1_1)


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

"""Account and credit balance routes."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import current_user
from app.constants import SCHEMA_V1, SCHEMA_V1_1
from app.db import get_db
from app.models import CreditLedger, User
from app.schemas import (
    AccountResponse,
    CreditBalanceResponse,
    CreditLedgerEntry,
    CreditLedgerListResponse,
)

router = APIRouter()

# Pagination bounds for GET /v1/credit/ledger. Defaults match the v1.1 dashboard
# brief: 20 entries per page is enough for the "Recent ledger entries" widget on
# /app/account without forcing a second request, and 100 is a generous ceiling
# for power users paging through history without enabling unbounded scrapes.
CREDIT_LEDGER_DEFAULT_LIMIT = 20
CREDIT_LEDGER_MAX_LIMIT = 100


def credit_balance(session: Session, user_id: int) -> int:
    """Compute a user's current balance from the append-only ledger."""
    value = session.execute(
        select(func.coalesce(func.sum(CreditLedger.amount_cents), 0)).where(CreditLedger.user_id == user_id)
    ).scalar_one()
    return int(value)


def last_ledger_at(session: Session, user_id: int) -> datetime | None:
    """Return the timestamp of the newest credit ledger entry."""
    return session.execute(
        select(func.max(CreditLedger.created_at)).where(CreditLedger.user_id == user_id)
    ).scalar_one()


@router.get("/account", response_model=AccountResponse)
def get_account(request: Request) -> AccountResponse:
    """Return metadata for the authenticated account."""
    user: User = current_user(request)
    return AccountResponse(
        email=user.email,
        status=user.status,
        created_at=user.created_at,
        stripe_customer_id=user.stripe_customer_id,
        schema_version=SCHEMA_V1,
    )


@router.get("/credit/balance", response_model=CreditBalanceResponse)
def get_credit_balance(request: Request, session: Session = Depends(get_db)) -> CreditBalanceResponse:
    """Return computed credit balance and newest ledger timestamp.

    `schema_version` is `SCHEMA_V1_1` (=2) to match the paired
    `GET /v1/credit/ledger` endpoint. A client that pins on the version
    field must get the same value from BOTH halves of the `credit` pair;
    the Stage 10 parity test in `test_schema_version_parity.py` enforces
    this invariant for every documented LIST/DETAIL or balance/ledger
    pair. The response shape itself is unchanged from V1; the bump is
    a pair-parity marker, not a contract break.
    """
    user: User = current_user(request)
    return CreditBalanceResponse(
        balance_cents=credit_balance(session, user.id),
        last_entry_at=last_ledger_at(session, user.id),
        schema_version=SCHEMA_V1_1,
    )


@router.get("/credit/ledger", response_model=CreditLedgerListResponse)
def get_credit_ledger(
    request: Request,
    limit: int = CREDIT_LEDGER_DEFAULT_LIMIT,
    offset: int = 0,
    session: Session = Depends(get_db),
) -> CreditLedgerListResponse:
    """Return paginated credit_ledger entries for the authenticated user.

    Args:
        request: FastAPI request; ``current_user`` is resolved from middleware.
        limit: Page size; clamped to ``[1, CREDIT_LEDGER_MAX_LIMIT]``. Default 20.
        offset: Row offset from the newest entry; clamped to ``>= 0``. Default 0.
        session: SQLAlchemy session injected by the ``get_db`` dependency.

    Returns:
        ``CreditLedgerListResponse`` with ``items`` ordered ``created_at DESC``,
        ``total`` (the user's full row count), and the resolved ``limit`` /
        ``offset`` so the client can render pagination without re-deriving them.

    Auth:
        Standard Bearer auth; the route always filters by the resolved
        ``current_user.id``, so a user can never see another user's ledger.

    Edge case:
        Excluded fields (``stripe_payment_intent_id``, ``api_key_id``) are
        internal-only and never returned. ``offset`` beyond ``total`` returns
        an empty ``items`` list with the unchanged ``total``; the route does
        NOT 404 in that case because clients legitimately page past the end
        on the last request of a paged scroll.
    """
    user: User = current_user(request)
    if limit < 1:
        limit = 1
    if limit > CREDIT_LEDGER_MAX_LIMIT:
        limit = CREDIT_LEDGER_MAX_LIMIT
    if offset < 0:
        offset = 0
    total = int(
        session.execute(
            select(func.count(CreditLedger.id)).where(CreditLedger.user_id == user.id)
        ).scalar_one()
    )
    rows = (
        session.execute(
            select(CreditLedger)
            .where(CreditLedger.user_id == user.id)
            .order_by(CreditLedger.created_at.desc(), CreditLedger.id.desc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    items = [CreditLedgerEntry.model_validate(row) for row in rows]
    return CreditLedgerListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        schema_version=SCHEMA_V1_1,
    )


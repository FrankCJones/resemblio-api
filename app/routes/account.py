"""Account and credit balance routes."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import current_user
from app.constants import SCHEMA_V1
from app.db import get_db
from app.models import CreditLedger, User
from app.schemas import AccountResponse, CreditBalanceResponse

router = APIRouter()


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
    """Return computed credit balance and newest ledger timestamp."""
    user: User = current_user(request)
    return CreditBalanceResponse(
        balance_cents=credit_balance(session, user.id),
        last_entry_at=last_ledger_at(session, user.id),
        schema_version=SCHEMA_V1,
    )


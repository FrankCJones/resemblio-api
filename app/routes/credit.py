"""Credit purchase routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app.auth import current_user
from app.constants import SCHEMA_V1, TOPUP_MAX_CENTS, TOPUP_MIN_CENTS
from app.db import get_db
from app.models import TopupSession, User
from app.payments import StripeGateway, get_stripe_service
from app.schemas import CreditTopupRequest, CreditTopupResponse

router = APIRouter()


@router.post("/credit/topup", response_model=CreditTopupResponse)
def create_credit_topup(
    payload: CreditTopupRequest,
    request: Request,
    session: Session = Depends(get_db),
    stripe_service: StripeGateway = Depends(get_stripe_service),
) -> CreditTopupResponse | JSONResponse:
    """Create a Stripe Checkout Session for a credit top-up."""
    user: User = current_user(request)
    if payload.amount_cents < TOPUP_MIN_CENTS:
        return JSONResponse(
            status_code=400,
            content={
                "error": "topup_minimum_not_met",
                "minimum_cents": TOPUP_MIN_CENTS,
                "schema_version": SCHEMA_V1,
            },
        )
    if payload.amount_cents > TOPUP_MAX_CENTS:
        # Hard ceiling defends against typos and abuse where a single Checkout
        # session authorizes an unintentionally large amount on the user's card.
        return JSONResponse(
            status_code=400,
            content={
                "error": "topup_maximum_exceeded",
                "maximum_cents": TOPUP_MAX_CENTS,
                "schema_version": SCHEMA_V1,
            },
        )
    db_user = session.get(User, user.id)
    if db_user is None:
        return JSONResponse(status_code=404, content={"error": "not_found", "schema_version": SCHEMA_V1})
    if not db_user.stripe_customer_id:
        return JSONResponse(
            status_code=409,
            content={"error": "stripe_customer_missing", "schema_version": SCHEMA_V1},
        )
    checkout = stripe_service.create_checkout_session(db_user.id, db_user.stripe_customer_id, payload.amount_cents)
    # Record the server-side TopupSession AFTER Stripe gives us the session id but
    # BEFORE we return to the caller. The webhook handler will refuse to credit
    # any session that lacks a matching row, so this insert is what binds the
    # Stripe session id to a specific user_id and amount on our side. The unique
    # PK on session id also makes accidental duplicate creation impossible.
    session.add(
        TopupSession(
            id=checkout.id,
            user_id=db_user.id,
            amount_cents=payload.amount_cents,
            status="pending",
        )
    )
    session.commit()
    return CreditTopupResponse(
        checkout_session_id=checkout.id,
        checkout_url=checkout.url,
        schema_version=SCHEMA_V1,
    )

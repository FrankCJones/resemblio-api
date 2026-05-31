"""SQLAlchemy ORM models for the Resemblio API schema."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.constants import DEFAULT_API_SCOPE, DEFAULT_EXTRACTION_CENTS
from app.db import Base

BigIntType = BigInteger().with_variant(Integer, "sqlite")
JsonType = postgresql.JSONB(astext_type=Text()).with_variant(JSON(), "sqlite")
InetType = postgresql.INET().with_variant(String(64), "sqlite")


class User(Base):
    """Account owner. Credits and API keys are scoped to this row."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    stripe_customer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    api_keys: Mapped[list[ApiKey]] = relationship(back_populates="user")
    extractions: Mapped[list[Extraction]] = relationship(back_populates="user")
    ledger_entries: Mapped[list[CreditLedger]] = relationship(back_populates="user")

    __table_args__ = (Index("ix_users_email_lower", func.lower(email), unique=True),)


class ApiKey(Base):
    """Hashed API credential with lifecycle state and rotation grace."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigIntType, ForeignKey("users.id"), nullable=False)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    key_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=lambda: [DEFAULT_API_SCOPE])
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_from_ip: Mapped[str | None] = mapped_column(InetType, nullable=True)
    grace_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    spend_cap_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)

    user: Mapped[User] = relationship(back_populates="api_keys")
    events: Mapped[list[ApiKeyEvent]] = relationship(back_populates="api_key")
    extractions: Mapped[list[Extraction]] = relationship(back_populates="api_key")
    ledger_entries: Mapped[list[CreditLedger]] = relationship(back_populates="api_key")

    __table_args__ = (
        Index("ix_api_keys_user_id", "user_id"),
        Index("ix_api_keys_status", "status"),
        Index("ix_api_keys_grace_expires_at", "grace_expires_at"),
    )


class ApiKeyEvent(Base):
    """Append-only API key lifecycle and usage audit event."""

    __tablename__ = "api_key_events"

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True, autoincrement=True)
    api_key_id: Mapped[int] = mapped_column(BigIntType, ForeignKey("api_keys.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    ip: Mapped[str | None] = mapped_column(InetType, nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column("metadata", JsonType, nullable=True)

    api_key: Mapped[ApiKey] = relationship(back_populates="events")

    __table_args__ = (Index("ix_api_key_events_api_key_id_occurred_at", "api_key_id", "occurred_at"),)


class Extraction(Base):
    """Persisted extraction result and R2 bundle pointer."""

    __tablename__ = "extractions"

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigIntType, ForeignKey("users.id"), nullable=False)
    # ``api_key_id`` is nullable so the DRL bulk-seed script
    # (``scripts/seed_from_drl.py``) can write seed rows that are not owned by
    # an API key. Organic extractions always populate it.
    api_key_id: Mapped[int | None] = mapped_column(BigIntType, ForeignKey("api_keys.id"), nullable=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    url_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    tokens_json: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    dtcg_json: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    r2_zip_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    zip_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    credit_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=DEFAULT_EXTRACTION_CENTS, server_default=str(DEFAULT_EXTRACTION_CENTS))
    # Seed-row provenance pair. NULL on organic rows. ``(seed_source, source_id)``
    # is the idempotency key for the DRL bulk-seed script under a partial
    # unique index where ``seed_source IS NOT NULL``.
    seed_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # S20 output-quality scoring columns. Migration 0008. ``quality_score`` is
    # the composite 0.0-1.0 score; ``quality_dimension_scores`` is the per-
    # dimension JSON. ``low_quality_review_pending`` is the operator queue
    # flag (indexed). ``reviewed_at`` / ``verdict`` / ``reviewer`` close the
    # review loop. See ``app/quality_scoring.py``.
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    quality_dimension_scores: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    low_quality_review_pending: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    low_quality_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    low_quality_review_verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)
    low_quality_reviewer: Mapped[str | None] = mapped_column(String(64), nullable=True)

    user: Mapped[User] = relationship(back_populates="extractions")
    api_key: Mapped[ApiKey] = relationship(back_populates="extractions")
    ledger_entries: Mapped[list[CreditLedger]] = relationship(back_populates="extraction")

    __table_args__ = (
        Index("ix_extractions_user_id_extracted_at", "user_id", "extracted_at"),
        Index("ix_extractions_url_normalized", "url_normalized"),
        Index("ix_extractions_low_quality_review_pending", "low_quality_review_pending"),
    )


class CreditLedger(Base):
    """Append-only credit ledger. Balances are the sum of signed entries."""

    __tablename__ = "credit_ledger"

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigIntType, ForeignKey("users.id"), nullable=False)
    entry_type: Mapped[str] = mapped_column(Text, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_id: Mapped[int | None] = mapped_column(BigIntType, ForeignKey("extractions.id"), nullable=True)
    api_key_id: Mapped[int | None] = mapped_column(BigIntType, ForeignKey("api_keys.id"), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    user: Mapped[User] = relationship(back_populates="ledger_entries")
    extraction: Mapped[Extraction | None] = relationship(back_populates="ledger_entries")
    api_key: Mapped[ApiKey | None] = relationship(back_populates="ledger_entries")

    # The non-negative CHECK on balance_after_cents is the database-level invariant
    # that prevents concurrent extraction charges from racing past zero. Application
    # code computes balance_before -> required, but two concurrent requests can both
    # read balance_before = $5 and both insert a $5 charge unless the DB rejects the
    # second insert. With the CHECK, the loser's insert raises IntegrityError and
    # the route retries (recomputing balance) or returns 402 insufficient_credit.
    __table_args__ = (
        Index("ix_credit_ledger_user_id_created_at", "user_id", "created_at"),
        CheckConstraint("balance_after_cents >= 0", name="ck_credit_ledger_balance_non_negative"),
    )


class TopupSession(Base):
    """Server-recorded Stripe Checkout session for credit top-up.

    The webhook handler refuses to credit any incoming checkout.session.completed
    event whose session id is not present in this table, which closes the
    ownership-spoof gap (an attacker cannot forge a webhook that credits another
    user even if they discover that user's user_id, because the session id will
    not exist server-side, or will be bound to a different user_id).

    Also tracks status so the webhook can refuse to double-credit a single
    session if Stripe redelivers after the row has already been marked completed.
    """

    __tablename__ = "topup_sessions"

    # Stripe Checkout session id (e.g. "cs_test_...") is the natural PK; uniqueness
    # is enforced by Stripe and gives us idempotency on the session-id dimension
    # in addition to the event-id dimension covered by stripe_events_seen.
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigIntType, ForeignKey("users.id"), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending", server_default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_topup_sessions_user_id", "user_id"),)


class StripeEventSeen(Base):
    """Processed Stripe event id for webhook idempotency.

    Stateful: a row is inserted in ``processing`` state as the first step of
    handling. The status flips to ``processed`` only after the handler's side
    effects (credit ledger row, email send) complete. If the handler raises or
    crashes mid-flight, the row is rolled back so Stripe's redelivery can claim
    the event id fresh and finish the credit. Without this, marking the event
    seen up front would let a partial failure permanently strand the customer:
    redelivery would see the existing row and short-circuit to a duplicate 200
    while the credit had never landed.
    """

    __tablename__ = "stripe_events_seen"

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="processed", server_default="processed")
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    # ``claimed_at`` records when a row last entered the ``processing`` state.
    # The webhook handler uses it as a lease: a ``processing`` row whose
    # ``claimed_at`` is older than ``_STALE_PROCESSING_LEASE_SECONDS`` (see
    # ``app/routes/webhooks.py``) is treated as abandoned and may be re-claimed
    # by a fresh delivery. Without this, a handler crash that prevents
    # ``_mark_event_failed`` from committing would strand the row at
    # ``processing`` forever, and every subsequent redelivery would short-
    # circuit to the in-flight branch with no credit ever landing.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, server_default=func.now())

    __table_args__ = (Index("ix_stripe_events_seen_event_id", "event_id", unique=True),)

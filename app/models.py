"""SQLAlchemy ORM models for the Resemblio API schema."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, func
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
    api_key_id: Mapped[int] = mapped_column(BigIntType, ForeignKey("api_keys.id"), nullable=False)
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

    user: Mapped[User] = relationship(back_populates="extractions")
    api_key: Mapped[ApiKey] = relationship(back_populates="extractions")
    ledger_entries: Mapped[list[CreditLedger]] = relationship(back_populates="extraction")

    __table_args__ = (
        Index("ix_extractions_user_id_extracted_at", "user_id", "extracted_at"),
        Index("ix_extractions_url_normalized", "url_normalized"),
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

    __table_args__ = (Index("ix_credit_ledger_user_id_created_at", "user_id", "created_at"),)


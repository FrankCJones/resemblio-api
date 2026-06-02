"""SQLAlchemy ORM models for the Resemblio API schema."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
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
    # Retained TEST-mode customer id, populated by the customer-reconciliation
    # helper when a user's primary ``stripe_customer_id`` is rewritten from a
    # TEST value to a LIVE value during a Stripe mode cutover. Forensic-only;
    # the application never reads this field at runtime. Migration 0011.
    stripe_customer_id_test: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
    # ``kind`` classifies how the key is used. ``'user'`` keys are minted by
    # the user from the dashboard and shown in the visible-keys list. The BFF
    # key minted at signup is ``'internal_bff'``: it powers the Next.js web
    # app's server-side proxy and is never displayed to the user.
    # ``'service'`` is reserved for internal automation. Migration 0013.
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="user", server_default="user")
    # Whether this key appears in the dashboard "your API keys" list. Derived
    # from ``kind`` at insert time but stored separately so a service key can
    # be flipped invisible without rewriting its audit ``kind``. Migration 0013.
    is_visible_to_user: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

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
    # The DTCG snapshot for this extraction lives on the joined
    # ``asset_versions`` row (FK below). Migration 0018 dropped the legacy
    # denormalized ``extractions.dtcg_json`` column; the ORM mapping was
    # removed in the same commit. Read via
    # ``app/asset_versions.py:dtcg_for_extraction``.
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
    # Raw composite from ``compute_quality_score`` BEFORE
    # ``apply_heuristic_penalties`` runs. ``quality_score`` carries the
    # penalized value (customer-facing + gate for the refund path);
    # ``raw_quality_score`` is the audit field that keeps the base scorer
    # vs heuristic calibration drift observable from row data alone.
    # Migration 0009. See ``app/quality_heuristics.py``.
    raw_quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    quality_dimension_scores: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    low_quality_review_pending: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    low_quality_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    low_quality_review_verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)
    low_quality_reviewer: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Library FK (migration 0016). Nullable: organic rows post-0016 set this
    # value at creation time; historical rows are backfilled in 0017; seed
    # rows older than 0017 stay NULL (no DTCG payload to hash).
    asset_version_id: Mapped[int | None] = mapped_column(
        BigIntType, ForeignKey("asset_versions.id"), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="extractions")
    api_key: Mapped[ApiKey] = relationship(back_populates="extractions")
    ledger_entries: Mapped[list[CreditLedger]] = relationship(back_populates="extraction")
    asset_version: Mapped["AssetVersion | None"] = relationship(back_populates="extractions")

    __table_args__ = (
        Index("ix_extractions_user_id_extracted_at", "user_id", "extracted_at"),
        Index("ix_extractions_url_normalized", "url_normalized"),
        Index("ix_extractions_low_quality_review_pending", "low_quality_review_pending"),
        Index("ix_extractions_asset_version_id", "asset_version_id"),
    )


class AssetVersion(Base):
    """Deduplicated DTCG snapshot of one URL at one moment in time.

    The library refactor (migrations 0015-0018, see brain-dump library
    architecture) decouples the per-extraction billing/audit row from the
    per-URL content snapshot. Many ``extractions`` rows for the same URL
    that produce identical DTCG output collapse to a single
    ``asset_versions`` row via the ``(url, content_hash)`` dedup key.

    Identity contract
    -----------------
    ``content_hash`` is the SHA-256 of the canonical-JSON serialization of
    ``dtcg_json`` (sort_keys=True, separators=(",", ":"),
    ensure_ascii=False). See ``app/asset_versions.py:canonicalize_dtcg``;
    callers must hash via that helper so the value matches across the
    extraction-creation path, the backfill migration (0017), and any
    future library-hit lookup.

    Public-corpus visibility
    ------------------------
    ``is_public`` is FALSE for every row written in v1.1. The v1.2
    moderation tooling flips selected rows TRUE; the partial index
    ``ix_asset_versions_is_public_fetched_at`` makes the public-browse
    query cheap once that flag flips on.
    """

    __tablename__ = "asset_versions"

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    dtcg_json: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    # Future-use slot for a per-snapshot ZIP pointer that may diverge from
    # the per-extraction ``extractions.r2_zip_key``. Stays NULL in v1.1.
    raw_assets_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    manifest_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="2"
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Audit-only; the runtime read path does not enforce ownership against
    # this field. Nullable for transformer-seeded or system-generated rows.
    first_extracted_by_user_id: Mapped[int | None] = mapped_column(
        BigIntType, ForeignKey("users.id"), nullable=True
    )
    is_public: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    version_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    extractions: Mapped[list[Extraction]] = relationship(back_populates="asset_version")

    __table_args__ = (
        Index("ix_asset_versions_url_fetched_at", "url", "fetched_at"),
        Index("ix_asset_versions_content_hash", "content_hash"),
        Index("ix_asset_versions_is_public_fetched_at", "is_public", "fetched_at"),
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


class AutoRefundAuditEvent(Base):
    """Append-only record of an S20 auto-refund event with email-send status.

    Separate from ``credit_ledger`` because credit_ledger is a financial record
    and this table is a customer-communication and operational-audit record.
    Mixing email-send status into credit_ledger would muddy the financial
    audit. See migration 0010 for the full rationale.

    Idempotency: ``extraction_id`` is unique. The route handler is expected to
    treat a duplicate-key insert as a no-op (the credit-ledger refund row is
    the primary financial idempotency gate; this table is the secondary
    customer-comms gate).
    """

    __tablename__ = "auto_refund_audit_events"

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True, autoincrement=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    extraction_id: Mapped[int] = mapped_column(BigIntType, ForeignKey("extractions.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(BigIntType, ForeignKey("users.id"), nullable=False)
    refund_amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    penalized_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    penalties_applied: Mapped[list[str] | None] = mapped_column(JsonType, nullable=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    # Plain-text vocabulary: "sent" | "failed" | "skipped_no_sender". A DBA
    # can read the table directly without decoding constants.
    email_status: Mapped[str] = mapped_column(String(32), nullable=False)
    email_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_auto_refund_audit_events_extraction_id", "extraction_id", unique=True),
        Index("ix_auto_refund_audit_events_created_at", "created_at"),
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


class MagicLinkToken(Base):
    """Single-use, time-limited token for passwordless signup/login.

    The plaintext token is never persisted; only the SHA-256 ``token_hash``
    is. ``email`` is the lookup key because the user row may not yet exist
    at the moment the link is requested (anti-enumeration semantics in the
    request endpoint). Single-use is enforced by flipping ``consumed_at``
    from NULL to a UTC timestamp at redemption time; once set, the row is
    rejected for any subsequent redemption attempt. Migration 0012.
    """

    __tablename__ = "magic_link_tokens"

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_magic_link_tokens_email", "email"),
        Index("ix_magic_link_tokens_token_hash", "token_hash", unique=True),
    )


class IdempotencyKey(Base):
    """Cached HTTP response keyed by ``(user_id, key)`` for replay safety.

    Purpose: a client retrying ``POST /v1/extractions`` after a transient
    network failure must not be charged twice. The client passes
    ``Idempotency-Key: <token>``; the route looks up
    ``(user_id, key)``, and on hit replays the cached HTTP status + body.

    ``request_hash`` (SHA-256 of the canonical request body) guards against
    the "same key, different body" misuse: a client that reuses one
    idempotency token across two semantically distinct requests is always
    a bug; the route rejects the second with HTTP 409.

    TTL: rows older than ``IDEMPOTENCY_KEY_TTL_SECONDS`` are treated as
    expired at lookup time. A separate sweep job (deferred; not v1.1) can
    prune them; until then ``created_at`` is indexed so a manual
    ``DELETE ... WHERE created_at < ...`` runs cheaply. Migration 0014.
    """

    __tablename__ = "idempotency_keys"

    user_id: Mapped[int] = mapped_column(BigIntType, ForeignKey("users.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_idempotency_keys_created_at", "created_at"),
    )


class WebSessionKey(Base):
    """Maps a user to the ApiKey row currently acting as their BFF session key.

    Exactly one active BFF key per user (UNIQUE on ``api_key_id``). On a new
    login, the existing BFF key (if any) is revoked and a fresh ApiKey row
    of ``kind='internal_bff'`` is minted; the corresponding row here is
    replaced and ``rotated_at`` on the predecessor is set for audit. The
    plaintext key value is NEVER stored in this table; the row only points
    at the api_keys row whose ``key_hash`` already exists, which means a
    leak of this table alone cannot grant API access. Migration 0013.
    """

    __tablename__ = "web_session_keys"

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigIntType, ForeignKey("users.id"), nullable=False)
    api_key_id: Mapped[int] = mapped_column(BigIntType, ForeignKey("api_keys.id"), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_web_session_keys_user_id", "user_id"),
        Index("ix_web_session_keys_api_key_id", "api_key_id", unique=True),
    )


class LibraryIndexJob(Base):
    """Queue row for the library indexer service (mission Phase 4).

    The indexer drains rows in ``pending`` state, moves them to ``running``,
    runs the DRL compose pipeline against the referenced ``asset_versions``
    row, and writes per-page renders into ``library_pages``. On success the
    row flips to ``complete``; on transient failure it flips back to
    ``pending`` with ``attempts`` incremented; once ``attempts`` exceeds
    ``LIBRARY_INDEX_MAX_ATTEMPTS`` the row is parked at ``failed`` for
    operator triage via ``last_error``. Migration 0019.
    """

    __tablename__ = "library_index_jobs"

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True, autoincrement=True)
    asset_version_id: Mapped[int] = mapped_column(
        BigIntType, ForeignKey("asset_versions.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_library_index_jobs_status_enqueued_at", "status", "enqueued_at"),
        Index("ix_library_index_jobs_asset_version_id", "asset_version_id"),
    )


class LibraryPage(Base):
    """One per-category compose render for an ``asset_versions`` row.

    Read directly by the Next.js library routes (Phase 5) to render
    ``/library/<brand_slug>/<category_slug>/`` and its versioned variants.
    ``is_canonical`` marks the latest version per ``(brand_slug,
    category_slug)``; the canonical-brand page reads only TRUE rows while
    versioned pages read every row regardless of the flag. ``metadata_json``
    carries the token subset + sample text + display font that powers OG
    image generation and page copy interpolation. Migration 0020.
    """

    __tablename__ = "library_pages"

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True, autoincrement=True)
    asset_version_id: Mapped[int] = mapped_column(
        BigIntType, ForeignKey("asset_versions.id"), nullable=False
    )
    category_slug: Mapped[str] = mapped_column(Text, nullable=False)
    brand_slug: Mapped[str] = mapped_column(Text, nullable=False)
    version_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    rendered_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    is_canonical: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("asset_version_id", "category_slug", name="uq_library_pages_asset_version_category"),
        Index("ix_library_pages_brand_category", "brand_slug", "category_slug"),
        Index("ix_library_pages_is_canonical", "is_canonical"),
    )

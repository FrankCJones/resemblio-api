"""Pydantic request and response schemas for the v1 API."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional, TypedDict

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator


class ErrorBody(TypedDict):
    """JSON error shape returned by auth and route guards."""

    error: str


class ExtractionCreateRequest(BaseModel):
    """Request body for creating a charged extraction."""

    url: AnyHttpUrl
    private: bool = False


class ExtractionManifest(BaseModel):
    """Top-level extraction envelope, additive in `schema_version=2` (v1.1 brief Section 3).

    The manifest is the canonical pointer record a client persists; the inline
    `tokens` + `dtcg` payload on the parent response is a convenience for
    one-shot integrations that don't want a second round-trip. Field set is
    intentionally narrow: anything that belongs in storage (the actual token
    payload, the ZIP bytes) is exposed via signed URLs, not embedded.

    Edge case: `tokens_url` and `download_url` are presigned and TTL-bounded
    (24h and 15 min respectively per `app.constants`). Clients caching the
    manifest beyond those windows must re-fetch the parent extraction to
    obtain fresh URLs. `id`, `source_url`, `created_at_utc`, and
    `schema_version` are stable for the life of the row.
    """

    id: int
    status: str
    source_url: str
    created_at_utc: datetime
    schema_version: int
    quality_score: float | None = None
    tokens_url: str | None = None
    download_url: str | None = None


class QualityScoreComponents(BaseModel):
    """Audit breakdown of how ``quality_score`` was computed for one extraction.

    Surfaced on successful extraction responses (both ``ok`` and
    ``low_quality``) so customers and operators can see the raw composite,
    each heuristic penalty that fired, the resulting penalized score, and the
    threshold used to gate the refund path. The route handler builds this
    from ``QualityScoreResult`` (raw) and ``HeuristicPenaltyResult``
    (penalized + penalty names + diagnostic).

    Edge case: ``raw`` and ``penalized`` are both null on rows where scoring
    did not run (seed rows, pre-S20 historical rows). ``penalties_applied``
    is an empty tuple when no heuristic triggered; ``diagnostic`` is the
    string ``"no penalties"`` in that case.
    """

    schema_version: str
    raw: float | None
    penalized: float | None
    threshold: float
    penalties_applied: list[str]
    diagnostic: str


class ExtractionResponse(BaseModel):
    """Extraction detail returned after creation or cached fetch.

    `schema_version` semantics:
      - `1` was the v1.1 S1 shape (tokens inline, ZIP via `download_url`).
      - `2` is the v1.1 R2-dispatch shape: additive `manifest` envelope and
        signed `tokens_url`. Old fields stay populated; v1 clients keep
        working unchanged. Bump rationale: v1.1 mission brief Section 3.
    """

    id: int
    status: str
    tokens: dict[str, Any] | None
    dtcg: dict[str, Any] | None
    download_url: str | None
    schema_version: int
    # v1.1 additive fields (schema_version=2). `tokens_url` is null when the
    # tokens.json object has not been (or could not be) uploaded for this row
    # (e.g. pre-v1.1 rows seeded before the upload path landed; storage write
    # failure on POST is reported via `error_log` and `tokens_url` stays null).
    # `manifest` is the canonical envelope per the v1.1 mission brief.
    tokens_url: str | None = None
    manifest: ExtractionManifest | None = None
    error_log: str | Any | None = None
    # S20 additive fields. Present on `status="low_quality"` responses; null
    # on `status="ok"` and on failure responses (those carry a string
    # `error_log` and are returned via JSONResponse in the route handler).
    error_code: str | None = None
    quality_score: float | None = None
    quality_dimension_scores: dict[str, float] | None = None
    # Raw composite from the base scorer BEFORE heuristic penalties. The
    # customer-facing ``quality_score`` field above carries the penalized
    # value; ``raw_quality_score`` is the audit field that lets a customer
    # see the unmodified base score for context. Null on rows where scoring
    # did not run. Added by the heuristic-penalty wiring dispatch 2026-05-31.
    raw_quality_score: float | None = None
    # Per-component breakdown of the quality score: raw, each penalty fired,
    # penalized result, threshold. Null when scoring did not run.
    quality_score_components: QualityScoreComponents | None = None
    refunded: bool | None = None
    # A1.1 additive field (2026-06-04). When non-null, lists lowercase hex
    # strings the rendered page shows but the declared-token + computed-
    # style signals missed. A non-empty list flags a likely "stock default
    # extraction" pathology on WordPress + page-builder sites where
    # theme.json declares Gutenberg defaults the visible site never uses.
    # Null when: the screenshot-palette pass was unavailable, the pass
    # errored, the surviving-color set was empty (palette complete), the
    # extraction was served from a cached row (the warning is computed
    # per-extraction and not persisted), or the row predates A1.1. This
    # is an ADDITIVE extension of `schema_version=2`; v1.1 R2 clients
    # parsing v2 must ignore unknown fields per the v1.1 brief.
    palette_completeness_warning: list[str] | None = None


class ExtractionListItem(BaseModel):
    """Compact extraction row for history lists."""

    id: int
    url: str
    status: str
    extracted_at: datetime
    schema_version: int

    model_config = ConfigDict(from_attributes=True)


class ExtractionListResponse(BaseModel):
    """Paginated extraction history response."""

    items: list[ExtractionListItem]
    schema_version: int


class ConvertRenderedArtifacts(BaseModel):
    """Optional render artifacts emitted alongside a converter payload.

    Only the shadcn converter currently populates this block; figma returns
    no rendered artifacts (the FigmaVariablesPayload IS the importable
    artifact). Kept as a dedicated model so the response shape is stable
    when future converters add their own render outputs.
    """

    globals_css: str | None = None
    tailwind_config_excerpt: str | None = None


class ConvertResponse(BaseModel):
    """Public response shape for ``POST /v1/convert/<target>/{extraction_id}``.

    ``payload`` carries the converter-specific dict (shadcn theme or figma
    variables payload, dumped via ``model_dump(by_alias=True)``). ``rendered``
    is omitted when the target produces no render artifacts (figma).

    Conversion is FREE in v1: the extraction was already paid for at creation
    time and conversion is value-add on top, per the pricing ladder in
    ``projects/Resemblio/CLAUDE.md``. No ledger debit is appended.
    """

    schema_version: int
    extraction_id: int
    target: str
    payload: dict[str, Any]
    rendered: ConvertRenderedArtifacts | None = None


class ApiKeyCreateRequest(BaseModel):
    """Request body for creating an API key."""

    label: str = Field(min_length=1, max_length=120)


class ApiKeyCreatedResponse(BaseModel):
    """API key creation response. Plaintext is returned once."""

    id: int
    api_key: str
    key_prefix: str
    label: str
    schema_version: int


class ApiKeyListItem(BaseModel):
    """Display-safe API key metadata.

    The ``key_prefix`` is the first 8 chars of the plaintext key minted at
    creation; suffix is irrecoverable (only ``key_hash`` is stored). The
    dashboard renders this as ``{key_prefix}***`` so the user can recognize
    a key they previously saved without exposing the full secret. Pluggable
    in the schema; the rendering convention lives in the web layer.
    """

    id: int
    key_prefix: str
    label: str
    scopes: list[str]
    status: str
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    grace_expires_at: datetime | None
    spend_cap_cents: int | None

    model_config = ConfigDict(from_attributes=True)


class ApiKeyAuditEvent(BaseModel):
    """One ``api_key_events`` row in the dashboard audit drawer.

    ``metadata`` is the JSON column persisted as ``metadata_json``; renamed
    here because ``metadata`` is the field the customer sees. Free-form
    by design (keys vary by event_type); the dashboard renders the JSON
    blob verbatim.
    """

    id: int
    event_type: str
    occurred_at: datetime
    ip: str | None
    metadata: dict[str, Any] | None


class ApiKeyAuditResponse(BaseModel):
    """Cursor-paginated audit-event response for a single key."""

    items: list[ApiKeyAuditEvent]
    schema_version: int


class ApiKeyListResponse(BaseModel):
    """List API keys without plaintext material."""

    items: list[ApiKeyListItem]
    schema_version: int


RevokeReason = Literal["lost", "rotated", "no_longer_needed", "suspected_compromise", "leaked_detected", "admin"]


class ApiKeyRevokeRequest(BaseModel):
    """Request body for revoking an API key."""

    reason: RevokeReason


class ApiKeyStatusResponse(BaseModel):
    """Mutation response for rotate and revoke endpoints."""

    id: int
    status: str
    key_prefix: str
    schema_version: int


class ApiKeySpendCapRequest(BaseModel):
    """Request body for setting or clearing an API key spend cap."""

    cap_cents: int | None = Field(default=None, ge=0)


class ApiKeySpendCapResponse(BaseModel):
    """Response returned after an API key spend-cap update."""

    id: int
    spend_cap_cents: int | None
    schema_version: int


class AccountResponse(BaseModel):
    """Current account metadata."""

    email: str
    status: str
    created_at: datetime
    stripe_customer_id: str | None
    schema_version: int


class CreditBalanceResponse(BaseModel):
    """Computed credit balance for the current user."""

    balance_cents: int
    last_entry_at: datetime | None
    schema_version: int


class CreditLedgerEntry(BaseModel):
    """Public, customer-safe view of a single credit_ledger row.

    Intentionally omits ``stripe_payment_intent_id`` and ``api_key_id``;
    both are internal-only fields per defensive-design (the v1.1 dashboard
    surface has no need to expose payment-processor or key-identifier values
    to the browser, and exposing them in a JSON response widens the blast
    radius of any future log-leak / Sentry breadcrumb regression).
    """

    id: int
    entry_type: str
    amount_cents: int
    balance_after_cents: int
    extraction_id: int | None
    note: str | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CreditLedgerListResponse(BaseModel):
    """Offset-paginated ledger view for the authenticated user.

    Convention follows the v1.1 dashboard brief: ``items`` newest-first,
    ``total`` reflects the full row count for the user (not the page size),
    and ``limit`` / ``offset`` echo the resolved (clamped) values the route
    actually used so the client can render pagination controls without
    re-deriving them.
    """

    items: list[CreditLedgerEntry]
    total: int
    limit: int
    offset: int
    schema_version: int


class CreditTopupRequest(BaseModel):
    """Request body for starting a Stripe Checkout credit top-up."""

    amount_cents: int


class CreditTopupResponse(BaseModel):
    """Checkout session details returned to the caller."""

    checkout_session_id: str
    checkout_url: str
    schema_version: int


class InternalCheckoutSessionRequest(BaseModel):
    """S3b Wave 2c body for ``POST /v1/internal/billing/create_checkout_session``.

    Called by the Next.js BFF on the user's behalf. ``user_id`` identifies
    the user whose reconciled Stripe customer id is used for the Checkout
    session; ``amount_cents`` must match one of the bundle tiers in
    ``TOPUP_BUNDLE_ACCEPTED_PAID_CENTS`` (closed set; off-list values 400).

    Edge case: the route also accepts optional ``success_url`` and
    ``cancel_url`` overrides so the BFF can include the Stripe-templated
    ``{CHECKOUT_SESSION_ID}`` placeholder in the return URLs. Both URLs are
    pass-through to Stripe; the StripeClient ignores them on this path and
    falls back to its configured defaults (the override is wired but not
    yet plumbed through the gateway protocol; deferred to a follow-on).
    """

    user_id: int
    amount_cents: int
    success_url: str | None = None
    cancel_url: str | None = None


class InternalCheckoutSessionResponse(BaseModel):
    """S3b Wave 2c response from the internal billing surface."""

    checkout_url: str
    session_id: str
    schema_version: int


class StripeCheckoutMetadata(BaseModel):
    """Metadata expected on Checkout sessions created for credit top-ups."""

    user_id: str | None = None
    purpose: str | None = None

    model_config = ConfigDict(extra="allow")


class StripeCheckoutSessionPayload(BaseModel):
    """Stripe Checkout session fields used by the webhook handler.

    ``payment_status`` is included because Stripe's ``checkout.session.completed``
    event fires for delayed-payment methods (SEPA debit, bank debit, OXXO, etc.)
    while funds are still ``processing`` or ``unpaid``. v1.1 restricts Checkout
    to card-only via ``payment_method_types=['card']`` (see ``payments.py``), so
    we expect ``payment_status='paid'`` on every credit-grant path. The webhook
    refuses to credit any session whose ``payment_status`` is anything else;
    this is a belt-and-braces defense in case a Checkout session was created
    before the card-only restriction landed, or via the Stripe dashboard.
    Reference: https://docs.stripe.com/payments/checkout/fulfill-orders
    """

    id: str
    amount_total: int | None = None
    payment_status: str | None = None
    payment_intent: str | dict[str, Any] | None = None
    metadata: StripeCheckoutMetadata = Field(default_factory=StripeCheckoutMetadata)

    model_config = ConfigDict(extra="allow")

    @field_validator("payment_intent")
    @classmethod
    def _payment_intent_to_id(cls, value: str | dict[str, Any] | None) -> str | None:
        """Accept expanded Stripe payment intents but keep only the id."""
        if isinstance(value, dict):
            nested = value.get("id")
            return str(nested) if nested is not None else None
        return value


class StripeEventData(BaseModel):
    """Stripe event data wrapper."""

    object: dict[str, Any]


class StripeEventEnvelope(BaseModel):
    """Minimal Stripe event envelope shared by webhook processing tests."""

    id: str
    type: str
    data: StripeEventData

    model_config = ConfigDict(extra="allow")

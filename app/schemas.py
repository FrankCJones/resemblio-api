"""Pydantic request and response schemas for the v1 API."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, TypedDict

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
    refunded: bool | None = None


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
    """Display-safe API key metadata."""

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


class CreditTopupRequest(BaseModel):
    """Request body for starting a Stripe Checkout credit top-up."""

    amount_cents: int


class CreditTopupResponse(BaseModel):
    """Checkout session details returned to the caller."""

    checkout_session_id: str
    checkout_url: str
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

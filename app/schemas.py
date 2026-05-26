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


class ExtractionResponse(BaseModel):
    """Extraction detail returned after creation or cached fetch."""

    id: int
    status: str
    tokens: dict[str, Any] | None
    dtcg: dict[str, Any] | None
    download_url: str | None
    schema_version: int
    error_log: str | None = None


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

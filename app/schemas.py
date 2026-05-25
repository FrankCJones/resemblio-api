"""Pydantic request and response schemas for the v1 API."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, TypedDict

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field


class ErrorBody(TypedDict):
    """JSON error shape returned by auth and route guards."""

    error: str


class ExtractionCreateRequest(BaseModel):
    """Request body for creating a charged extraction."""

    url: AnyHttpUrl


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


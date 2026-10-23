"""Phase 7 Apple DTCG parity, access control, and determinism tests."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.routes import apple_exports
from app.apple_export_product import (
    AppleSelectorParseError,
    _load_projection,
    _public_safe,
    _validate_document,
    build_design_system_sha256,
    build_apple_dtcg_export,
    build_export,
    canonical_artifact_bytes,
    canonical_complete_token_bytes,
    parse_selection,
    validate_export,
)
from app.constants import DEFAULT_API_SCOPE, ONBOARDING_GRANT_CENTS
from app.crypto import generate_api_key, hash_password
from app.models import ApiKey, CreditLedger, User


def _headers(plaintext: str) -> dict[str, str]:
    """Build the normal bearer header for a seeded API key."""
    return {"Authorization": f"Bearer {plaintext}"}


def _seed_user(session: Session, email: str) -> tuple[User, str]:
    """Create a local test account, bearer key, and initial ledger grant."""
    user = User(email=email, password_hash=hash_password("password"), stripe_customer_id="cus_test_export")
    session.add(user)
    session.flush()
    plaintext, digest, prefix = generate_api_key("live")
    session.add(ApiKey(user_id=user.id, key_hash=digest, key_prefix=prefix, label="test", scopes=[DEFAULT_API_SCOPE]))
    session.add(CreditLedger(
        user_id=user.id,
        entry_type="onboarding_grant",
        amount_cents=ONBOARDING_GRANT_CENTS,
        balance_after_cents=ONBOARDING_GRANT_CENTS,
        note="test fixture",
    ))
    session.flush()
    session.commit()
    return user, plaintext


def _request(
    client: TestClient,
    plaintext: str,
    query: str = "",
    extra_headers: dict[str, str] | None = None,
):
    """Call the protected DTCG endpoint with optional query and headers."""
    headers = _headers(plaintext)
    if extra_headers:
        headers.update(extra_headers)
    return client.get(f"/v1/apple/exports/dtcg{query}", headers=headers)


def test_complete_export_separates_pure_tokens_and_integrity_bound_manifest() -> None:
    """The API returns a pure DTCG value and a separately hashed identity manifest."""
    artifact = build_export({"kind": "complete"})
    assert set(artifact) == {"tokens", "manifest"}
    assert artifact["tokens"]["$schema"] == "https://www.designtokens.org/schemas/2025.10/format.json"
    assert all(not key.startswith("$") for key in artifact["tokens"] if key != "$schema")
    product_manifest = artifact["manifest"]
    assert product_manifest["schema_version"] == "resemblio_apple_export_manifest_v2"
    assert product_manifest["tokens"] == {
        "path": "tokens/design.tokens.json",
        "media_type": "application/design-tokens+json",
        "schema_url": artifact["tokens"]["$schema"],
        "sha256": hashlib.sha256(canonical_artifact_bytes(artifact["tokens"])).hexdigest(),
        "count": 72,
        "domains": ["color", "typography", "spacing", "layout", "motion"],
    }
    assert product_manifest["selection"]["selected_breakdown"] == {"foundations": 11, "components": 17, "compositions": 13}
    manifest_body = dict(product_manifest)
    recorded = manifest_body.pop("manifest_sha256")
    assert recorded == hashlib.sha256(canonical_artifact_bytes(manifest_body)).hexdigest()
    assert len(product_manifest["artifact_fingerprint"]) == 64
    validate_export(artifact)


def test_design_system_identity_changes_for_token_projection_only() -> None:
    """A token-only change must invalidate artifact and cache identity."""
    manifest_sha256 = "a" * 64
    first = build_design_system_sha256(manifest_sha256, "b" * 64)
    token_changed = build_design_system_sha256(manifest_sha256, "c" * 64)

    assert len(first) == 64
    assert token_changed != first


def test_builder_is_repeatable_and_accepts_convenience_selector() -> None:
    """Convenience builder repeats canonical bytes for a fixed selection."""
    first = build_apple_dtcg_export("classification", classification="component")
    second = build_apple_dtcg_export("classification", classification="component")
    assert canonical_artifact_bytes(first) == canonical_artifact_bytes(second)
    assert first == second
    assert first["manifest"]["artifact_fingerprint"] == second["manifest"]["artifact_fingerprint"]


@pytest.mark.parametrize("classification", ["foundation", "component", "composition"])
def test_classification_exports_preserve_selection_identity(classification: str) -> None:
    """Each classification manifest contains only its selected record class."""
    artifact = build_export({"kind": "classification", "classification": classification})
    assert all(record["classification"] == classification for record in artifact["manifest"]["records"])


def test_record_export_normalizes_repeated_values() -> None:
    """Record IDs deduplicate and order canonically before identity hashing."""
    params = {"selection": ["record"], "record": ["component:buttons", "component:buttons"]}
    selection = parse_selection(params)
    artifact = build_export(selection)
    assert artifact["manifest"]["selection"]["selected_record_ids"] == ["component:buttons"]


def test_public_builder_normalizes_duplicate_and_reordered_record_ids() -> None:
    """Direct builder calls dedupe and reorder ids using canonical manifest order."""
    artifact = build_apple_dtcg_export(
        "record",
        record_ids=("component:forms", "component:buttons", "component:buttons"),
    )
    assert artifact["manifest"]["selection"]["selected_record_ids"] == ["component:buttons", "component:forms"]


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({}, "invalid_selection"),
        ({"selection": ["complete", "record"]}, "invalid_selection"),
        ({"selection": ["complete"], "classification": ["component"]}, "mixed_selection_contracts"),
        ({"selection": ["classification"]}, "invalid_classification"),
        ({"selection": ["classification"], "classification": ["component", "foundation"]}, "invalid_classification"),
        ({"selection": ["record"]}, "missing_records"),
        ({"selection": ["record"], "record": ["brand:bad"]}, "invalid_record_id"),
        ({"selection": ["record"], "record": ["component:no-such-record"]}, "unknown_record"),
    ],
)
def test_selector_errors_are_stable(params: dict[str, list[str]], code: str) -> None:
    """Selector parser returns the same stable error codes as the web product."""
    with pytest.raises(AppleSelectorParseError) as caught:
        parse_selection(params)
    assert caught.value.code == code


def test_projection_is_pinned_scrubbed_and_attribution_is_preserved() -> None:
    """Projection integrity and canonical public attribution survive export."""
    projection = _load_projection()
    artifact = build_export({"kind": "complete"})
    assert projection["source_projection_sha256"] == "dafcc67c2551b4919ea903b8480e2dc3ac8c5d114db19d6997f00a5e2948666e"
    assert artifact["manifest"]["attribution"] == {"source_name": "Apple", "statement": "Inspired by Apple. No affiliation or endorsement."}
    assert artifact["manifest"]["scrub_policy"]["identity_removed"] is True
    _public_safe(artifact)


@pytest.mark.parametrize(
    "unsafe",
    [
        {"path": "_vendored/source.html"},
        {"font": "San Francisco Pro"},
        {"claim": "Official Apple export"},
    ],
)
def test_public_scrub_rejects_internal_identity_and_affiliation(unsafe: dict[str, str]) -> None:
    """Scrub guards reject source provenance, protected identity, and affiliation."""
    with pytest.raises(ValueError):
        _public_safe(unsafe)


def test_validator_rejects_manifest_hash_tampering() -> None:
    """The export validator checks the independent manifest content hash."""
    artifact = deepcopy(build_export({"kind": "complete"}))
    artifact["manifest"]["attribution"]["statement"] = "A modified attribution statement."
    with pytest.raises(ValueError, match="manifest digest does not match"):
        validate_export(artifact)


def test_validator_rejects_token_hash_tampering_and_combined_envelope() -> None:
    """Tokens remain independently hash-bound and cannot return to an envelope."""
    artifact = deepcopy(build_export({"kind": "complete"}))
    artifact["tokens"]["color"]["bg"]["$value"]["alpha"] = 0.5
    with pytest.raises(ValueError, match="token descriptor"):
        validate_export(artifact)
    with pytest.raises(ValueError, match="separate tokens and manifest"):
        validate_export({"schema_version": 2, "data": artifact})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("tokens", "error"),
    [
        ({"$schema": "https://www.designtokens.org/schemas/2025.10/format.json", "$manifest": {}}, "prohibited root"),
        ({"$schema": "https://www.designtokens.org/schemas/2025.10/format.json", "color": {"a": {"$type": "color", "$value": "{color.missing}"}}}, "missing target"),
        ({"$schema": "https://www.designtokens.org/schemas/2025.10/format.json", "color": {"a": {"$type": "color", "$value": "{color.b}"}, "b": {"$type": "color", "$value": "{color.a}"}}}, "cycle"),
        ({"$schema": "https://www.designtokens.org/schemas/2025.10/format.json", "color": {"a": {"$type": "color", "$value": "{dimension.b}"}}, "dimension": {"b": {"$type": "dimension", "$value": {"value": 1, "unit": "px"}}}}, "changes type"),
    ],
)
def test_token_document_rejects_nonportable_roots_and_invalid_alias_graphs(tokens: dict, error: str) -> None:
    """Token validation rejects proprietary roots and invalid alias references."""
    with pytest.raises(ValueError, match=error):
        _validate_document(tokens)


def test_complete_token_bytes_are_locked_to_the_reference_document() -> None:
    """The sole customer artifact has stable canonical bytes and identity."""
    token_bytes = canonical_complete_token_bytes()

    assert len(token_bytes) == 6196
    assert hashlib.sha256(token_bytes).hexdigest() == "274087d7c084b605f7e40ab6376f2b52b79cbb96b57fd332fc190ad0b6541cd5"
    assert token_bytes.endswith(b"\n")
    assert not token_bytes.endswith(b"\n\n")
    assert json.loads(token_bytes) == build_export({"kind": "complete"})["tokens"]


def test_endpoint_auth_and_entitlement_precede_conditional_delivery(
    client: TestClient, session: Session
) -> None:
    """An If-None-Match wildcard cannot bypass authentication or entitlement."""
    anonymous = client.get(
        "/v1/apple/exports/dtcg?selection=invalid",
        headers={"If-None-Match": "*"},
    )
    assert anonymous.status_code == 401
    _, plaintext = _seed_user(session, email="free-export@example.com")
    denied = _request(
        client,
        plaintext,
        "?selection=invalid",
        {"If-None-Match": "*"},
    )
    assert denied.status_code == 403
    assert denied.json() == {"error": "library_export_not_entitled"}


def test_endpoint_returns_exact_complete_document_and_zero_credit_delivery(
    client: TestClient, session: Session
) -> None:
    """Entitled delivery returns exact canonical bytes without a credit write."""
    user, plaintext = _seed_user(session, email="paid-export@example.com")
    user.subscription_tier = "solo"
    session.flush()
    before = list(session.scalars(select(CreditLedger).where(CreditLedger.user_id == user.id)))
    response = _request(client, plaintext)
    after = list(session.scalars(select(CreditLedger).where(CreditLedger.user_id == user.id)))

    expected = canonical_complete_token_bytes()
    expected_digest = hashlib.sha256(expected).hexdigest()
    assert response.status_code == 200
    assert response.content == expected
    assert response.json() == build_export({"kind": "complete"})["tokens"]
    assert response.headers["content-type"] == "application/design-tokens+json"
    assert response.headers["content-disposition"] == 'attachment; filename="design.tokens.json"'
    assert response.headers["cache-control"] == "private, max-age=0, must-revalidate"
    assert response.headers["etag"] == f'"{expected_digest}"'
    assert before == after


@pytest.mark.parametrize(
    "query",
    [
        "?selection=complete",
        "?selection=classification&classification=component",
        "?selection=record&record=component%3Abuttons",
        "?selection=invalid&classification=other&record=component%3Aunknown",
    ],
)
def test_endpoint_ignores_legacy_selectors_and_returns_only_complete_tokens(
    client: TestClient,
    session: Session,
    query: str,
) -> None:
    """Legacy selectors cannot restore slices, manifests, or response envelopes."""
    user, plaintext = _seed_user(session, email=f"legacy-{len(query)}@example.com")
    user.subscription_tier = "pro"
    session.flush()
    response = _request(client, plaintext, query)

    assert response.status_code == 200
    assert response.content == canonical_complete_token_bytes()
    assert set(response.json()) == {"$schema", "color", "fontFamily", "dimension", "lineHeight", "letterSpacing", "shadow", "cubicBezier", "duration"}


@pytest.mark.parametrize(
    "validator",
    [
        '"274087d7c084b605f7e40ab6376f2b52b79cbb96b57fd332fc190ad0b6541cd5"',
        'W/"274087d7c084b605f7e40ab6376f2b52b79cbb96b57fd332fc190ad0b6541cd5"',
        '"stale", "274087d7c084b605f7e40ab6376f2b52b79cbb96b57fd332fc190ad0b6541cd5"',
        "*",
    ],
)
def test_endpoint_returns_bodyless_304_for_matching_validators_without_credit(
    client: TestClient,
    session: Session,
    validator: str,
) -> None:
    """Strong, weak, listed, and wildcard validators revalidate after entitlement."""
    user, plaintext = _seed_user(session, email=f"etag-{hashlib.sha256(validator.encode()).hexdigest()[:12]}@example.com")
    user.subscription_tier = "studio"
    session.flush()
    before = list(session.scalars(select(CreditLedger).where(CreditLedger.user_id == user.id)))
    response = _request(client, plaintext, extra_headers={"If-None-Match": validator})
    after = list(session.scalars(select(CreditLedger).where(CreditLedger.user_id == user.id)))

    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["etag"] == '"274087d7c084b605f7e40ab6376f2b52b79cbb96b57fd332fc190ad0b6541cd5"'
    assert response.headers["cache-control"] == "private, max-age=0, must-revalidate"
    assert before == after


def test_endpoint_returns_body_for_stale_etag(client: TestClient, session: Session) -> None:
    """A stale validator returns the complete canonical document with status 200."""
    user, plaintext = _seed_user(session, email="etag-stale@example.com")
    user.subscription_tier = "enterprise"
    session.flush()

    response = _request(
        client,
        plaintext,
        extra_headers={"If-None-Match": f'"{"0" * 64}"'},
    )

    assert response.status_code == 200
    assert response.content == canonical_complete_token_bytes()


@pytest.mark.parametrize(
    "construction_error",
    [
        "Apple token projection is unavailable",
        "Apple token projection is invalid",
        "Public artifact scrub rejected protected identity",
    ],
)
def test_endpoint_export_construction_errors_fail_closed(
    client: TestClient,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    construction_error: str,
) -> None:
    """Construction and scrub failures return one opaque retryable contract."""
    user, plaintext = _seed_user(
        session,
        email=f"construction-error-{construction_error.split()[3]}@example.com",
    )
    user.subscription_tier = "solo"
    session.flush()

    def reject_export() -> bytes:
        """Model an internal projection or scrub validation failure."""
        raise ValueError(construction_error)

    monkeypatch.setattr(apple_exports, "canonical_complete_token_bytes", reject_export)
    response = _request(client, plaintext, "?selection=complete")

    assert response.status_code == 503
    assert response.json() == {"error": "library_export_unavailable"}
    assert construction_error not in response.text

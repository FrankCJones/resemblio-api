"""Phase 7 Apple DTCG parity, access control, and determinism tests."""
from __future__ import annotations

from copy import deepcopy
import hashlib
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
    build_apple_dtcg_export,
    build_export,
    canonical_artifact_bytes,
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


def _request(client: TestClient, plaintext: str, query: str = ""):
    """Call the protected Phase 7 endpoint with one query string."""
    return client.get(f"/v1/apple/exports/dtcg{query}", headers=_headers(plaintext))


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
        "count": 74,
        "domains": ["color", "typography", "spacing", "layout", "motion"],
    }
    assert product_manifest["selection"]["selected_breakdown"] == {"foundations": 11, "components": 17, "compositions": 13}
    manifest_body = dict(product_manifest)
    recorded = manifest_body.pop("manifest_sha256")
    assert recorded == hashlib.sha256(canonical_artifact_bytes(manifest_body)).hexdigest()
    assert len(product_manifest["artifact_fingerprint"]) == 64
    validate_export(artifact)


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
    assert projection["source_projection_sha256"] == "7a1e84039ba1ffcb3ec6f22f34907ce5f1c1f9e52fc347beef173836748d2b85"
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


def test_endpoint_auth_and_entitlement_precede_selector_parsing(
    client: TestClient, session: Session
) -> None:
    """Missing auth is rejected first and free accounts see entitlement denial first."""
    anonymous = client.get("/v1/apple/exports/dtcg?selection=invalid")
    assert anonymous.status_code == 401
    _, plaintext = _seed_user(session, email="free-export@example.com")
    denied = _request(client, plaintext, "?selection=invalid")
    assert denied.status_code == 403
    assert denied.json() == {"error": "library_export_not_entitled"}


def test_endpoint_envelope_and_zero_credit_delivery(client: TestClient, session: Session) -> None:
    """Entitled delivery wraps the exact artifact and never appends a credit row."""
    user, plaintext = _seed_user(session, email="paid-export@example.com")
    user.subscription_tier = "solo"
    session.flush()
    before = list(session.scalars(select(CreditLedger).where(CreditLedger.user_id == user.id)))
    response = _request(client, plaintext, "?selection=complete")
    after = list(session.scalars(select(CreditLedger).where(CreditLedger.user_id == user.id)))
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == 2
    expected = build_export({"kind": "complete"})
    assert set(payload["data"]) == {"tokens", "manifest"}
    assert payload["data"]["tokens"] == expected["tokens"]
    assert payload["data"]["manifest"] == expected["manifest"]
    assert before == after


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("", "invalid_selection"),
        ("?selection=complete&classification=component", "mixed_selection_contracts"),
        ("?selection=classification&classification=other", "invalid_classification"),
        ("?selection=record", "missing_records"),
        ("?selection=record&record=component%3Aunknown", "unknown_record"),
    ],
)
def test_endpoint_selector_errors(client: TestClient, session: Session, query: str, code: str) -> None:
    """Paid callers receive stable 400 selector codes inside a compact body."""
    user, plaintext = _seed_user(session, email=f"error-{code}-{len(query)}@example.com")
    user.subscription_tier = "pro"
    session.flush()
    response = _request(client, plaintext, query)
    assert response.status_code == 400
    assert response.json()["error"] == code


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

    def reject_export(_selection: object) -> None:
        """Model an internal projection or scrub validation failure."""
        raise ValueError(construction_error)

    monkeypatch.setattr(apple_exports, "build_export", reject_export)
    response = _request(client, plaintext, "?selection=complete")

    assert response.status_code == 503
    assert response.json() == {"error": "library_export_unavailable"}
    assert construction_error not in response.text

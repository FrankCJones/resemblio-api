"""Phase 7 Apple DTCG parity, access control, and determinism tests."""
from __future__ import annotations

from copy import deepcopy
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.routes import apple_exports
from app.apple_export_product import (
    AppleSelectorParseError,
    _load_projection,
    _public_safe,
    build_apple_dtcg_export,
    build_export,
    canonical_artifact_bytes,
    parse_selection,
    validate_export,
)
from app.constants import DEFAULT_API_SCOPE, ONBOARDING_GRANT_CENTS
from app.crypto import generate_api_key, hash_password
from app.models import ApiKey, CreditLedger, User


PARITY_HASHES = {
    "complete": "8e04c84a60a20b2544df4fc78aab8fda6bb1dd4c67d16e03752a6191546983ea",
    "foundation": "278fff8beb6ff8acd2d8a1190ea770a7c844f2d8f42610af9c14d13082f27757",
    "component": "8d502d43822d90cf81c79acd761d889cb337f891a66063307005a2ae92e68a90",
    "composition": "db5784677380bd78e40d8378e727f48f103d813276b5ecdcdfcd896916409953",
    "record": "842819ab2788274fc24bd3108f66e520ecfdb9b324bd1ef116338057b1144454",
}


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


def test_complete_export_matches_web_artifact_golden() -> None:
    """The complete API artifact matches the web compiler byte-for-byte hash."""
    artifact = build_export({"kind": "complete"})
    assert artifact["artifact_sha256"] == PARITY_HASHES["complete"]
    assert len(canonical_artifact_bytes(artifact)) == 63892
    assert artifact["selection"]["selected_breakdown"] == {"foundations": 11, "components": 17, "compositions": 13}
    assert artifact["token_payload_reference"]["token_count"] == 74


def test_builder_is_repeatable_and_accepts_convenience_selector() -> None:
    """Convenience builder repeats canonical bytes for a fixed selection."""
    first = build_apple_dtcg_export("classification", classification="component")
    second = build_apple_dtcg_export("classification", classification="component")
    assert canonical_artifact_bytes(first) == canonical_artifact_bytes(second)
    assert first["artifact_sha256"] == PARITY_HASHES["component"]


@pytest.mark.parametrize("classification", ["foundation", "component", "composition"])
def test_classification_exports_match_web_artifact_golden(classification: str) -> None:
    """Every classification slice preserves the web compiler's exact hash."""
    artifact = build_export({"kind": "classification", "classification": classification})
    assert artifact["artifact_sha256"] == PARITY_HASHES[classification]
    assert all(record["classification"] == classification for record in artifact["records"])


def test_record_export_matches_web_hash_and_normalizes_repeated_values() -> None:
    """Record ids deduplicate and order canonically before digesting the artifact."""
    params = {"selection": ["record"], "record": ["component:buttons", "component:buttons"]}
    selection = parse_selection(params)
    artifact = build_export(selection)
    assert artifact["artifact_sha256"] == PARITY_HASHES["record"]
    assert artifact["selection"]["selected_record_ids"] == ["component:buttons"]


def test_public_builder_normalizes_duplicate_and_reordered_record_ids() -> None:
    """Direct builder calls dedupe and reorder ids using canonical manifest order."""
    artifact = build_apple_dtcg_export(
        "record",
        record_ids=("component:forms", "component:buttons", "component:buttons"),
    )
    assert artifact["selection"]["selected_record_ids"] == ["component:buttons", "component:forms"]
    assert artifact["artifact_sha256"] == "7347e07bf9ad5a3dd9f9bef8ca6697d581bba624261381c2590a2fc91c3c509b"


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
    assert artifact["attribution"] == {"source_name": "Apple", "statement": "Inspired by Apple. No affiliation or endorsement."}
    assert artifact["scrub_policy"]["identity_removed"] is True
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


def test_validator_rejects_artifact_hash_tampering() -> None:
    """The public artifact validator checks its content-addressed hash."""
    artifact = deepcopy(build_export({"kind": "complete"}))
    artifact["attribution"]["statement"] = "A modified attribution statement."
    with pytest.raises(ValueError, match="digest does not match"):
        validate_export(artifact)


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
    assert payload["data"] == build_export({"kind": "complete"})
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

"""Tests for Stage O1 anonymous-extraction surface.

Covers:
- Anonymous POST returns 202 with extraction_id + claim_token + classification
  + schema_version=1 envelope, with no Authorization header.
- Per-IP daily cap returns 429 with retry_after_s after the cap is hit.
- Unsupported classification returns out_of_scope + refunded=true with no
  underlying extraction row written.
- Claim_token uniqueness across N requests from distinct IPs.
- GET without claim_token returns 403; mismatched token returns 403.
- Notify-when-supported capture writes a row.
- Pure helpers (hash_ip, mint_claim_token, today_bucket, per_ip_daily_cap,
  is_supported, _tokens_preview) behave as documented.
- Feature flag default-off returns 503.

Synthetic-only; no network in tests.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import (
    ANON_CLASS_HTML_FIRST,
    ANON_CLASS_WIX,
    ANON_EXTRACTION_SCHEMA_VERSION,
    ANON_EXTRACT_FLAG_ENV_VAR,
    ANON_EXTRACT_PER_IP_PER_DAY_DEFAULT,
    ANON_EXTRACT_PER_IP_PER_DAY_ENV_VAR,
)
from app.models import AnonExtractCounter, AnonymousExtraction, Extraction, NotifyRequest
from app.routes import extractions_anonymous as anon_route
from app.routes.extractions_anonymous import (
    _tokens_preview,
    hash_ip,
    is_supported,
    mint_claim_token,
    per_ip_daily_cap,
    today_bucket,
)
from app.site_classifier import ClassificationResult


# ---------------------------------------------------------------------------
# Pure-data helper tests
# ---------------------------------------------------------------------------


def test_hash_ip_is_stable_and_hex() -> None:
    """Hashing the same IP twice returns the same 64-char hex string."""
    a = hash_ip("198.51.100.7")
    b = hash_ip("198.51.100.7")
    assert a == b
    assert len(a) == 64
    assert all(c in "0123456789abcdef" for c in a)


def test_hash_ip_changes_with_input() -> None:
    """Distinct IPs hash to distinct values."""
    assert hash_ip("198.51.100.7") != hash_ip("198.51.100.8")


def test_mint_claim_token_is_unique_and_url_safe() -> None:
    """Two minted tokens differ; chars are URL-safe (no padding)."""
    a = mint_claim_token()
    b = mint_claim_token()
    assert a != b
    # secrets.token_urlsafe drops `=` padding and uses `-_` substitutions.
    assert "=" not in a
    assert all(c.isalnum() or c in "-_" for c in a)
    assert len(a) >= 32


def test_today_bucket_is_iso_date() -> None:
    """Bucket is ISO-8601 UTC date string of length 10."""
    bucket = today_bucket(datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc))
    assert bucket == "2026-06-03"
    assert len(bucket) == 10


def test_per_ip_daily_cap_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset env returns the default cap."""
    monkeypatch.delenv(ANON_EXTRACT_PER_IP_PER_DAY_ENV_VAR, raising=False)
    assert per_ip_daily_cap() == ANON_EXTRACT_PER_IP_PER_DAY_DEFAULT


def test_per_ip_daily_cap_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting the env var to a positive int overrides the default."""
    monkeypatch.setenv(ANON_EXTRACT_PER_IP_PER_DAY_ENV_VAR, "5")
    assert per_ip_daily_cap() == 5


def test_per_ip_daily_cap_invalid_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Garbage env value falls back to the default rather than raising."""
    monkeypatch.setenv(ANON_EXTRACT_PER_IP_PER_DAY_ENV_VAR, "not-an-int")
    assert per_ip_daily_cap() == ANON_EXTRACT_PER_IP_PER_DAY_DEFAULT


def test_per_ip_daily_cap_zero_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero is rejected and falls back; zero would block everyone."""
    monkeypatch.setenv(ANON_EXTRACT_PER_IP_PER_DAY_ENV_VAR, "0")
    assert per_ip_daily_cap() == ANON_EXTRACT_PER_IP_PER_DAY_DEFAULT


def test_is_supported_taxonomy() -> None:
    """html_first + js_rendered are supported; everything else is out-of-scope."""
    assert is_supported("html_first") is True
    assert is_supported("js_rendered") is True
    assert is_supported("wix_class") is False
    assert is_supported("waf_blocked") is False
    assert is_supported("unknown") is False


def test_tokens_preview_slices_categories() -> None:
    """Preview slices colors, fonts, and spacing into the documented caps."""
    tokens = {
        "bg": "#fff",
        "text": "#111",
        "accent": "#f33",
        "surface": "#f5f5f5",
        "font_body": "Inter",
        "font_display": "Playfair",
        "space_1": "4px",
        "space_2": "8px",
        "radius_sm": "4px",
        "unrelated": "ignore",
    }
    preview = _tokens_preview(tokens)
    # colors slot captured bg/text/accent/surface
    assert "bg" in preview["colors"]
    assert "accent" in preview["colors"]
    # fonts captured both font_* keys
    assert preview["fonts"]["font_body"] == "Inter"
    assert preview["fonts"]["font_display"] == "Playfair"
    # spacing captured space_* and radius_* keys
    assert "space_1" in preview["spacing"]
    assert "radius_sm" in preview["spacing"]
    # unrelated keys do not bleed in
    assert "unrelated" not in preview["colors"]
    assert "unrelated" not in preview["spacing"]


# ---------------------------------------------------------------------------
# Route tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _enable_feature_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the anonymous-extract feature flag on for every route test.

    The flag defaults off in production; tests need it on to exercise
    the success path. One feature-flag-off test below explicitly
    clears the env var before its assertion.
    """
    monkeypatch.setenv(ANON_EXTRACT_FLAG_ENV_VAR, "true")


@pytest.fixture(autouse=True)
def _stub_classify_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default classifier stub: every URL classifies as html_first.

    Stage O3 (`app.site_classifier.classify_url`) is real and makes a
    live HTTP fetch. Route tests in this module exercise the
    rate-limit, claim-token, and registry contracts, not the classifier
    itself; we therefore stub the classifier to the stable Stage-O1
    behaviour. Tests that need an unsupported-class path
    (`test_anonymous_extract_blocks_unsupported_class`,
    `test_anonymous_extract_classifies_js_rendered_as_supported`)
    monkeypatch over this default; pytest's per-test monkeypatch
    ordering ensures the test-specific override wins.
    """
    monkeypatch.setattr(
        anon_route,
        "classify_url",
        lambda url, **_kwargs: ClassificationResult(
            label=ANON_CLASS_HTML_FIRST, confidence=1.0
        ),
    )


def _post_anon(client: TestClient, url: str, ip: str = "203.0.113.5") -> "tuple[int, dict]":
    """Helper: POST /v1/anonymous/extractions; returns (status, body)."""
    response = client.post(
        "/v1/anonymous/extractions",
        headers={"X-Forwarded-For": ip},
        json={"url": url},
    )
    return response.status_code, response.json()


def test_anonymous_extract_returns_extraction_id_without_auth(
    client: TestClient, session: Session
) -> None:
    """POST with no Authorization header returns 202 + extraction_id + claim_token."""
    status, body = _post_anon(client, "https://example.com")
    assert status == 202
    assert body["schema_version"] == ANON_EXTRACTION_SCHEMA_VERSION
    assert body["status"] == "pending"
    assert body["classification"] == ANON_CLASS_HTML_FIRST
    assert isinstance(body["extraction_id"], int)
    assert isinstance(body["claim_token"], str)
    assert len(body["claim_token"]) >= 32
    # Underlying extractions row exists, tagged to the service user.
    row = session.get(Extraction, body["extraction_id"])
    assert row is not None
    assert row.status == "pending"
    assert row.api_key_id is None
    # Anonymous extraction does NOT write a credit ledger row.
    # (Stage O5 reconciles billing at signup time per the respec.)


def test_anonymous_extract_enforces_per_ip_daily_cap(
    client: TestClient, session: Session
) -> None:
    """Second POST from the same IP within 24h returns 429 with retry_after_s."""
    status1, body1 = _post_anon(client, "https://example.com", ip="198.51.100.10")
    assert status1 == 202
    assert body1["extraction_id"] is not None

    response = client.post(
        "/v1/anonymous/extractions",
        headers={"X-Forwarded-For": "198.51.100.10"},
        json={"url": "https://example.org"},
    )
    assert response.status_code == 429
    body2 = response.json()
    assert body2["error"] == "rate_limited"
    assert body2["schema_version"] == ANON_EXTRACTION_SCHEMA_VERSION
    assert isinstance(body2["retry_after_s"], int)
    assert body2["retry_after_s"] >= 1
    assert response.headers.get("retry-after") == str(body2["retry_after_s"])


def test_anonymous_extract_blocks_unsupported_class(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wix-class URL returns out_of_scope + refunded; no extraction row written."""
    monkeypatch.setattr(
        anon_route,
        "classify_url",
        lambda url: ClassificationResult(label=ANON_CLASS_WIX, confidence=0.9),
    )
    status, body = _post_anon(client, "https://example-wix-site.com", ip="198.51.100.20")
    assert status == 200
    assert body["status"] == "out_of_scope"
    assert body["classification"] == ANON_CLASS_WIX
    assert body["refunded"] is True
    assert body["extraction_id"] is None
    assert body["claim_token"] is None
    assert body["notify_email_capture_url"] == "/api/notify-when-supported"
    # No extractions row was created for the unsupported class.
    rows = session.query(Extraction).all()
    assert rows == []


def test_anonymous_extract_classifies_js_rendered_as_supported(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JS-rendered class is supported just like html_first."""
    from app.constants import ANON_CLASS_JS_RENDERED

    monkeypatch.setattr(
        anon_route,
        "classify_url",
        lambda url: ClassificationResult(label=ANON_CLASS_JS_RENDERED, confidence=0.55),
    )
    status, body = _post_anon(client, "https://js-app.example", ip="198.51.100.30")
    assert status == 202
    assert body["classification"] == ANON_CLASS_JS_RENDERED
    assert body["extraction_id"] is not None


def test_claim_token_required_to_bind_to_new_account(
    client: TestClient, session: Session
) -> None:
    """GET without claim_token returns 403; with bad token returns 403."""
    status, body = _post_anon(client, "https://example.com", ip="198.51.100.40")
    assert status == 202
    extraction_id = body["extraction_id"]

    no_token = client.get(f"/v1/anonymous/extractions/{extraction_id}")
    assert no_token.status_code == 403
    assert no_token.json()["error"] == "claim_token_required"

    bad_token = client.get(
        f"/v1/anonymous/extractions/{extraction_id}",
        params={"claim_token": "definitely-not-the-real-token"},
    )
    assert bad_token.status_code == 403
    assert bad_token.json()["error"] == "invalid_claim_token"


def test_get_with_valid_claim_token_returns_row(
    client: TestClient, session: Session
) -> None:
    """Valid claim_token surfaces the classification + status payload."""
    status, body = _post_anon(client, "https://example.com", ip="198.51.100.50")
    assert status == 202
    response = client.get(
        f"/v1/anonymous/extractions/{body['extraction_id']}",
        params={"claim_token": body["claim_token"]},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == ANON_EXTRACTION_SCHEMA_VERSION
    assert payload["classification"] == ANON_CLASS_HTML_FIRST
    assert payload["extraction_id"] == body["extraction_id"]


def test_claim_token_unique_across_requests(
    client: TestClient, session: Session
) -> None:
    """Distinct IPs yield distinct claim_tokens; tokens are not predictable."""
    tokens = set()
    for i in range(5):
        status, body = _post_anon(client, f"https://site-{i}.example", ip=f"203.0.113.{100 + i}")
        assert status == 202
        tokens.add(body["claim_token"])
    assert len(tokens) == 5


def test_schema_version_on_every_response(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every response path (202 / 429 / 200-out-of-scope / 403) carries schema_version."""
    # 202 success
    status, body = _post_anon(client, "https://example.com", ip="198.51.100.60")
    assert body["schema_version"] == ANON_EXTRACTION_SCHEMA_VERSION
    # 429 rate-limited
    response = client.post(
        "/v1/anonymous/extractions",
        headers={"X-Forwarded-For": "198.51.100.60"},
        json={"url": "https://example.org"},
    )
    assert response.json()["schema_version"] == ANON_EXTRACTION_SCHEMA_VERSION
    # 200 out-of-scope
    monkeypatch.setattr(
        anon_route,
        "classify_url",
        lambda url: ClassificationResult(label=ANON_CLASS_WIX, confidence=0.9),
    )
    _, oos_body = _post_anon(client, "https://wix.example", ip="198.51.100.61")
    assert oos_body["schema_version"] == ANON_EXTRACTION_SCHEMA_VERSION
    # 403 claim_token_required
    response = client.get(f"/v1/anonymous/extractions/{body['extraction_id']}")
    assert response.json()["schema_version"] == ANON_EXTRACTION_SCHEMA_VERSION


def test_feature_flag_default_off_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the feature flag is not set, the endpoint returns 503."""
    monkeypatch.delenv(ANON_EXTRACT_FLAG_ENV_VAR, raising=False)
    response = client.post(
        "/v1/anonymous/extractions",
        headers={"X-Forwarded-For": "198.51.100.70"},
        json={"url": "https://example.com"},
    )
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "feature_disabled"
    assert body["schema_version"] == ANON_EXTRACTION_SCHEMA_VERSION


def test_per_ip_counter_row_is_written(
    client: TestClient, session: Session
) -> None:
    """The counter row lands with count=1 after one successful POST."""
    _post_anon(client, "https://example.com", ip="198.51.100.80")
    rows = session.query(AnonExtractCounter).all()
    assert len(rows) == 1
    assert rows[0].count == 1
    assert rows[0].day == today_bucket()


def test_registry_row_is_written_with_expires_at(
    client: TestClient, session: Session
) -> None:
    """Successful POST writes an anonymous_extractions row with expires_at set."""
    _, body = _post_anon(client, "https://example.com", ip="198.51.100.90")
    rows = session.query(AnonymousExtraction).all()
    assert len(rows) == 1
    registry = rows[0]
    assert registry.claim_token == body["claim_token"]
    assert registry.classification == ANON_CLASS_HTML_FIRST
    assert registry.status == "pending"
    assert registry.expires_at is not None


def test_notify_when_supported_writes_row(
    client: TestClient, session: Session
) -> None:
    """POST /v1/notify-when-supported appends a NotifyRequest row."""
    response = client.post(
        "/v1/notify-when-supported",
        json={
            "url": "https://wix-site.example",
            "email": "tester@example.com",
            "detected_class": "wix_class",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["schema_version"] == ANON_EXTRACTION_SCHEMA_VERSION
    rows = session.query(NotifyRequest).all()
    assert len(rows) == 1
    assert rows[0].email == "tester@example.com"
    assert rows[0].detected_class == "wix_class"


def test_notify_when_supported_rejects_invalid_email_short(
    client: TestClient,
) -> None:
    """Very short email is rejected by Pydantic min_length floor."""
    response = client.post(
        "/v1/notify-when-supported",
        json={"url": "https://x.example", "email": "x", "detected_class": "wix_class"},
    )
    assert response.status_code in (400, 422)

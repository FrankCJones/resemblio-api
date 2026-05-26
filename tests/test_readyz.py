"""Tests for the auth-free ``/v1/readyz`` readiness endpoint.

Closes audit finding M-API-3 (`projects/OptSus Team/security-audits/
2026-05-26-initial.md`). Verifies that:

- ``/v1/readyz`` is auth-free (no bearer token required).
- A healthy database probe + healthy storage probe returns HTTP 200 with a
  per-component breakdown.
- A failing database probe returns HTTP 503 with status='fail' and the
  component-level detail flips to 'fail'.
- A failing storage probe returns HTTP 503 in the same way.

The probes use monkeypatch to inject failures rather than real outages so the
test suite stays offline and deterministic.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.routes import health


def test_readyz_returns_200_when_all_components_healthy(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Healthy DB + healthy storage produce a 200 with status=ok."""
    # Stub the storage probe (no real R2 credentials in tests). The database
    # probe runs against the real in-memory SQLite fixture, which is healthy.
    monkeypatch.setattr(health, "_check_storage", lambda: {"status": "ok", "detail": None})
    response = client.get("/v1/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"]["status"] == "ok"
    assert body["storage"]["status"] == "ok"
    assert body["schema_version"] == 1


def test_readyz_returns_503_when_database_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing database probe flips the aggregate status to fail with 503."""
    monkeypatch.setattr(health, "_check_database", lambda: {"status": "fail", "detail": "OperationalError"})
    monkeypatch.setattr(health, "_check_storage", lambda: {"status": "ok", "detail": None})
    response = client.get("/v1/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "fail"
    assert body["database"]["status"] == "fail"
    assert body["database"]["detail"] == "OperationalError"


def test_readyz_returns_503_when_storage_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing storage probe flips the aggregate status to fail with 503."""
    monkeypatch.setattr(health, "_check_database", lambda: {"status": "ok", "detail": None})
    monkeypatch.setattr(health, "_check_storage", lambda: {"status": "fail", "detail": "EndpointConnectionError"})
    response = client.get("/v1/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "fail"
    assert body["storage"]["status"] == "fail"


def test_readyz_is_auth_free(client: TestClient) -> None:
    """Readiness probe must not require a bearer token (Uptime Kuma callers)."""
    # Without monkeypatching, _check_storage will fail (no R2 creds) and
    # _check_database will succeed. Either path returns a JSON body, not a
    # 401 missing_credentials error from AuthMiddleware - which is what we
    # are actually asserting here.
    response = client.get("/v1/readyz")
    assert response.status_code in (200, 503)
    assert "error" not in response.json() or response.json().get("error") != "missing_credentials"

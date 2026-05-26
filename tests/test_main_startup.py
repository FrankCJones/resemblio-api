"""Tests for the FastAPI startup guards in ``app.main``.

Covers the worker-concurrency guard added by security audit H4
(``projects/OptSus Team/security-audits/2026-05-26-initial.md``). The in-memory
rate limiter cannot be safely shared across uvicorn workers, so the app must
refuse to start when either ``WEB_CONCURRENCY`` or ``UVICORN_WORKERS`` is set
above 1.
"""
from __future__ import annotations

import pytest

from app.main import validate_worker_concurrency


def test_validate_worker_concurrency_accepts_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset env vars pass the guard (default uvicorn single-worker run)."""
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.delenv("UVICORN_WORKERS", raising=False)
    validate_worker_concurrency()


def test_validate_worker_concurrency_accepts_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicitly setting workers=1 is the canonical safe configuration."""
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    monkeypatch.setenv("UVICORN_WORKERS", "1")
    validate_worker_concurrency()


def test_validate_worker_concurrency_rejects_web_concurrency_gt_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """WEB_CONCURRENCY > 1 fails fast to prevent silent rate-limit ceiling multiplication."""
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    monkeypatch.delenv("UVICORN_WORKERS", raising=False)
    with pytest.raises(RuntimeError, match="WEB_CONCURRENCY=4"):
        validate_worker_concurrency()


def test_validate_worker_concurrency_rejects_uvicorn_workers_gt_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """UVICORN_WORKERS > 1 fails fast for the same reason as WEB_CONCURRENCY."""
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.setenv("UVICORN_WORKERS", "2")
    with pytest.raises(RuntimeError, match="UVICORN_WORKERS=2"):
        validate_worker_concurrency()


def test_validate_worker_concurrency_rejects_non_integer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed worker count is rejected before the int-comparison branch."""
    monkeypatch.setenv("WEB_CONCURRENCY", "many")
    monkeypatch.delenv("UVICORN_WORKERS", raising=False)
    with pytest.raises(RuntimeError, match="must be an integer"):
        validate_worker_concurrency()


def test_validate_worker_concurrency_ignores_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty-string env var is treated as unset rather than a parse error."""
    monkeypatch.setenv("WEB_CONCURRENCY", "")
    monkeypatch.setenv("UVICORN_WORKERS", "")
    validate_worker_concurrency()

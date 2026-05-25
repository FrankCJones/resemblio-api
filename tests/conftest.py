"""Shared pytest fixtures for the API test suite."""
from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

os.environ.setdefault("RESEMBLIO_KEY_PEPPER", "test-pepper-value-with-thirty-two-chars")
os.environ.setdefault("RESEMBLIO_DB_URL", "sqlite+pysqlite:///:memory:")

from app import db  # noqa: E402
from app import extractor_bridge as _extractor_bridge  # noqa: E402
from app.config import reset_settings_cache  # noqa: E402
from app.constants import DEFAULT_API_SCOPE, ONBOARDING_GRANT_CENTS  # noqa: E402
from app.crypto import generate_api_key, hash_password  # noqa: E402
from app.db import Base  # noqa: E402
from app.extractor_bridge import ExtractionBundle, bundle_from_token_set  # noqa: E402
from app.main import app  # noqa: E402
from app.models import ApiKey, CreditLedger, User  # noqa: E402
from app.rate_limit import reset_rate_limiter  # noqa: E402
from app.routes.extractions import get_extractor  # noqa: E402
from app.storage import get_storage  # noqa: E402


TOKEN_SET: dict[str, str] = {
    "bg": "#ffffff",
    "surface": "#f5f5f5",
    "text": "#111111",
    "text_muted": "#555555",
    "accent": "#ff3366",
    "font_body": "Inter, sans-serif",
    "font_display": "Inter, sans-serif",
    "space_1": "4px",
    "space_2": "8px",
    "radius_sm": "4px",
    "radius_md": "8px",
    "shadow_sm": "0 1px 2px rgb(0 0 0 / 0.1)",
    "duration_fast": "120ms",
    "ease_standard": "cubic-bezier(0.2, 0, 0, 1)",
}


class FakeStorage:
    """In-memory storage adapter matching the route dependency surface."""

    def __init__(self) -> None:
        """Create an empty object store."""
        self.objects: dict[str, bytes] = {}

    def put_extraction_zip(self, extraction_id: int, user_id: int, zip_bytes: bytes) -> tuple[str, str]:
        """Store ZIP bytes and return the expected key plus SHA."""
        key = f"extractions/{user_id}/{extraction_id}.zip"
        self.objects[key] = zip_bytes
        return key, hashlib.sha256(zip_bytes).hexdigest()

    def get_extraction_zip(self, object_key: str) -> bytes:
        """Return stored ZIP bytes."""
        return self.objects[object_key]

    def sign_download_url(self, object_key: str, expires_in: int = 900) -> str:
        """Return a deterministic fake signed URL."""
        return f"https://r2.test/{object_key}?expires={expires_in}"


class _FakeBridgeExtractor:
    """Minimal extractor stand-in used when the real DRL extractor is unavailable."""

    def extract(self, url: str) -> tuple[dict[str, str], None]:
        """Return the shared synthetic TokenSet without touching the network."""
        return TOKEN_SET, None


def _fake_load_extractor() -> tuple[type, int, type, Any]:
    """Lazy-import stand-in returning (ExtractorCls, SCHEMA_VERSION, TokenSet, to_dtcg_json)."""
    return (
        _FakeBridgeExtractor,
        1,
        dict,
        lambda ts: {"color": {}, "dimension": {}, "fontFamily": {}},
    )


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Reset SQLite schema, rate limiter, and settings around every test.

    Pins ``RESEMBLIO_KEY_PEPPER`` and ``RESEMBLIO_DB_URL`` via ``monkeypatch.setenv``
    so the values are guaranteed restored after each test, even if an earlier test
    or a credentials-file load mutated ``os.environ`` directly. Without this pin,
    ``test_crypto.test_generate_api_key_hash_and_prefix`` becomes order-dependent
    because it hard-codes the expected pepper.
    """
    monkeypatch.setenv("RESEMBLIO_KEY_PEPPER", "test-pepper-value-with-thirty-two-chars")
    monkeypatch.setenv("RESEMBLIO_DB_URL", "sqlite+pysqlite:///:memory:")
    reset_settings_cache()
    db.reset_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=db.engine)
    reset_rate_limiter()
    monkeypatch.setattr(_extractor_bridge, "_load_extractor", _fake_load_extractor)
    yield
    Base.metadata.drop_all(bind=db.engine)
    app.dependency_overrides.clear()
    reset_settings_cache()


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """Return a direct SQLAlchemy session for assertions."""
    with db.SessionLocal() as current:
        yield current


@pytest.fixture
def fake_storage() -> FakeStorage:
    """Return fake object storage for route tests."""
    return FakeStorage()


@pytest.fixture
def fake_extractor() -> Callable[[str], ExtractionBundle]:
    """Return a fake extractor bridge that packages a synthetic TokenSet."""
    def _extract(url: str) -> ExtractionBundle:
        return bundle_from_token_set(url, TOKEN_SET)

    return _extract


@pytest.fixture
def client(fake_storage: FakeStorage, fake_extractor: Callable[[str], ExtractionBundle]) -> Generator[TestClient, None, None]:
    """Return a TestClient with storage and extractor dependencies replaced."""
    app.dependency_overrides[get_storage] = lambda: fake_storage
    app.dependency_overrides[get_extractor] = lambda: fake_extractor
    with TestClient(app) as current:
        yield current


def seed_user(session: Session, email: str = "frank@optsus.com", balance: int = ONBOARDING_GRANT_CENTS) -> tuple[User, ApiKey, str]:
    """Create a user, starter key, and optional onboarding grant."""
    user = User(email=email.lower(), password_hash=hash_password("password"), status="active")
    session.add(user)
    session.flush()
    plaintext, digest, prefix = generate_api_key("live")
    api_key = ApiKey(user_id=user.id, key_hash=digest, key_prefix=prefix, label="test", scopes=[DEFAULT_API_SCOPE])
    session.add(api_key)
    session.flush()
    if balance:
        session.add(
            CreditLedger(
                user_id=user.id,
                entry_type="onboarding_grant",
                amount_cents=balance,
                balance_after_cents=balance,
                note="test seed",
            )
        )
    session.commit()
    return user, api_key, plaintext


def auth_headers(plaintext: str) -> dict[str, str]:
    """Build bearer auth headers for tests."""
    return {"Authorization": f"Bearer {plaintext}"}


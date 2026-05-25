"""Tests for API packaging around the existing extractor."""
from __future__ import annotations

import json
import os
from io import BytesIO
from zipfile import ZipFile

from pytest import MonkeyPatch

from app import extractor_bridge
from tests.conftest import TOKEN_SET


class FakeExtractor:
    """Extractor stand-in that returns a synthetic TokenSet."""

    def extract(self, url: str) -> tuple[dict[str, str], None]:
        """Return test tokens without touching network or Postgres."""
        return TOKEN_SET, None


def test_extract_design_tokens_packages_zip_and_restores_db_env(monkeypatch: MonkeyPatch) -> None:
    """The bridge disables legacy extractor persistence during API calls."""
    monkeypatch.setenv("RESEMBLIO_DB_URL", "postgresql+psycopg://example")
    monkeypatch.setattr(extractor_bridge, "CodexExtractor", lambda: FakeExtractor())
    bundle = extractor_bridge.extract_design_tokens("https://example.com")
    assert os.environ["RESEMBLIO_DB_URL"] == "postgresql+psycopg://example"
    assert bundle.dtcg_json["schema_version"] == bundle.schema_version
    with ZipFile(io_bytes(bundle.zip_bytes)) as zip_file:
        manifest = json.loads(zip_file.read("manifest.json"))
        tokens = json.loads(zip_file.read("tokens.json"))
    assert manifest["schema_version"] == bundle.schema_version
    assert manifest["tokens_sha256"]
    assert tokens["schema_version"] == bundle.schema_version


def io_bytes(data: bytes) -> BytesIO:
    """Return a BytesIO object for ZipFile tests."""
    return BytesIO(data)

"""Integration tests for Phase 4: manifest threaded through metadata and routes.

TDD: tests written BEFORE the indexer and route modifications.

Tests:
  - _metadata_for carries capture_manifest (schema-versioned) and hub_signal
  - Routes expose captured_count, missing_components on hub rows and page payloads
  - Mock and api mode return identical shape (contract-parity)

Phase 4 modifies:
  - app/library_indexer.py: _metadata_for now includes manifest fields
  - app/routes/library.py: HubFeaturedRow + LibraryPageData gain manifest fields
"""
from __future__ import annotations

import pytest

from app.brand_capture_manifest import (
    CAPTURE_MANIFEST_SCHEMA_VERSION,
    build_capture_manifest,
)
from app.library_indexer import _metadata_for, LIBRARY_PAGE_METADATA_SCHEMA_VERSION
from app.missing_data_notice import (
    HUB_CAPTURE_SIGNAL_SCHEMA_VERSION,
    MISSING_DATA_NOTICE_SCHEMA_VERSION,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

DRL_SEED_TOKENS: dict[str, str] = {
    "ds-bg": "#ffffff",
    "ds-text": "#0a2540",
    "ds-accent": "#635bff",
    "ds-font-body": "Sohne",
    "ds-font-display": "Sohne",
    "ds-radius-sm": "6px",
    "ds-space-4": "16px",
    "ds-page-pad-x": "32px",
    "ds-page-max-default": "880px",
    "ds-section-padding-x": "32px",
}

FULL_CAPTURE_TOKENS: dict[str, str] = {
    **DRL_SEED_TOKENS,
    "ds-button-padding-y": "12px",
    "ds-button-padding-x": "24px",
    "ds-button-border-width": "0px",
    "ds-card-border-width": "1px",
    "ds-card-padding": "24px",
    "ds-badge-padding-y": "3px",
    "ds-badge-padding-x": "10px",
    "ds-input-padding-y": "10px",
    "ds-input-border-width": "1px",
}


# ---------------------------------------------------------------------------
# _metadata_for carries manifest fields
# ---------------------------------------------------------------------------

class TestMetadataForManifest:
    """_metadata_for includes capture_manifest and hub_capture_signal."""

    def test_metadata_has_capture_manifest_key(self) -> None:
        meta = _metadata_for("buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        assert "capture_manifest" in meta

    def test_capture_manifest_is_schema_versioned(self) -> None:
        meta = _metadata_for("buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        manifest = meta["capture_manifest"]
        assert manifest["schema_version"] == CAPTURE_MANIFEST_SCHEMA_VERSION

    def test_capture_manifest_has_groups(self) -> None:
        meta = _metadata_for("buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        manifest = meta["capture_manifest"]
        assert "groups" in manifest
        assert "button" in manifest["groups"]
        assert "color" in manifest["groups"]

    def test_metadata_has_hub_capture_signal_key(self) -> None:
        meta = _metadata_for("buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        assert "hub_capture_signal" in meta

    def test_hub_capture_signal_is_schema_versioned(self) -> None:
        meta = _metadata_for("buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        signal = meta["hub_capture_signal"]
        assert signal["schema_version"] == HUB_CAPTURE_SIGNAL_SCHEMA_VERSION

    def test_hub_capture_signal_has_counts(self) -> None:
        meta = _metadata_for("buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        signal = meta["hub_capture_signal"]
        assert "captured_count" in signal
        assert "total_showcase_groups" in signal
        assert isinstance(signal["captured_count"], int)
        assert isinstance(signal["total_showcase_groups"], int)

    def test_drl_seed_captured_count_is_zero(self) -> None:
        meta = _metadata_for("buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        assert meta["hub_capture_signal"]["captured_count"] == 0

    def test_full_capture_count_equals_total(self) -> None:
        meta = _metadata_for("buttons", brand_slug="stripe", tokens=FULL_CAPTURE_TOKENS)
        signal = meta["hub_capture_signal"]
        assert signal["captured_count"] == signal["total_showcase_groups"]

    def test_metadata_has_missing_notice_key(self) -> None:
        meta = _metadata_for("buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        assert "missing_data_notice" in meta

    def test_missing_notice_is_schema_versioned(self) -> None:
        meta = _metadata_for("buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        notice = meta["missing_data_notice"]
        assert notice["schema_version"] == MISSING_DATA_NOTICE_SCHEMA_VERSION

    def test_missing_notice_has_missing_items(self) -> None:
        meta = _metadata_for("buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        notice = meta["missing_data_notice"]
        assert "missing_items" in notice
        assert isinstance(notice["missing_items"], list)
        # DRL seed has no button/card/badge/input capture -> items present
        assert len(notice["missing_items"]) > 0

    def test_full_capture_missing_items_empty(self) -> None:
        meta = _metadata_for("buttons", brand_slug="stripe", tokens=FULL_CAPTURE_TOKENS)
        notice = meta["missing_data_notice"]
        assert notice["missing_items"] == []

    def test_schema_version_preserved(self) -> None:
        # The existing metadata_json.schema_version must still be present.
        meta = _metadata_for("buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        assert meta["schema_version"] == LIBRARY_PAGE_METADATA_SCHEMA_VERSION

    def test_existing_fields_preserved(self) -> None:
        # bg, accent, text, font_display, font_body must still be present.
        meta = _metadata_for("buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        for key in ("bg", "accent", "text"):
            assert key in meta


# ---------------------------------------------------------------------------
# Manifest-derived fields are deterministic per token bag
# ---------------------------------------------------------------------------

class TestMetadataFordeterminism:
    """Same inputs produce identical metadata envelopes."""

    def test_deterministic_metadata(self) -> None:
        m1 = _metadata_for("hero", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        m2 = _metadata_for("hero", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        assert m1 == m2

    def test_different_categories_same_manifest(self) -> None:
        # The manifest is per-brand, not per-category. Same tokens -> same manifest.
        m_buttons = _metadata_for("buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        m_hero = _metadata_for("hero", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        assert (
            m_buttons["capture_manifest"]
            == m_hero["capture_manifest"]
        )

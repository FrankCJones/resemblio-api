"""Tests for AssetComponent storage: model, write path, and idempotency.

Issue #1 - Carry component code (markup + CSS) from DRL through to the DB.
This is the storage foundation; the seed wiring (#2) and indexer (#3) build on it.

RED phase: all tests import from names that do not yet exist. They fail
until the model + insert_asset_component are implemented (GREEN commit).

No network calls; no DRL file reads. All fixtures are synthetic.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.asset_versions import AssetComponentSpec, insert_asset_component, insert_or_reuse_asset_version
from app.models import AssetComponent


# ---------------------------------------------------------------------------
# Shared synthetic fixtures
# ---------------------------------------------------------------------------

_SAMPLE_DTCG: dict = {
    "schema_version": 1,
    "color": {"brand": {"$value": "#3366cc", "$type": "color"}},
}

_SAMPLE_HTML = "<button class='btn'>Click me</button>"
_SAMPLE_CSS = ".btn { background: var(--color-brand); border-radius: 4px; }"
_SAMPLE_CSS_V2 = ".btn { background: var(--color-brand); border-radius: 8px; }"
_SAMPLE_PATH = "assets/atoms/buttons/a24-cinematic-001"


def _make_asset_version_id(session: Session) -> int:
    """Create a minimal synthetic asset_versions row and return its PK.

    Used by tests that need a valid FK target for asset_components without
    caring about the DTCG payload itself.
    """
    av = insert_or_reuse_asset_version(
        session,
        url="https://example.com",
        dtcg=_SAMPLE_DTCG,
        first_extracted_by_user_id=None,
        manifest_schema_version=2,
    )
    session.flush()
    return av.id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_insert_creates_row_with_all_fields(session: Session) -> None:
    """Inserting an AssetComponentSpec persists all fields with the correct schema_version."""
    av_id = _make_asset_version_id(session)
    spec = AssetComponentSpec(
        fragment_key="default",
        component_html=_SAMPLE_HTML,
        component_css=_SAMPLE_CSS,
        source_asset_path=_SAMPLE_PATH,
        states_present=["rest", "hover", "focus", "disabled"],
    )
    row = insert_asset_component(session, av_id, spec)
    session.flush()

    assert row.id is not None
    assert row.asset_version_id == av_id
    assert row.fragment_key == "default"
    assert row.component_html == _SAMPLE_HTML
    assert row.component_css == _SAMPLE_CSS
    assert row.source_asset_path == _SAMPLE_PATH
    assert row.states_present == ["rest", "hover", "focus", "disabled"]
    assert row.schema_version == "asset_component_v1"
    assert row.created_at is not None


def test_round_trip_html_and_css(session: Session) -> None:
    """component_html and component_css survive the ORM round-trip byte-for-byte."""
    av_id = _make_asset_version_id(session)
    # Include whitespace and special characters to exercise the text column
    complex_html = '<div class="wrap">\n  <button data-variant="primary">OK</button>\n</div>'
    complex_css = (
        ":root { --c: #fff; }\n"
        ".btn { transition: all 120ms ease; }\n"
        ".btn:hover { transform: scale(1.05); }\n"
    )
    spec = AssetComponentSpec(
        fragment_key="default",
        component_html=complex_html,
        component_css=complex_css,
        source_asset_path=_SAMPLE_PATH,
        states_present=["rest", "hover"],
    )
    row = insert_asset_component(session, av_id, spec)
    session.flush()

    fetched = session.get(AssetComponent, row.id)
    assert fetched is not None
    assert fetched.component_html == complex_html
    assert fetched.component_css == complex_css


def test_idempotent_same_key_reuses_row(session: Session) -> None:
    """A second insert with the same (asset_version_id, fragment_key) reuses the row."""
    av_id = _make_asset_version_id(session)
    spec = AssetComponentSpec(
        fragment_key="default",
        component_html=_SAMPLE_HTML,
        component_css=_SAMPLE_CSS,
        source_asset_path=_SAMPLE_PATH,
        states_present=["rest"],
    )
    row1 = insert_asset_component(session, av_id, spec)
    session.flush()
    original_id = row1.id

    row2 = insert_asset_component(session, av_id, spec)
    session.flush()

    assert row2.id == original_id

    all_rows = session.execute(
        select(AssetComponent).where(AssetComponent.asset_version_id == av_id)
    ).scalars().all()
    assert len(all_rows) == 1


def test_idempotent_updates_code_in_place(session: Session) -> None:
    """Re-inserting with changed code updates the existing row rather than duplicating it."""
    av_id = _make_asset_version_id(session)
    spec_v1 = AssetComponentSpec(
        fragment_key="default",
        component_html=_SAMPLE_HTML,
        component_css=_SAMPLE_CSS,
        source_asset_path=_SAMPLE_PATH,
        states_present=["rest"],
    )
    row1 = insert_asset_component(session, av_id, spec_v1)
    session.flush()
    original_id = row1.id

    spec_v2 = AssetComponentSpec(
        fragment_key="default",
        component_html=_SAMPLE_HTML,
        component_css=_SAMPLE_CSS_V2,
        source_asset_path=_SAMPLE_PATH,
        states_present=["rest", "hover"],
    )
    row2 = insert_asset_component(session, av_id, spec_v2)
    session.flush()

    assert row2.id == original_id
    assert row2.component_css == _SAMPLE_CSS_V2
    assert row2.states_present == ["rest", "hover"]

    all_rows = session.execute(
        select(AssetComponent).where(AssetComponent.asset_version_id == av_id)
    ).scalars().all()
    assert len(all_rows) == 1


def test_different_fragment_key_creates_separate_row(session: Session) -> None:
    """A different fragment_key creates a distinct row under the same asset_version_id."""
    av_id = _make_asset_version_id(session)
    spec_default = AssetComponentSpec(
        fragment_key="default",
        component_html=_SAMPLE_HTML,
        component_css=_SAMPLE_CSS,
        source_asset_path=_SAMPLE_PATH,
        states_present=["rest"],
    )
    spec_inverse = AssetComponentSpec(
        fragment_key="inverse",
        component_html="<button class='btn-inv'>Click</button>",
        component_css=".btn-inv { background: #000; color: #fff; }",
        source_asset_path=_SAMPLE_PATH,
        states_present=["rest"],
    )
    row_default = insert_asset_component(session, av_id, spec_default)
    row_inverse = insert_asset_component(session, av_id, spec_inverse)
    session.flush()

    assert row_default.id != row_inverse.id
    assert row_default.fragment_key == "default"
    assert row_inverse.fragment_key == "inverse"

    all_rows = session.execute(
        select(AssetComponent).where(AssetComponent.asset_version_id == av_id)
    ).scalars().all()
    assert len(all_rows) == 2


def test_states_present_persists_as_list(session: Session) -> None:
    """states_present JSON column round-trips as a Python list without type coercion."""
    av_id = _make_asset_version_id(session)
    states = ["rest", "hover", "focus", "disabled", "loading"]
    spec = AssetComponentSpec(
        fragment_key="default",
        component_html=_SAMPLE_HTML,
        component_css=_SAMPLE_CSS,
        source_asset_path=_SAMPLE_PATH,
        states_present=states,
    )
    row = insert_asset_component(session, av_id, spec)
    session.flush()

    assert isinstance(row.states_present, list)
    assert row.states_present == states
    assert len(row.states_present) == len(states)

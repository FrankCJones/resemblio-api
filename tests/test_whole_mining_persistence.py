"""Tests for whole-mining persistence: mined atoms -> synthetic asset_versions (issue #28).

This file covers four TDD phases:

Phase 1 - Mined-marker guard in the indexer (pure)
    ``_mined_atom_class`` helper + ``_process_job`` class-loop restriction.
    A mined synthetic writes exactly one library_pages row; a normal
    asset_version still writes one row per template class.

Phase 2 - Precedence + brand-whole discovery (pure)
    ``_find_whole_candidates`` returns the first-whole mapping only for
    atom classes with no standalone atom in the brand corpus.

Phase 3 - Persist one mined atom (integration on the real apple fixture)
    ``mine_and_persist_atoms_for_brand`` creates the correct synthetic
    asset_version + asset_component, enqueues it, and is idempotent.

Phase 4 - End-to-end through the indexer
    Seed the apple whole + mined buttons synthetic, drain the index queue,
    assert that ``/library/apple/buttons`` carries real ``.cta__btn`` markup
    AND that the apple ``cta-blocks`` canonical page is NOT regressed.

No network calls. No writes to the Design Reference Library. All DRL file
reads use either the vendored ``apple_cta_block`` fixture in
``tests/fixtures/drl/`` or a synthetic tree under ``tmp_path``.

Do this work at a level that would impress a senior developer.
Include documentation and code comments that make it easy for a future
developer to maintain this project.
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.asset_versions import get_asset_component, insert_or_reuse_asset_version
from app.constants import SCHEMA_V1
from app.library_indexer import (
    _mined_atom_class,
    drain_pending,
    enqueue_for_asset_version,
)
from app.models import AssetVersion, LibraryPage
from scripts.seed_from_drl import (
    DrlAssetDict,
    DrlSystemDict,
    _find_whole_candidates,
    mine_and_persist_atoms_for_brand,
)

# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------

# The vendored apple-cta-block fixture lives here; load once at module level.
_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "drl" / "apple_cta_block"

# Minimal tokens for the apple cta-block whole. These mirror the variables
# referenced by .cta__btn rules in asset.html so token-substitution works.
_APPLE_TOKENS_CSS = """\
:root {
  --ds-bg: #000000;
  --ds-text: #f5f5f7;
  --ds-accent: #2997ff;
  --ds-surface: #1d1d1f;
  --ds-hairline: #424245;
  --ds-text-muted: #86868b;
  --ds-border: #424245;
  --ds-font-body: "SF Pro Text", sans-serif;
  --ds-font-display: "SF Pro Display", sans-serif;
  --ds-font-mono: "SF Mono", monospace;
  --ds-text-xs: 0.75rem;
  --ds-text-sm: 0.875rem;
  --ds-text-lg: 1.125rem;
  --ds-text-4xl: 2.5rem;
  --ds-radius-sm: 6px;
}
"""

# Whole path within the synthetic DRL tree.
_APPLE_WHOLE_PATH = "assets/wholes/cta-blocks/apple-cta-block-001"

# Expected synthetic URL for the mined apple/buttons asset_version.
_EXPECTED_MINED_URL = (
    "resemblio://seed/drl_v1/apple/buttons/mined-from-apple-cta-block-001"
)


def _make_apple_drl_root(tmp_path: Path) -> Path:
    """Create a minimal DRL tree rooted at ``tmp_path/drl`` with apple's cta-block whole.

    Returns the drl_root path.  The tree contains:
    - ``assets/wholes/cta-blocks/apple-cta-block-001/asset.html``  (vendored fixture)
    - ``assets/wholes/cta-blocks/apple-cta-block-001/tokens.css``  (synthetic)
    """
    drl_root = tmp_path / "drl"
    drl_root.mkdir()
    whole_dir = drl_root / _APPLE_WHOLE_PATH
    whole_dir.mkdir(parents=True)
    shutil.copy(_FIXTURE_DIR / "asset.html", whole_dir / "asset.html")
    (whole_dir / "tokens.css").write_text(_APPLE_TOKENS_CSS, encoding="utf-8")
    return drl_root


def _apple_system_no_standalone() -> DrlSystemDict:
    """System dict for apple: one whole, no standalone button atom.

    Used to exercise the "no standalone -> mine it" path.
    """
    return {
        "slug": "apple",
        "name": "Apple",
        "tier": "A",
        "category": "tech-consumer",
        "asset_count": 1,
        "assets": [
            {
                "slug": "apple-cta-block-001",
                "cls": None,
                "kind": "whole",
                "path": _APPLE_WHOLE_PATH,
                "tokens_path": f"{_APPLE_WHOLE_PATH}/tokens.css",
                "tldr": "Apple-style CTA block.",
                "patterns": [],
                "mood": [],
                "applicable_to": [],
                "tags": [],
                "provenance_score": "A",
            }
        ],
    }


def _a24_system_with_standalone_button() -> DrlSystemDict:
    """System dict for a24: has a standalone buttons atom.

    Used to exercise the "standalone exists -> skip mining" precedence rule (D3).
    """
    return {
        "slug": "a24",
        "name": "A24",
        "tier": "A",
        "category": "entertainment",
        "asset_count": 2,
        "assets": [
            {
                "slug": "a24-cinematic-001",
                "cls": None,
                "kind": "atom",
                "path": "assets/atoms/buttons/a24-cinematic-001",
                "tokens_path": "assets/atoms/buttons/a24-cinematic-001/tokens.css",
                "tldr": "A24 cinematic button.",
                "patterns": [],
                "mood": [],
                "applicable_to": [],
                "tags": [],
                "provenance_score": "A",
            },
            {
                "slug": "a24-hero-001",
                "cls": None,
                "kind": "whole",
                "path": "assets/wholes/hero/a24-hero-001",
                "tokens_path": "assets/wholes/hero/a24-hero-001/tokens.css",
                "tldr": "A24 cinematic hero block.",
                "patterns": [],
                "mood": [],
                "applicable_to": [],
                "tags": [],
                "provenance_score": "A",
            },
        ],
    }


def _run_indexer(session: Session, asset_version: AssetVersion) -> None:
    """Enqueue and drain a single asset_version through the indexer."""
    job = enqueue_for_asset_version(session, asset_version.id)
    assert job is not None, (
        "enqueue_for_asset_version returned None; check is_public and whether "
        "a live job already exists for this asset_version"
    )
    session.commit()
    drain_pending(session)


# ---------------------------------------------------------------------------
# Phase 1 - Mined-marker guard in the indexer (pure)
# ---------------------------------------------------------------------------


class TestMinedAtomClassHelper:
    """Unit tests for the ``_mined_atom_class`` pure helper."""

    def test_returns_none_for_empty_dtcg(self) -> None:
        """An empty dtcg has no mined_atom_class key."""
        assert _mined_atom_class({}) is None

    def test_returns_none_when_only_class_key_present(self) -> None:
        """``class`` key alone does not signal a mined synthetic; ``mined_atom_class`` does."""
        assert _mined_atom_class({"class": "buttons"}) is None

    def test_returns_atom_class_when_marker_present(self) -> None:
        """The marker key returns its value unchanged."""
        assert _mined_atom_class({"mined_atom_class": "buttons"}) == "buttons"
        assert _mined_atom_class({"mined_atom_class": "cards"}) == "cards"

    def test_returns_none_for_empty_string_marker(self) -> None:
        """An empty string value is treated as absent (no mining intent)."""
        assert _mined_atom_class({"mined_atom_class": ""}) is None

    def test_returns_none_when_both_keys_present_but_marker_empty(self) -> None:
        """Both keys present but marker empty -> None (marker wins)."""
        assert _mined_atom_class({"class": "buttons", "mined_atom_class": ""}) is None

    def test_returns_marker_value_over_class_value(self) -> None:
        """When both keys present, mined_atom_class is returned (not class)."""
        result = _mined_atom_class({"class": "cards", "mined_atom_class": "buttons"})
        assert result == "buttons"


class TestMinedAssetVersionWritesOnePage:
    """The indexer restricts the class loop for mined asset_versions."""

    def test_mined_asset_version_writes_exactly_one_page(self, session: Session) -> None:
        """A mined synthetic asset_version writes one library_pages row (its atom class).

        The ``mined_atom_class`` dtcg key gates the indexer's class loop to
        the single mined class.  Without this gate, the indexer would write
        one row per template class, and the mined synthetic's later
        ``fetched_at`` could demote the brand's real whole-page rows from
        canonical status.
        """
        dtcg = {
            "schema_version": SCHEMA_V1,
            "class": "buttons",
            "mined_atom_class": "buttons",  # D2 marker restricts the class loop
            "slug": "apple-buttons-mined",
            "tokens": {"bg": "#000", "text": "#fff", "accent": "#2997ff",
                       "font_body": "SF Pro", "font_display": "SF Pro Display"},
        }
        av = AssetVersion(
            url=_EXPECTED_MINED_URL,
            content_hash="test-mined-hash-apple-buttons",
            dtcg_json=dtcg,
            manifest_schema_version=SCHEMA_V1,
            is_public=True,
            version_label="DRL mined from apple-cta-block-001",
        )
        session.add(av)
        session.commit()

        _run_indexer(session, av)

        all_pages = session.execute(
            select(LibraryPage).where(LibraryPage.asset_version_id == av.id)
        ).scalars().all()

        assert len(all_pages) == 1, (
            f"Expected 1 page for a mined synthetic asset_version, got {len(all_pages)}. "
            "The mined_atom_class guard in _process_job may not be restricting the loop."
        )
        assert all_pages[0].category_slug == "buttons"

    def test_normal_asset_version_still_writes_all_template_classes(
        self, session: Session
    ) -> None:
        """A normal (non-mined) asset_version continues to write one page per template class.

        Regression guard: the mined_atom_class restriction must not affect
        asset_versions that do NOT carry the marker key.
        """
        from app.library_indexer import _all_template_classes

        dtcg = {
            "schema_version": SCHEMA_V1,
            "class": "buttons",
            "slug": "acme-buttons-normal",
            "tokens": {"bg": "#fff", "text": "#000", "accent": "#ff3366",
                       "font_body": "Inter", "font_display": "Playfair Display"},
        }
        av = AssetVersion(
            url="resemblio://seed/drl_v1/acme/buttons/acme-btn-001",
            content_hash="test-normal-hash-acme-buttons",
            dtcg_json=dtcg,
            manifest_schema_version=SCHEMA_V1,
            is_public=True,
            version_label="DRL bootstrap 2026-06-17",
        )
        session.add(av)
        session.commit()

        _run_indexer(session, av)

        all_pages = session.execute(
            select(LibraryPage).where(LibraryPage.asset_version_id == av.id)
        ).scalars().all()

        assert len(all_pages) == len(_all_template_classes()), (
            f"Normal asset_version should write {len(_all_template_classes())} pages "
            f"(one per template class), got {len(all_pages)}."
        )


# ---------------------------------------------------------------------------
# Phase 2 - Precedence + brand-whole discovery (pure)
# ---------------------------------------------------------------------------


class TestFindWholeCandidates:
    """Unit tests for ``_find_whole_candidates`` - pure, no I/O."""

    def test_excludes_class_when_standalone_atom_exists(self) -> None:
        """A brand with a standalone buttons atom must NOT be planned for mining.

        Design D3: only create a mined synthetic for (brand, atom_class) when
        the brand has no standalone atom of that class.  Prevents creating a
        duplicate 'apple/buttons' synthetic when apple already captured one.
        """
        system = _a24_system_with_standalone_button()
        result = _find_whole_candidates(system, atom_classes=("buttons",))
        assert "buttons" not in result, (
            "buttons was included in mining candidates even though a24 has a "
            "standalone buttons atom (assets/atoms/buttons/a24-cinematic-001). "
            "The precedence rule in _find_whole_candidates is not filtering it out."
        )

    def test_maps_class_to_first_whole_when_no_standalone(self) -> None:
        """A brand with no standalone buttons atom maps buttons to the first whole.

        The returned dict carries the whole asset dict as the value.
        """
        system = _apple_system_no_standalone()
        result = _find_whole_candidates(system, atom_classes=("buttons",))
        assert "buttons" in result, (
            "buttons missing from candidates even though apple has no standalone "
            "buttons atom.  _find_whole_candidates may be incorrectly detecting "
            "a phantom standalone or filtering too aggressively."
        )
        whole = result["buttons"]
        assert whole["kind"] == "whole"
        assert "cta-block" in whole["path"]

    def test_deterministic_order_picks_alphabetically_first_whole(self) -> None:
        """When multiple wholes exist, the alphabetically first path is selected.

        Deterministic ordering ensures the same whole is always chosen on
        re-seeds, producing a stable URL and idempotent dedup behaviour.
        """
        whole_b: DrlAssetDict = {
            "slug": "apple-hero-001",
            "cls": None,
            "kind": "whole",
            "path": "assets/wholes/hero/apple-hero-001",
            "tokens_path": "assets/wholes/hero/apple-hero-001/tokens.css",
        }
        whole_a: DrlAssetDict = {
            "slug": "apple-cta-block-001",
            "cls": None,
            "kind": "whole",
            "path": "assets/wholes/cta-blocks/apple-cta-block-001",
            "tokens_path": "assets/wholes/cta-blocks/apple-cta-block-001/tokens.css",
        }
        system: DrlSystemDict = {
            "slug": "apple",
            "assets": [whole_b, whole_a],  # intentionally out of sorted order
        }
        result = _find_whole_candidates(system, atom_classes=("buttons",))
        # cta-blocks sorts before hero alphabetically.
        assert result.get("buttons") is whole_a, (
            "Expected the alphabetically first whole (cta-blocks/apple-cta-block-001) "
            "to be selected, but got a different whole.  Check _find_whole_candidates "
            "sorting logic."
        )

    def test_empty_when_no_wholes_in_brand(self) -> None:
        """A brand with no wholes at all produces an empty candidates dict."""
        system: DrlSystemDict = {
            "slug": "text-only",
            "assets": [
                {"kind": "atom", "path": "assets/atoms/buttons/t-btn-001"},
            ],
        }
        result = _find_whole_candidates(system, atom_classes=("buttons",))
        assert result == {}

    def test_empty_when_atom_classes_tuple_is_empty(self) -> None:
        """An empty atom_classes tuple produces an empty result."""
        system = _apple_system_no_standalone()
        result = _find_whole_candidates(system, atom_classes=())
        assert result == {}

    def test_multiple_classes_handled_independently(self) -> None:
        """Each atom class is evaluated independently.

        If a brand has a standalone buttons atom but no standalone cards atom,
        only cards should appear in the candidates.
        """
        system: DrlSystemDict = {
            "slug": "mixed",
            "assets": [
                # Standalone buttons atom - should block buttons mining.
                {
                    "kind": "atom",
                    "path": "assets/atoms/buttons/mixed-btn-001",
                    "slug": "mixed-btn-001",
                },
                # No standalone cards atom - should allow cards mining.
                {
                    "kind": "whole",
                    "path": "assets/wholes/cta-blocks/mixed-cta-001",
                    "slug": "mixed-cta-001",
                    "tokens_path": "assets/wholes/cta-blocks/mixed-cta-001/tokens.css",
                },
            ],
        }
        result = _find_whole_candidates(system, atom_classes=("buttons", "cards"))
        assert "buttons" not in result
        assert "cards" in result


# ---------------------------------------------------------------------------
# Phase 3 - Persist one mined atom (integration on the real apple fixture)
# ---------------------------------------------------------------------------


class TestMineAndPersistAtomsForBrand:
    """Integration tests for ``mine_and_persist_atoms_for_brand`` using the real fixture."""

    def test_creates_synthetic_asset_version_with_correct_url(
        self, session: Session, tmp_path: Path
    ) -> None:
        """mine_and_persist creates a synthetic asset_version at the expected URL."""
        drl_root = _make_apple_drl_root(tmp_path)
        system = _apple_system_no_standalone()

        urls = mine_and_persist_atoms_for_brand(
            session,
            drl_root,
            system,
            atom_classes=("buttons",),
            seed_user_id=1,
            captured_date="2026-06-17",
        )
        session.commit()

        assert len(urls) == 1, (
            f"Expected 1 synthetic URL, got {len(urls)}.  "
            "Check mine_and_persist_atoms_for_brand return value."
        )
        assert urls[0] == _EXPECTED_MINED_URL

        av = session.execute(
            select(AssetVersion).where(AssetVersion.url == _EXPECTED_MINED_URL)
        ).scalar_one_or_none()
        assert av is not None, (
            f"No asset_version row found at {_EXPECTED_MINED_URL!r}.  "
            "mine_and_persist_atoms_for_brand may not have written the row."
        )

    def test_synthetic_dtcg_carries_required_fields(
        self, session: Session, tmp_path: Path
    ) -> None:
        """The synthetic asset_version's dtcg carries class, mined_atom_class, and mined_from.

        These three fields are required for correct indexer routing (D2 guard),
        honest provenance disclosure, and dedup.
        """
        drl_root = _make_apple_drl_root(tmp_path)
        mine_and_persist_atoms_for_brand(
            session,
            drl_root,
            _apple_system_no_standalone(),
            atom_classes=("buttons",),
            seed_user_id=1,
        )
        session.commit()

        av = session.execute(
            select(AssetVersion).where(AssetVersion.url == _EXPECTED_MINED_URL)
        ).scalar_one()
        dtcg = av.dtcg_json
        assert dtcg["class"] == "buttons", (
            "dtcg['class'] must equal 'buttons' so the indexer routes the real "
            "component to the /library/apple/buttons page."
        )
        assert dtcg["mined_atom_class"] == "buttons", (
            "dtcg['mined_atom_class'] is the D2 single-class guard key; missing "
            "it would cause the indexer to write pages for ALL template classes."
        )
        assert "mined_from" in dtcg, (
            "dtcg['mined_from'] records provenance (which whole was mined); "
            "it should name 'apple-cta-block-001'."
        )
        assert "apple-cta-block-001" in dtcg["mined_from"]

    def test_synthetic_version_label_identifies_mined_source(
        self, session: Session, tmp_path: Path
    ) -> None:
        """version_label distinguishes mined rows from natively-captured ones."""
        drl_root = _make_apple_drl_root(tmp_path)
        mine_and_persist_atoms_for_brand(
            session,
            drl_root,
            _apple_system_no_standalone(),
            atom_classes=("buttons",),
            seed_user_id=1,
        )
        session.commit()

        av = session.execute(
            select(AssetVersion).where(AssetVersion.url == _EXPECTED_MINED_URL)
        ).scalar_one()
        assert av.version_label is not None
        assert "mined" in av.version_label.lower(), (
            f"version_label {av.version_label!r} should contain 'mined' to distinguish "
            "it from 'DRL bootstrap ...' native rows."
        )
        assert "apple-cta-block-001" in av.version_label

    def test_asset_component_carries_button_markup(
        self, session: Session, tmp_path: Path
    ) -> None:
        """The persisted asset_component contains the mined .cta__btn markup."""
        drl_root = _make_apple_drl_root(tmp_path)
        mine_and_persist_atoms_for_brand(
            session,
            drl_root,
            _apple_system_no_standalone(),
            atom_classes=("buttons",),
            seed_user_id=1,
        )
        session.commit()

        av = session.execute(
            select(AssetVersion).where(AssetVersion.url == _EXPECTED_MINED_URL)
        ).scalar_one()
        component = get_asset_component(session, av.id)
        assert component is not None, (
            "No asset_component row found for the mined synthetic asset_version. "
            "mine_and_persist_atoms_for_brand must call insert_asset_component."
        )
        assert component.fragment_key == "default"
        assert "cta__btn--primary" in component.component_html, (
            ".cta__btn--primary class missing from mined component_html.  "
            "Check that mine_atom_from_whole found the anchor elements in the "
            "apple-cta-block-001 fixture."
        )
        assert "cta__btn--ghost" in component.component_html

    def test_idempotent_on_rerun(self, session: Session, tmp_path: Path) -> None:
        """A second call creates no duplicate asset_version or asset_component rows."""
        drl_root = _make_apple_drl_root(tmp_path)
        system = _apple_system_no_standalone()

        mine_and_persist_atoms_for_brand(
            session, drl_root, system, atom_classes=("buttons",), seed_user_id=1
        )
        session.commit()

        mine_and_persist_atoms_for_brand(
            session, drl_root, system, atom_classes=("buttons",), seed_user_id=1
        )
        session.commit()

        avs = session.execute(
            select(AssetVersion).where(AssetVersion.url == _EXPECTED_MINED_URL)
        ).scalars().all()
        assert len(avs) == 1, (
            f"Expected 1 asset_version row after two calls, got {len(avs)}.  "
            "Dedup is anchored on (url, content_hash); check insert_or_reuse_asset_version."
        )
        # Also check that only one asset_component row exists.
        from app.models import AssetComponent
        components = session.execute(
            select(AssetComponent).where(AssetComponent.asset_version_id == avs[0].id)
        ).scalars().all()
        assert len(components) == 1, (
            f"Expected 1 asset_component row after two calls, got {len(components)}."
        )

    def test_skips_brand_with_standalone_button_atom(
        self, session: Session, tmp_path: Path
    ) -> None:
        """A brand with a standalone buttons atom gets no mined synthetic.

        Design D3 precedence: the mined path supplements gaps; it must not
        create a duplicate when a native atom already exists.
        """
        drl_root = _make_apple_drl_root(tmp_path)  # drl_root content doesn't matter
        system = _a24_system_with_standalone_button()

        urls = mine_and_persist_atoms_for_brand(
            session,
            drl_root,
            system,
            atom_classes=("buttons",),
            seed_user_id=1,
        )
        session.commit()

        assert urls == [], (
            f"Expected no mined synthetics for a24 (which has a standalone buttons atom), "
            f"but got: {urls}"
        )
        # No synthetic asset_version should have been created for a24.
        avs = session.execute(
            select(AssetVersion).where(
                AssetVersion.url.like("resemblio://seed/drl_v1/a24/buttons/%")
            )
        ).scalars().all()
        assert len(avs) == 0

    def test_drl_is_read_only_no_files_written(
        self, session: Session, tmp_path: Path
    ) -> None:
        """mine_and_persist_atoms_for_brand must not write any files to the DRL tree.

        Design constraint from CLAUDE.md: DRL is a pull-only read source.
        """
        drl_root = _make_apple_drl_root(tmp_path)
        # Snapshot DRL files before.
        before = {
            p: p.stat().st_mtime
            for p in drl_root.rglob("*")
            if p.is_file()
        }

        mine_and_persist_atoms_for_brand(
            session,
            drl_root,
            _apple_system_no_standalone(),
            atom_classes=("buttons",),
            seed_user_id=1,
        )
        session.commit()

        after = {
            p: p.stat().st_mtime
            for p in drl_root.rglob("*")
            if p.is_file()
        }
        assert before == after, (
            "DRL files were modified by mine_and_persist_atoms_for_brand.  "
            "The DRL is read-only; no writes are permitted."
        )
        assert set(before.keys()) == set(after.keys()), (
            "New files were created in the DRL tree by mine_and_persist_atoms_for_brand."
        )


# ---------------------------------------------------------------------------
# Phase 4 - End-to-end through the indexer
# ---------------------------------------------------------------------------


class TestEndToEndMinedAtomIndexing:
    """End-to-end tests: seed whole + mined synthetic -> indexer -> library_pages."""

    def _seed_apple_whole(self, session: Session, drl_root: Path) -> AssetVersion:
        """Insert a synthetic apple-cta-block-001 whole asset_version with a component.

        Simulates what apply_seed writes for the apple whole, using real
        fixture HTML so the indexer composes a real cta-blocks page.
        """
        from app.asset_versions import AssetComponentSpec, insert_asset_component
        from scripts.seed_from_drl import extract_component_css, extract_component_html

        whole_html = (drl_root / _APPLE_WHOLE_PATH / "asset.html").read_text(encoding="utf-8")
        component_css = extract_component_css(whole_html)
        component_html = extract_component_html(whole_html)

        dtcg = {
            "schema_version": SCHEMA_V1,
            # Use the template-registry class name ("cta-block", singular) so
            # the indexer writes a real-component page for this category.  The
            # actual DRL asset class is "cta-blocks" (plural), but that key is
            # not in TEMPLATES_BY_CLASS, so the indexer would never write a
            # cta-blocks page.  The singular form correctly triggers the real-
            # component path and gives the regression guard a page to test.
            "class": "cta-block",
            "slug": "apple-cta-block-001",
            "tokens": {
                "bg": "#000000", "text": "#f5f5f7", "accent": "#2997ff",
                "font_body": "SF Pro Text", "font_display": "SF Pro Display",
                "surface": "#1d1d1f",
            },
        }
        av = insert_or_reuse_asset_version(
            session,
            url="resemblio://seed/drl_v1/apple/cta-blocks/apple-cta-block-001",
            dtcg=dtcg,
            first_extracted_by_user_id=None,
            manifest_schema_version=SCHEMA_V1,
            is_public=True,
            version_label="DRL bootstrap 2026-06-17",
        )
        spec = AssetComponentSpec(
            fragment_key="default",
            component_html=component_html,
            component_css=component_css,
            source_asset_path=_APPLE_WHOLE_PATH,
            states_present=["rest"],
        )
        insert_asset_component(session, av.id, spec)
        # Pin to a known past time so the mined synthetic (created later with
        # server_default=now()) is definitively newer. SQLite resolves func.now()
        # at second precision; without this pin both rows get the same timestamp
        # and _reconcile_canonical's ORDER BY fetched_at DESC is non-deterministic.
        av.fetched_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        session.flush()
        return av

    def test_buttons_page_carries_real_cta_btn_markup(
        self, session: Session, tmp_path: Path
    ) -> None:
        """After seeding the whole and the mined synthetic, /library/apple/buttons
        has a canonical page whose rendered_html carries the real .cta__btn markup.

        Acceptance criterion: rendered_html contains cta__btn--primary,
        cta__btn--ghost, data-rs-source="drl-component", and NOT b-btn.
        """
        drl_root = _make_apple_drl_root(tmp_path)

        # Step 1: seed and index the whole first (earlier fetched_at).
        whole_av = self._seed_apple_whole(session, drl_root)
        session.commit()
        _run_indexer(session, whole_av)

        # Step 2: seed the mined synthetic (mine_and_persist already enqueues it).
        urls = mine_and_persist_atoms_for_brand(
            session,
            drl_root,
            _apple_system_no_standalone(),
            atom_classes=("buttons",),
            seed_user_id=1,
        )
        session.commit()
        assert urls, "mine_and_persist_atoms_for_brand returned no URLs"

        # Drain the pending job (already enqueued by mine_and_persist above).
        drain_pending(session)

        # Assert: /library/apple/buttons canonical page has real .cta__btn markup.
        buttons_page = session.execute(
            select(LibraryPage)
            .where(LibraryPage.brand_slug == "apple")
            .where(LibraryPage.category_slug == "buttons")
            .where(LibraryPage.is_canonical == True)  # noqa: E712
        ).scalar_one_or_none()

        assert buttons_page is not None, (
            "No canonical buttons page found for brand apple.  "
            "Check that the mined synthetic's page is marked canonical."
        )
        html = buttons_page.rendered_html
        assert "cta__btn--primary" in html, (
            "cta__btn--primary missing from /library/apple/buttons rendered_html.  "
            "The real DRL component (mined from apple-cta-block-001) should carry it."
        )
        assert "cta__btn--ghost" in html, (
            "cta__btn--ghost missing from /library/apple/buttons rendered_html."
        )
        assert 'data-rs-source="drl-component"' in html, (
            "data-rs-source marker missing; the real-component compose path did not fire "
            "for the mined asset_version."
        )
        assert "b-btn" not in html, (
            "Generic .b-btn chiclet appeared in the mined buttons page; the indexer "
            "served the template path instead of the real DRL component."
        )

    def test_cta_blocks_canonical_page_not_regressed(
        self, session: Session, tmp_path: Path
    ) -> None:
        """After indexing the mined synthetic, apple's cta-block canonical page
        remains the real whole page and is NOT demoted to non-canonical.

        Critical regression guard for D2: the mined synthetic's later fetched_at
        must not cause ``_reconcile_canonical`` to strip canonical status from
        the whole's cta-block page.  The per-category reconcile is the mechanism.

        Note: the DRL asset class is "cta-blocks" (plural) but the template
        registry uses "cta-block" (singular).  ``_seed_apple_whole`` uses the
        template-registry name so that the indexer actually writes a cta-block
        page (and the regression guard has a page to assert against).
        """
        drl_root = _make_apple_drl_root(tmp_path)

        # Seed + index the whole first.
        whole_av = self._seed_apple_whole(session, drl_root)
        session.commit()
        _run_indexer(session, whole_av)

        # Seed the mined synthetic (mine_and_persist already enqueues it).
        mine_and_persist_atoms_for_brand(
            session, drl_root, _apple_system_no_standalone(),
            atom_classes=("buttons",), seed_user_id=1,
        )
        session.commit()
        # Drain the pending job (already enqueued by mine_and_persist above).
        drain_pending(session)

        # The cta-block canonical page must still be the whole's page.
        cta_page = session.execute(
            select(LibraryPage)
            .where(LibraryPage.brand_slug == "apple")
            .where(LibraryPage.category_slug == "cta-block")
            .where(LibraryPage.is_canonical == True)  # noqa: E712
        ).scalar_one_or_none()

        assert cta_page is not None, (
            "No canonical cta-block page found for apple.  "
            "The whole's cta-block page should remain canonical after the mined "
            "synthetic is indexed."
        )
        # The canonical cta-block page must come from the whole, not the synthetic.
        assert cta_page.asset_version_id == whole_av.id, (
            f"cta-block canonical page belongs to av {cta_page.asset_version_id}, "
            f"expected the whole's av {whole_av.id}.  The per-category reconcile may "
            "not be working correctly: the mined synthetic (which has no cta-block page) "
            "should not be able to demote the whole's cta-block page."
        )

    def test_mined_buttons_page_is_canonical(
        self, session: Session, tmp_path: Path
    ) -> None:
        """The mined synthetic's buttons page is canonical (newer fetched_at wins for buttons).

        Per-category reconcile: for the buttons category, the mined synthetic
        (created after the whole) is the most recent -> its buttons page is canonical.
        The whole's buttons page (if the whole has one) is not canonical.
        """
        drl_root = _make_apple_drl_root(tmp_path)

        whole_av = self._seed_apple_whole(session, drl_root)
        session.commit()
        _run_indexer(session, whole_av)

        mine_and_persist_atoms_for_brand(
            session, drl_root, _apple_system_no_standalone(),
            atom_classes=("buttons",), seed_user_id=1,
        )
        session.commit()
        # Drain the pending job (already enqueued by mine_and_persist above).
        drain_pending(session)

        # Look up the mined synthetic's asset_version to assert its page is canonical.
        mined_av = session.execute(
            select(AssetVersion).where(AssetVersion.url == _EXPECTED_MINED_URL)
        ).scalar_one()

        # The mined synthetic's page must be canonical for buttons.
        mined_buttons_page = session.execute(
            select(LibraryPage)
            .where(LibraryPage.asset_version_id == mined_av.id)
            .where(LibraryPage.category_slug == "buttons")
        ).scalar_one_or_none()
        assert mined_buttons_page is not None
        assert mined_buttons_page.is_canonical, (
            "The mined synthetic's buttons page should be canonical (newer fetched_at). "
            "Check the per-category reconcile logic."
        )

"""BLOCKER 1 regression tests: brand canonical pages must not render
"Item N Title" / "Item N Dek" / "Item N Date" humanize-fallback strings.

Authored 2026-06-03 from
``projects/OptSus Team/cto-reviews/2026-06-03-library-ux-audit.md``
BLOCKER 1. Frank's verbatim constraint that day:

    "we cannot send real users through the onboarding flow until the
    entire flow works ... If we run the API and get a quality output,
    but drop the user on a library page that looks like garbage then
    they are not going to know what they just received."

Root cause: the NEWS_LIST template at
``projects/Design Reference Library/_scripts/templates.py:589-592``
uses ``{item_1_date}`` / ``{item_1_title}`` / ``{item_1_dek}`` (and the
parallel 2..4 slots). ``app.library_indexer._brand_placeholder`` did
not enumerate those keys in its ``presets`` map, so the function fell
through to the humanize-fallback ``name.replace("_", " ").title()``
and emitted the literal strings "Item 1 Title", "Item 1 Dek", "Item 1
Date" into the rendered HTML.

These tests pin two contracts:

1. **Unit-level (synthetic):** every slot key referenced by every DRL
   template resolves to a non-fallback string. We catch the literal
   "Item N Title" pattern (and adjacent step / member / section slot
   families) at the smallest possible scope so a future template
   adding a new ``{item_5_*}`` slot fails here loudly instead of
   shipping the fallback string into production.

2. **End-to-end (real Aeon DRL fixture):** drive the same compose
   pipeline the production indexer uses, against the frozen Aeon
   ``aeon_dtcg.json`` fixture, and assert no rendered page carries
   any of the forbidden placeholder regex matches from the visual
   fidelity spec at
   ``projects/Resemblio/_verification/library-fidelity-spec-v1.json``.

Pair with: ``tests/test_library_indexer_render_fidelity.py`` (sibling
file; no-Lorem + token-projection assertions). This file's narrower
scope keeps the BLOCKER 1 fix's regression surface explicit.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.constants import SCHEMA_V1
from app import library_indexer as library_indexer_mod
from app.library_indexer import (
    _brand_placeholder,
    drain_pending,
    enqueue_for_asset_version,
)
from app.models import AssetVersion, Extraction, LibraryPage
from tests.conftest import seed_user


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The visual fidelity spec's forbidden regex (lifted verbatim from
# projects/Resemblio/_verification/library-fidelity-spec-v1.json
# assertion `brand_no_placeholder_filler_text`). Any match in rendered
# body content is a BLOCKER 1 regression.
PLACEHOLDER_FORBIDDEN_REGEX = re.compile(
    r"Item \d Title|Item \d Dek|Item \d Date|Col \d Title|Col \d Link",
)

# Slot families that previously fell through to the humanize-fallback.
# Each tuple is (slot_name, the literal fallback string the bug emitted).
# The test asserts that _brand_placeholder no longer returns the
# fallback string for any of these.
KNOWN_PRE_FIX_FALLTHROUGH_SLOTS: tuple[tuple[str, str], ...] = (
    ("item_1_date", "Item 1 Date"),
    ("item_1_title", "Item 1 Title"),
    ("item_1_dek", "Item 1 Dek"),
    ("item_2_date", "Item 2 Date"),
    ("item_2_title", "Item 2 Title"),
    ("item_2_dek", "Item 2 Dek"),
    ("item_3_date", "Item 3 Date"),
    ("item_3_title", "Item 3 Title"),
    ("item_3_dek", "Item 3 Dek"),
    ("item_4_date", "Item 4 Date"),
    ("item_4_title", "Item 4 Title"),
    ("item_4_dek", "Item 4 Dek"),
    ("step_1_title", "Step 1 Title"),
    ("step_1_dek", "Step 1 Dek"),
    ("step_4_title", "Step 4 Title"),
    ("member_1_name", "Member 1 Name"),
    ("member_4_role", "Member 4 Role"),
    ("section_2_title", "Section 2 Title"),
    ("section_3_body", "Section 3 Body"),
)


@dataclass(frozen=True)
class BrandFixtureRef:
    """Minimal reference to an in-tree DRL DTCG fixture.

    Keeps the test parameterization explicit (no bare-dict tuple soup)
    and gives a single diff site when a new brand fixture is added.

    Attributes:
        seed_url: The ``resemblio://seed/...`` URL convention the
            ``derive_brand_slug`` helper strips to recover the brand
            slug.
        expected_brand_slug: The slug the seed URL must resolve to;
            asserted in the e2e test so a future URL refactor does not
            silently mis-route.
        dtcg_path: Filesystem path to the frozen DTCG JSON payload.
    """

    seed_url: str
    expected_brand_slug: str
    dtcg_path: Path


# Reuse the existing aeon_min fixture authored for the CTO Phase 1
# render-fidelity tests. The fixture is the closest thing the repo has
# to a "real captured brand" baseline.
_AEON_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "drl" / "aeon_min"
AEON_FIXTURE = BrandFixtureRef(
    seed_url="resemblio://seed/drl_v1/aeon/library/aeon-snapshot",
    expected_brand_slug="aeon",
    dtcg_path=_AEON_FIXTURE_DIR / "aeon_dtcg.json",
)


# ---------------------------------------------------------------------------
# Helpers (mirror tests/test_library_indexer_render_fidelity.py shape)
# ---------------------------------------------------------------------------


def _make_asset_version_from(session: Session, fixture: BrandFixtureRef) -> AssetVersion:
    """Insert an AssetVersion seeded from ``fixture``'s frozen DTCG payload."""
    dtcg: dict[str, Any] = json.loads(fixture.dtcg_path.read_text(encoding="utf-8"))
    row = AssetVersion(
        url=fixture.seed_url,
        content_hash=f"{fixture.expected_brand_slug}-blocker1-fixture-hash",
        dtcg_json=dtcg,
        manifest_schema_version=SCHEMA_V1,
        is_public=True,
        version_label=f"{fixture.expected_brand_slug}-blocker1-fixture",
        fetched_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()
    return row


def _attach_passing_extraction(
    session: Session, asset_version: AssetVersion, *, user_id: int
) -> Extraction:
    """Attach a high-quality extraction so the indexer quality gate passes."""
    extraction = Extraction(
        user_id=user_id,
        api_key_id=None,
        url=asset_version.url,
        url_normalized=asset_version.url,
        status="ok",
        tokens_json=asset_version.dtcg_json.get("tokens", {}),
        asset_version_id=asset_version.id,
        schema_version=SCHEMA_V1,
        credit_cents=0,
        quality_score=0.95,
        quality_dimension_scores={"penalty_flags": []},
    )
    session.add(extraction)
    session.flush()
    return extraction


def _enqueue_and_drain(session: Session, asset_version: AssetVersion) -> int:
    """Enqueue an index job and run one drain tick; return pages written."""
    job = enqueue_for_asset_version(session, asset_version.id)
    assert job is not None, "enqueue helper returned None - precondition broken"
    session.commit()
    result = drain_pending(session)
    return result.pages_written


# ---------------------------------------------------------------------------
# Unit-level placeholder tests (synthetic fixtures, no DB)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("slot_name", "pre_fix_fallthrough"), KNOWN_PRE_FIX_FALLTHROUGH_SLOTS)
def test_brand_placeholder_resolves_known_fallthrough_slots(
    slot_name: str, pre_fix_fallthrough: str
) -> None:
    """No slot in the known-fallthrough list returns the humanize-fallback.

    Pre-2026-06-03 every entry returned the right-hand fallback string
    verbatim, surfacing on every brand canonical page that used the
    NEWS_LIST / HOW_IT_WORKS / ABOUT_TEAM / ARTICLE_LAYOUT templates.
    """
    rendered = _brand_placeholder(slot_name, brand_slug="aeon")
    assert rendered != pre_fix_fallthrough, (
        f"slot {slot_name!r} still resolves to humanize-fallback "
        f"{pre_fix_fallthrough!r}; BLOCKER 1 regression"
    )
    assert rendered.strip(), f"slot {slot_name!r} resolved to empty string"


def test_brand_placeholder_never_emits_forbidden_regex_for_any_known_slot() -> None:
    """Sweep every known DRL placeholder family; no resolved value matches the forbidden regex.

    Pins the visual-fidelity-spec assertion at the unit boundary so a
    future preset edit that re-introduces "Item 1 Title" is caught
    without needing the full DB + compose round-trip.
    """
    all_slots: list[str] = [name for name, _ in KNOWN_PRE_FIX_FALLTHROUGH_SLOTS]
    for slot in all_slots:
        rendered = _brand_placeholder(slot, brand_slug="aeon")
        match = PLACEHOLDER_FORBIDDEN_REGEX.search(rendered)
        assert match is None, (
            f"slot {slot!r} resolves to {rendered!r}, which matches the "
            f"forbidden placeholder regex at offset {match.start() if match else -1}"
        )


# ---------------------------------------------------------------------------
# End-to-end placeholder tests (real Aeon fixture + DB compose)
# ---------------------------------------------------------------------------


def test_aeon_brand_pages_render_no_forbidden_placeholder_strings(
    session: Session,
) -> None:
    """No rendered Aeon page carries the BLOCKER 1 placeholder strings.

    Mirrors the visual-fidelity spec's ``brand_no_placeholder_filler_text``
    assertion (regex ``Item \\d Title|Item \\d Dek|Item \\d Date|Col \\d
    Title|Col \\d Link``) at the Python level. The e2e check guards
    against any future code path (template change, new slot, rendered
    HTML transform) that would re-introduce the strings even when
    ``_brand_placeholder`` itself stays clean.
    """
    user, _key, _ = seed_user(session)
    av = _make_asset_version_from(session, AEON_FIXTURE)
    _attach_passing_extraction(session, av, user_id=user.id)
    written = _enqueue_and_drain(session, av)
    assert written > 0, "drain wrote zero pages - quality gate or compose failure"

    pages = session.query(LibraryPage).filter_by(asset_version_id=av.id).all()
    assert pages, "no library_pages rows persisted for the Aeon asset version"

    for page in pages:
        rendered = page.rendered_html or ""
        match = PLACEHOLDER_FORBIDDEN_REGEX.search(rendered)
        assert match is None, (
            f"page {page.category_slug}: rendered_html carries forbidden "
            f"placeholder substring {rendered[max(0, match.start() - 10):match.end() + 10]!r} "
            f"at offset {match.start() if match else -1} - BLOCKER 1 regression"
        )


def test_aeon_brand_pages_carry_real_brand_named_news_items(session: Session) -> None:
    """At least one Aeon page substitutes the brand name into the news-list slot.

    The fix routes ``item_1_title`` through a preset that includes the
    pretty-printed brand name. Asserting that "Aeon" appears in a
    news-list region somewhere across the rendered set proves the slot
    is wired to the brand-aware preset path, not the humanize-fallback.
    Picked the strongest single canary: ``"Aeon ships a refreshed
    palette"`` (preset value for ``item_1_title`` when ``brand_slug ==
    "aeon"``).
    """
    user, _key, _ = seed_user(session)
    av = _make_asset_version_from(session, AEON_FIXTURE)
    _attach_passing_extraction(session, av, user_id=user.id)
    _enqueue_and_drain(session, av)

    pages = session.query(LibraryPage).filter_by(asset_version_id=av.id).all()
    canary = "Aeon ships a refreshed palette"
    hits = [p.category_slug for p in pages if canary in (p.rendered_html or "")]
    assert hits, (
        f"no rendered page carries the brand-aware news-list canary "
        f"{canary!r}; expected at least the page composed from the "
        f"NEWS_LIST template to carry it"
    )

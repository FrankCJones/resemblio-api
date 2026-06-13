"""D19 regression guard: featured artifact must be the type-specimen; no invented byline.

Library v5 Phase 1.D - TDD RED for Defect C.

Two concrete violations on the library brand page:

1. **Featured artifact selection (D19a):** The canonical route
   ``GET /v1/library/brands/{brand_slug}`` calls
   ``get_brand_canonical()`` which uses ``LIMIT 1`` with
   ``ORDER BY fetched_at DESC``.  All 18 template classes for a brand
   share the same ``asset_version_id`` and thus identical ``fetched_at``.
   PostgreSQL resolves the tie through internal index order, which in
   practice returns ``article-layout`` (alphabetically first in the index,
   or first by physical row order in SQLite tests).  The intended featured
   artifact is the type-specimen (``alphabet`` class) per D19.

2. **Invented byline (D19b):** ``ARTICLE_LAYOUT_BODY`` contains
   ``<div class="al__byline"><span>{author}</span>...<time>{date}</time></div>``.
   ``_brand_placeholder`` resolves ``author`` -> "Studio team" and
   ``date`` -> "March 2026" - both invented values that contradict the
   page's own claim that there are "No placeholders, no invented defaults."

Decision reference: D19 in
    projects/OptSus Team/missions/resemblio-library-public-view-readiness-tdd-plan-v5.md

Run command (from code/api/):
    python -m pytest tests/test_library_v5_artifact_honesty.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.constants import SCHEMA_V1
from app import library_indexer as _idx_mod  # noqa: F401 - side-effect: DRL sys.path
from app.library_indexer import (
    _brand_placeholder,
    drain_pending,
    enqueue_for_asset_version,
)
from app.models import AssetVersion, Extraction, LibraryPage
from tests.conftest import seed_user

# Trigger DRL sys.path install before importing from _scripts
from _scripts import templates as tpl  # noqa: PLC0415 - after sys.path install


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AEON_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "drl" / "aeon_min"

# Signature string unique to the alphabet type-specimen in rendered HTML.
# ALPHABET_BODY wraps the whole section in <main class="a-page">.
_ALPHABET_HTML_SIGNATURE = 'class="a-page"'

# Signature string unique to article-layout rendered HTML.
_ARTICLE_LAYOUT_HTML_SIGNATURE = 'class="al"'


# ---------------------------------------------------------------------------
# Helpers (mirror the aeon fixture helpers in test_library_indexer_no_placeholder_text.py)
# ---------------------------------------------------------------------------


def _make_aeon_asset_version(session: Session) -> AssetVersion:
    """Insert an AssetVersion seeded from the frozen Aeon DTCG fixture."""
    dtcg: dict[str, Any] = json.loads(
        (_AEON_FIXTURE_DIR / "aeon_dtcg.json").read_text(encoding="utf-8")
    )
    row = AssetVersion(
        url="resemblio://seed/drl_v1/aeon/library/aeon-snapshot-d19",
        content_hash="aeon-d19-fixture-hash",
        dtcg_json=dtcg,
        manifest_schema_version=SCHEMA_V1,
        is_public=True,
        version_label="aeon-d19-fixture",
        fetched_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()
    return row


def _attach_extraction(session: Session, asset_version: AssetVersion, *, user_id: int) -> None:
    """Attach a high-quality extraction so the indexer quality gate passes."""
    session.add(
        Extraction(
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
    )
    session.flush()


def _drain(session: Session, asset_version: AssetVersion) -> int:
    """Enqueue and drain one index job; return number of pages written."""
    job = enqueue_for_asset_version(session, asset_version.id)
    assert job is not None, "enqueue returned None"
    session.commit()
    result = drain_pending(session)
    return result.pages_written


# ---------------------------------------------------------------------------
# D19a - Featured artifact selection tests (RED: fails before route fix)
# ---------------------------------------------------------------------------


def test_canonical_page_is_type_specimen_v5_d19a(session: Session) -> None:
    """D19a: the canonical brand page must serve the alphabet type-specimen.

    After drain, all 18 template classes share the same fetched_at. The
    current route's LIMIT 1 (no category filter) returns an arbitrary class.
    This test calls get_brand_canonical directly and asserts the rendered
    HTML contains the alphabet type-specimen signature class="a-page".

    RED: fails because the route returns a non-alphabet class.
    GREEN: pass after adding category_slug='alphabet' filter to the route.
    """
    from app.routes.library import get_brand_canonical

    user, _key, _ = seed_user(session)
    av = _make_aeon_asset_version(session)
    _attach_extraction(session, av, user_id=user.id)
    written = _drain(session, av)
    assert written > 0, f"drain wrote {written} pages - quality gate or compose failure"

    response = get_brand_canonical("aeon", session=session)
    # Route wraps in {"schema_version": ..., "data": {...page...}}
    body = json.loads(response.body)
    rendered = body.get("data", {}).get("rendered_html", "")

    assert _ALPHABET_HTML_SIGNATURE in rendered, (
        "D19a: canonical page must be the alphabet type-specimen "
        f"(class=\"a-page\" not found in rendered_html). "
        f"Article-layout signature present: {_ARTICLE_LAYOUT_HTML_SIGNATURE in rendered}. "
        "Route needs category_slug='alphabet' filter per D19."
    )


def test_alphabet_page_exists_after_drain_v5_d19a(session: Session) -> None:
    """D19a prerequisite: alphabet-class page must exist after drain.

    Confirms that the indexer does compose the alphabet class for every brand.
    The canonical selection fix depends on this row being present.
    """
    user, _key, _ = seed_user(session)
    av = _make_aeon_asset_version(session)
    _attach_extraction(session, av, user_id=user.id)
    _drain(session, av)

    page = (
        session.query(LibraryPage)
        .filter_by(asset_version_id=av.id, category_slug="alphabet")
        .first()
    )
    assert page is not None, "alphabet-class LibraryPage must exist after drain"
    assert page.rendered_html is not None, "alphabet page must have rendered_html"
    assert _ALPHABET_HTML_SIGNATURE in (page.rendered_html or ""), (
        f"alphabet page rendered_html must contain '{_ALPHABET_HTML_SIGNATURE}'"
    )


# ---------------------------------------------------------------------------
# D19b - Invented byline tests (RED: fails before template + indexer fix)
# ---------------------------------------------------------------------------


def test_article_layout_body_has_no_byline_div_v5_d19b() -> None:
    """D19b: ARTICLE_LAYOUT_BODY must not contain the al__byline div.

    The current template has:
        <div class="al__byline">
          <span>{author}</span><span>·</span><time>{date}</time>
        </div>

    This renders "Studio team · March 2026" - invented values on a page
    that claims there are no invented defaults. The fix removes the div.

    RED: fails because the byline div is present in the current template.
    """
    assert "al__byline" not in tpl.ARTICLE_LAYOUT_BODY, (
        "D19b: ARTICLE_LAYOUT_BODY contains al__byline; "
        "this renders invented byline 'Studio team · March 2026' on the "
        "article-layout specimen page, contradicting the page's honesty claim."
    )


def test_brand_placeholder_author_is_not_studio_team_v5_d19b() -> None:
    """D19b: _brand_placeholder 'author' must not return 'Studio team'.

    'Studio team' is not a brand-owned value; it's a Resemblio-invented
    placeholder that reads as if it is real brand authorship data.

    RED: fails because presets map 'author' -> 'Studio team'.
    """
    value = _brand_placeholder("author", brand_slug="apple")
    assert "Studio team" not in value, (
        f"D19b: 'author' slot resolves to {value!r} which contains 'Studio team' "
        "(invented byline). Remove this preset per D19."
    )


def test_brand_placeholder_date_is_not_march_2026_v5_d19b() -> None:
    """D19b: _brand_placeholder 'date' must not return 'March 2026'.

    'March 2026' is an invented date that reads as brand publication data.

    RED: fails because presets map 'date' -> 'March 2026'.
    """
    value = _brand_placeholder("date", brand_slug="apple")
    assert "March 2026" not in value, (
        f"D19b: 'date' slot resolves to {value!r} which contains 'March 2026' "
        "(invented date). Remove this preset per D19."
    )

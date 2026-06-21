"""Tests for the library indexer service (mission Phase 4).

Coverage matrix (mission spec):

- happy path: 1 eligible asset_version -> N library_pages rows (N = registered template count)
- quality gate fail: low quality_score -> 0 rows
- quality gate fail: penalty flags present -> 0 rows
- is_public=False -> 0 rows
- is_canonical flip: 2 versions of one brand -> newer canonical, older not
- retry on compose failure -> status pending + attempts incremented
- retry exhaustion -> status failed
- idempotency: re-running for same asset_version does NOT duplicate rows
- enqueue from seed: enqueue helper inserts a job row
- enqueue from route: POST /v1/extractions creates a job row
- derive_brand_slug helper coverage (seed URLs + organic URLs)

The DRL ``_scripts`` package is imported lazily inside the indexer; tests
assert on row counts via the registered template set rather than hard-coding
"18" so the test stays green if the registry grows or shrinks.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.constants import (
    LIBRARY_INDEX_MAX_ATTEMPTS,
    LIBRARY_INDEX_QUALITY_THRESHOLD,
    SCHEMA_V1,
)
from app.library_indexer import (
    _compose_one_page,
    _tokens_to_inline_css,
    derive_brand_slug,
    drain_pending,
    enqueue_for_asset_version,
    tokens_for_compose,
)
from app.models import AssetVersion, Extraction, LibraryIndexJob, LibraryPage
from tests.conftest import auth_headers, seed_user


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


_HEALTHY_TOKENS: dict[str, str] = {
    "bg": "#0a0a0a",
    "surface": "#1a1a1a",
    "text": "#ffffff",
    "accent": "#ff3366",
    "font_body": "Inter, sans-serif",
    "font_display": "Playfair Display, serif",
}


def _registered_template_count() -> int:
    """Return the number of compose-template classes the indexer renders."""
    from _scripts.templates import TEMPLATES_BY_CLASS  # noqa: PLC0415

    return len(TEMPLATES_BY_CLASS)


def _make_asset_version(
    session: Session,
    *,
    url: str = "https://stripe.com/",
    is_public: bool = True,
    fetched_at: datetime | None = None,
    version_label: str | None = None,
    tokens: dict[str, str] | None = None,
) -> AssetVersion:
    """Insert an asset_versions row with an in-test DTCG payload."""
    payload_tokens = tokens if tokens is not None else _HEALTHY_TOKENS
    dtcg: dict[str, Any] = {
        "schema_version": SCHEMA_V1,
        "slug": "stripe",
        "class": "buttons",
        "tokens": dict(payload_tokens),
    }
    row = AssetVersion(
        url=url,
        content_hash=f"hash-{url}-{fetched_at!s}",
        dtcg_json=dtcg,
        manifest_schema_version=SCHEMA_V1,
        is_public=is_public,
        version_label=version_label,
        fetched_at=fetched_at or datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()
    return row


def _attach_scored_extraction(
    session: Session,
    asset_version: AssetVersion,
    *,
    user_id: int,
    quality_score: float = 0.9,
    penalty_flags: list[str] | None = None,
) -> Extraction:
    """Attach an extraction row carrying the quality-gate signals."""
    dimensions: dict[str, Any] = {"penalty_flags": list(penalty_flags or [])}
    extraction = Extraction(
        user_id=user_id,
        api_key_id=None,
        url=asset_version.url,
        url_normalized=asset_version.url,
        status="ok",
        tokens_json=asset_version.dtcg_json["tokens"],
        asset_version_id=asset_version.id,
        schema_version=SCHEMA_V1,
        credit_cents=0,
        quality_score=quality_score,
        quality_dimension_scores=dimensions,
    )
    session.add(extraction)
    session.flush()
    return extraction


def _enqueue(session: Session, asset_version: AssetVersion) -> LibraryIndexJob:
    """Helper: enqueue and return the resulting job row."""
    job = enqueue_for_asset_version(session, asset_version.id)
    assert job is not None
    session.commit()
    return job


# ----------------------------------------------------------------------
# Unit tests: pure helpers
# ----------------------------------------------------------------------


def test_derive_brand_slug_handles_seed_and_organic_urls() -> None:
    """Seed-shape URLs and organic URLs both produce stable slugs."""
    seed_url = "resemblio://seed/drl_v1/stripe-com/buttons/primary-cta"
    organic_url = "https://stripe.com/pricing"
    assert derive_brand_slug(seed_url) == "stripe-com"
    assert derive_brand_slug(organic_url) == "stripe-com"


def test_tokens_for_compose_handles_nested_and_flat_shapes() -> None:
    """The compose adapter accepts both seed (nested) and organic (flat) DTCG."""
    nested = {"tokens": {"bg": "#fff", "accent": "#f00"}}
    flat = {"bg": "#fff", "accent": "#f00", "patterns": ["foo"]}
    assert tokens_for_compose(nested) == {"bg": "#fff", "accent": "#f00"}
    # The flat shape drops non-string/number values (patterns list filtered out).
    assert tokens_for_compose(flat) == {"bg": "#fff", "accent": "#f00"}


# ----------------------------------------------------------------------
# Compose output shape (Bug 2a + 2b regression coverage)
# ----------------------------------------------------------------------


def test_compose_one_page_emits_body_fragment_with_real_tokens() -> None:
    """Composed rendered_html is a body fragment carrying the brand's real tokens.

    Regression for Bug 2 (2026-06-02): the indexer was producing full
    ``<!doctype html>...`` documents with un-interpolated Lorem placeholder
    text and no live token values. After the fix, output is:

    - an ``<article>`` body fragment (no doctype, no nested ``<html>``)
    - inlined ``:root { --ds-*: ... }`` carrying every passed brand token
      so DRL template ``var(--ds-*)`` references resolve at paint time
    - safe to inject into a Next.js page without breaking the parent DOM
    """
    rendered = _compose_one_page(
        "navigation",
        brand_slug="aeon",
        tokens=_HEALTHY_TOKENS,
    )
    # (a) NO Lorem-ipsum placeholder text leaks into user-facing copy.
    lowered = rendered.lower()
    assert "lorem ipsum" not in lowered
    assert "consectetur adipiscing" not in lowered
    # (b) NO doctype / nested html document
    assert "<!doctype" not in lowered
    assert "<html" not in lowered
    assert "<head>" not in lowered
    # The fragment IS an article wrapper the web mapper expects.
    assert rendered.startswith('<article class="rs-library-page"')
    # (c) every brand token value reaches the page text.
    for token_value in _HEALTHY_TOKENS.values():
        assert token_value in rendered, f"missing token value {token_value!r}"
    # Custom-property names use the DRL --ds-* convention with dashes.
    assert "--ds-bg:" in rendered
    assert "--ds-font-display:" in rendered


def test_compose_one_page_article_layout_strips_lorem() -> None:
    """The article-layout class (visible regression on aeon) no longer leaks Lorem."""
    rendered = _compose_one_page(
        "article-layout",
        brand_slug="aeon",
        tokens=_HEALTHY_TOKENS,
    )
    lowered = rendered.lower()
    assert "lorem ipsum" not in lowered
    assert "consectetur adipiscing" not in lowered
    assert "<!doctype" not in lowered
    # Brand-aware title slot uses the brand name, not Lorem.
    assert "Aeon" in rendered


def test_tokens_to_inline_css_emits_ds_custom_properties() -> None:
    """Underscore token names map to dashed --ds-* custom properties, sorted."""
    css = _tokens_to_inline_css({"bg": "#000", "font_display": "Inter"})
    assert ":root {" in css
    assert "--ds-bg: #000;" in css
    assert "--ds-font-display: Inter;" in css
    # Sorted: bg before font_display
    assert css.index("--ds-bg") < css.index("--ds-font-display")


def test_tokens_to_inline_css_empty_tokens_yields_contract_defaults() -> None:
    """Per Path C (2026-06-03), empty tokens populate every contract slot with its
    default value via _emit_brand_root. Back-compat: rendered output matches
    pre-Path-C because templates use var(--ds-*, <literal>) where <literal>
    equals the contract default."""
    out = _tokens_to_inline_css({})
    assert out.startswith(":root {")
    assert out.endswith("}")
    assert "--ds-bg:" in out
    assert "--ds-radius-sm:" in out
    assert "--ds-button-radius:" in out


# ----------------------------------------------------------------------
# drain_pending: happy path
# ----------------------------------------------------------------------


def test_drain_pending_happy_path_writes_one_page_per_template(session: Session) -> None:
    """A single eligible asset_version yields exactly one row per template class."""
    user, _key, _ = seed_user(session)
    av = _make_asset_version(session)
    _attach_scored_extraction(session, av, user_id=user.id, quality_score=0.95)
    _enqueue(session, av)

    result = drain_pending(session)

    assert result.jobs_run == 1
    expected = _registered_template_count()
    pages = session.query(LibraryPage).filter_by(asset_version_id=av.id).all()
    assert len(pages) == expected
    assert result.pages_written == expected
    # Every page carries the v1 metadata schema tag.
    for page in pages:
        assert page.metadata_json["schema_version"] == 1
        assert page.brand_slug == "stripe-com"


# ----------------------------------------------------------------------
# Quality-gate skips
# ----------------------------------------------------------------------


def test_drain_pending_skips_when_quality_below_threshold(session: Session) -> None:
    """A low quality_score blocks page generation; job completes with reason."""
    user, _key, _ = seed_user(session)
    av = _make_asset_version(session)
    low = LIBRARY_INDEX_QUALITY_THRESHOLD - 0.1
    _attach_scored_extraction(session, av, user_id=user.id, quality_score=low)
    job = _enqueue(session, av)

    result = drain_pending(session)

    assert result.jobs_run == 1
    assert result.pages_written == 0
    assert session.query(LibraryPage).count() == 0
    session.refresh(job)
    assert job.status == "complete"
    assert job.last_error is not None
    assert "threshold" in job.last_error


def test_drain_pending_skips_when_penalty_flags_present(session: Session) -> None:
    """Any penalty flag blocks page generation even when score is high."""
    user, _key, _ = seed_user(session)
    av = _make_asset_version(session)
    _attach_scored_extraction(
        session, av, user_id=user.id,
        quality_score=0.95,
        penalty_flags=["all_common_default_colors"],
    )
    _enqueue(session, av)

    result = drain_pending(session)

    assert result.pages_written == 0
    assert session.query(LibraryPage).count() == 0


def test_drain_pending_skips_when_asset_version_not_public(session: Session) -> None:
    """is_public=False blocks every job regardless of quality."""
    user, _key, _ = seed_user(session)
    av = _make_asset_version(session, is_public=False)
    _attach_scored_extraction(session, av, user_id=user.id, quality_score=0.95)
    _enqueue(session, av)

    result = drain_pending(session)

    assert result.pages_written == 0
    assert session.query(LibraryPage).count() == 0


# ----------------------------------------------------------------------
# is_canonical reconciliation
# ----------------------------------------------------------------------


def test_canonical_flips_when_newer_version_indexed(session: Session) -> None:
    """Indexing a newer asset_version for the same brand flips canonical."""
    user, _key, _ = seed_user(session)
    older_at = datetime.now(timezone.utc) - timedelta(days=30)
    newer_at = datetime.now(timezone.utc)
    av_old = _make_asset_version(
        session, url="https://stripe.com/", fetched_at=older_at, version_label="v1"
    )
    _attach_scored_extraction(session, av_old, user_id=user.id, quality_score=0.9)
    _enqueue(session, av_old)
    drain_pending(session)

    av_new = _make_asset_version(
        session,
        url="https://stripe.com/",
        fetched_at=newer_at,
        version_label="v2",
        # Distinct content so insert_or_reuse path isn't triggered against a
        # duplicate (test uses a manual constructor, so this is moot in
        # practice but keeps the intent obvious).
        tokens={**_HEALTHY_TOKENS, "bg": "#020202"},
    )
    _attach_scored_extraction(session, av_new, user_id=user.id, quality_score=0.95)
    _enqueue(session, av_new)
    drain_pending(session)

    newer_pages = session.query(LibraryPage).filter_by(asset_version_id=av_new.id).all()
    older_pages = session.query(LibraryPage).filter_by(asset_version_id=av_old.id).all()
    assert newer_pages and all(p.is_canonical for p in newer_pages)
    assert older_pages and not any(p.is_canonical for p in older_pages)


def test_canonical_prefers_nonempty_over_empty_sibling(session: Session) -> None:
    """issue #31: the real-content page wins canonical over an empty sibling.

    In the cross-category page model every whole asset_version writes a page
    for every template class; classes it does not own render to ``""``. After
    a single corpus re-seed all wholes share one ``fetched_at``, so a
    ``fetched_at``-only ranking could crown an empty placeholder canonical and
    serve a blank ``/library/<brand>/<category>`` page. This pins the fix:
    a non-empty page must win even when an empty sibling is *newer*.
    """
    from app.library_indexer import _reconcile_canonical, derive_brand_slug

    seed_user(session)
    ts = datetime.now(timezone.utc)
    brand_url = "https://stripe.com/"
    brand = derive_brand_slug(brand_url)
    av_real = _make_asset_version(
        session, url=brand_url, fetched_at=ts, version_label="real"
    )
    # Empty sibling fetched one second LATER: a fetched_at-only ranking would
    # wrongly prefer it. The fix ranks non-empty content first.
    av_empty = _make_asset_version(
        session,
        url=brand_url,
        fetched_at=ts + timedelta(seconds=1),
        version_label="empty",
        tokens={**_HEALTHY_TOKENS, "bg": "#010101"},
    )
    real_page = LibraryPage(
        asset_version_id=av_real.id,
        category_slug="buttons",
        brand_slug=brand,
        version_label="real",
        rendered_html='<article data-rs-source="drl-component">real</article>',
        metadata_json={},
        is_canonical=False,
    )
    empty_page = LibraryPage(
        asset_version_id=av_empty.id,
        category_slug="buttons",
        brand_slug=brand,
        version_label="empty",
        rendered_html="",
        metadata_json={},
        is_canonical=True,  # the bug state: empty page currently canonical
    )
    session.add_all([real_page, empty_page])
    session.flush()

    _reconcile_canonical(session, av_empty)
    session.refresh(real_page)
    session.refresh(empty_page)

    assert real_page.is_canonical is True
    assert empty_page.is_canonical is False


# ----------------------------------------------------------------------
# Retry semantics
# ----------------------------------------------------------------------


def test_compose_failure_flips_job_back_to_pending(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compose exception bumps attempts and returns the row to pending."""
    user, _key, _ = seed_user(session)
    av = _make_asset_version(session)
    _attach_scored_extraction(session, av, user_id=user.id, quality_score=0.95)
    job = _enqueue(session, av)

    import app.library_indexer as indexer

    def _boom(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("compose blew up")

    monkeypatch.setattr(indexer, "_compose_one_page", _boom)

    result = drain_pending(session)

    assert result.jobs_run == 1
    session.refresh(job)
    assert job.status == "pending"
    assert job.attempts == 1
    assert "compose blew up" in (job.last_error or "")
    assert session.query(LibraryPage).count() == 0


def test_compose_failure_exhausts_retry_budget(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three failures park the job at status=failed."""
    user, _key, _ = seed_user(session)
    av = _make_asset_version(session)
    _attach_scored_extraction(session, av, user_id=user.id, quality_score=0.95)
    job = _enqueue(session, av)

    import app.library_indexer as indexer

    def _boom(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("perma-fail")

    monkeypatch.setattr(indexer, "_compose_one_page", _boom)

    for _ in range(LIBRARY_INDEX_MAX_ATTEMPTS):
        # Each tick picks up the row (status == pending) until attempts hit cap.
        drain_pending(session)

    session.refresh(job)
    assert job.status == "failed"
    assert job.attempts == LIBRARY_INDEX_MAX_ATTEMPTS


# ----------------------------------------------------------------------
# Idempotency
# ----------------------------------------------------------------------


def test_re_running_same_asset_version_does_not_duplicate_pages(session: Session) -> None:
    """The UNIQUE constraint blocks duplicate (asset_version_id, category) rows."""
    user, _key, _ = seed_user(session)
    av = _make_asset_version(session)
    _attach_scored_extraction(session, av, user_id=user.id, quality_score=0.95)
    _enqueue(session, av)
    drain_pending(session)
    initial_count = session.query(LibraryPage).count()
    assert initial_count == _registered_template_count()

    # Re-enqueue the same asset_version and drain again. The compose pass
    # should hit the unique constraint and produce zero new rows.
    _enqueue(session, av)
    drain_pending(session)

    assert session.query(LibraryPage).count() == initial_count


def test_re_enqueue_self_heals_stale_rendered_html(session: Session) -> None:
    """Re-enqueue UPDATEs rendered_html on an existing (av, category) row.

    L-17 root cause (2026-06-03): the IntegrityError branch in
    ``_process_job`` historically rolled back and continued, which silently
    froze any library_pages row written under an earlier template or
    compose pipeline. The fix turns the conflict into an UPDATE so a
    re-enqueue actually refreshes stale content; this test pins that
    contract so a future revert lands red here instead of shipping empty
    alphabet pages to production again.
    """
    user, _key, _ = seed_user(session)
    av = _make_asset_version(session)
    _attach_scored_extraction(session, av, user_id=user.id, quality_score=0.95)
    _enqueue(session, av)
    drain_pending(session)

    # Simulate a stale row by zeroing out rendered_html on one page in
    # place. (In production this models a row written by an older indexer
    # version whose template emitted no substantive body markup.)
    target = (
        session.query(LibraryPage)
        .filter(LibraryPage.asset_version_id == av.id)
        .filter(LibraryPage.category_slug == "alphabet")
        .one()
    )
    target.rendered_html = ""
    session.flush()
    assert target.rendered_html == ""

    # Re-enqueue + drain. The conflict path must UPDATE the stale row
    # rather than skip it.
    _enqueue(session, av)
    drain_pending(session)

    refreshed = (
        session.query(LibraryPage)
        .filter(LibraryPage.asset_version_id == av.id)
        .filter(LibraryPage.category_slug == "alphabet")
        .one()
    )
    assert refreshed.rendered_html, (
        "L-17 regression: re-enqueue did not refresh stale alphabet "
        "rendered_html (IntegrityError branch reverted to rollback-and-skip)"
    )
    # Substantive marker from the DRL alphabet template: a freshly composed
    # row carries the per-row specimen wrapper.
    assert "a-row" in refreshed.rendered_html, (
        "L-17 regression: refreshed alphabet row lacks the substantive "
        "body markup ('a-row' wrapper). Compose pipeline wrote chrome only."
    )


# ----------------------------------------------------------------------
# Enqueue trigger surfaces
# ----------------------------------------------------------------------


def test_enqueue_is_idempotent_against_live_jobs(session: Session) -> None:
    """A second enqueue against a pending job returns None (no duplicate row)."""
    user, _key, _ = seed_user(session)
    av = _make_asset_version(session)
    _attach_scored_extraction(session, av, user_id=user.id, quality_score=0.95)
    first = enqueue_for_asset_version(session, av.id)
    second = enqueue_for_asset_version(session, av.id)
    session.commit()
    assert first is not None
    assert second is None
    assert session.query(LibraryIndexJob).filter_by(asset_version_id=av.id).count() == 1


def test_route_post_extraction_enqueues_library_index_job(client, session: Session) -> None:
    """POST /v1/extractions creates a library_index_jobs row via the route hook."""
    user, _key, plaintext = seed_user(session)
    response = client.post(
        "/v1/extractions",
        json={"url": "https://stripe.com/"},
        headers=auth_headers(plaintext),
    )
    assert response.status_code in (200, 201)
    jobs = session.query(LibraryIndexJob).all()
    assert len(jobs) == 1
    assert jobs[0].status == "pending"

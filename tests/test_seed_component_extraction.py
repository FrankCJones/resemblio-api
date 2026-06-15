"""Tests for DRL component-extraction functions added in issue #2.

Validates the four pure functions (strip_provenance_comments, extract_component_css,
extract_component_html, derive_states_present) and the persist-path integration
that writes asset_components rows from asset.html files.

All tests use synthetic in-memory fixtures or a tmpdir; no real DRL access,
no network.

Do this work at a level that would impress a senior developer.
Include documentation and code comments that make it easy for a future developer
to maintain this project.
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AssetComponent, Extraction
from scripts.seed_from_drl import (
    DEFAULT_BATCH_SIZE,
    apply_seed,
    derive_states_present,
    extract_component_css,
    extract_component_html,
    iter_assets,
    load_asset_html,
    load_corpus,
    strip_provenance_comments,
)


# ---------------------------------------------------------------------------
# Synthetic HTML fixtures (mirror the real DRL a24-cinematic-001 structure)
# ---------------------------------------------------------------------------

# A realistic asset.html that includes both comment forms with provenance data
# and all four interactive states in the inline <style> block.
_HTML_WITH_BOTH_COMMENT_FORMS = """\
<!-- Inspired by: Test Brand - do not distribute -->
<!doctype html>
<html lang="en">
<head>
<style>
/* Proprietary colour palette */
.btn { background: var(--ds-bg); color: var(--ds-text); }
.btn:hover { background: var(--ds-accent); }
.btn:focus-visible { outline: 2px solid var(--ds-focus-ring); }
.btn[disabled] { opacity: 0.5; cursor: not-allowed; }
</style>
</head>
<body>
<!-- HTML comment: do not serve this brand name -->
<button class="btn">Click me</button>
<button class="btn" disabled>Disabled</button>
</body>
</html>
"""

_HTML_NO_BODY = """\
<!doctype html>
<html>
<head><title>No body</title></head>
<p>Content without a body tag.</p>
</html>
"""

_HTML_MULTIPLE_STYLE_BLOCKS = """\
<html>
<head>
<style>/* block A */ .a { color: red; }</style>
<style>.b { color: blue; } .b:hover { color: darkblue; }</style>
</head>
<body><p>hello</p></body>
</html>
"""

# CSS strings covering each state combination tested by derive_states_present.
_CSS_ALL_STATES = """\
.btn { background: red; }
.btn:hover { background: darkred; }
.btn:focus-visible { outline: 2px solid blue; }
.btn:active { transform: scale(0.98); }
.btn[disabled] { opacity: 0.5; }
"""

_CSS_ONLY_REST = ".btn { background: red; }"

_CSS_FOCUS_AND_ARIA_DISABLED = """\
.btn:focus { outline: 2px solid blue; }
.btn[aria-disabled="true"] { opacity: 0.5; }
"""


# ---------------------------------------------------------------------------
# strip_provenance_comments
# ---------------------------------------------------------------------------


def test_strip_provenance_comments_removes_html_comments() -> None:
    """HTML block comments (<!-- -->) are stripped; surrounding text is preserved."""
    text = "before <!-- this is a comment --> after"
    result = strip_provenance_comments(text)
    assert "<!--" not in result
    assert "-->" not in result
    assert "this is a comment" not in result
    assert "before" in result
    assert "after" in result


def test_strip_provenance_comments_removes_css_comments() -> None:
    """CSS block comments (/* */) are stripped; surrounding text is preserved."""
    text = ".btn { /* Inspired by: Brand */ color: red; }"
    result = strip_provenance_comments(text)
    assert "/*" not in result
    assert "*/" not in result
    assert "Inspired by: Brand" not in result
    assert "color: red" in result


def test_strip_provenance_comments_handles_multiline_html_comment() -> None:
    """Multiline HTML comments spanning several lines are removed as a single unit."""
    text = "a\n<!--\n  Brand: ACME\n  Notes: private\n-->\nb"
    result = strip_provenance_comments(text)
    assert "Brand: ACME" not in result
    assert "a" in result
    assert "b" in result


def test_strip_provenance_comments_handles_multiline_css_comment() -> None:
    """Multiline CSS block comments are removed as a single unit."""
    text = ".x { /* line1\n   Proprietary\n   line3 */ color: blue; }"
    result = strip_provenance_comments(text)
    assert "Proprietary" not in result
    assert "color: blue" in result


def test_strip_provenance_comments_preserves_rendered_text() -> None:
    """Content outside any comment form is returned unchanged."""
    text = ".btn { color: red; } .btn:hover { color: darkred; }"
    assert strip_provenance_comments(text) == text


# ---------------------------------------------------------------------------
# extract_component_css
# ---------------------------------------------------------------------------


def test_extract_component_css_returns_style_block_content() -> None:
    """State selector rules from an inline <style> block survive extraction."""
    result = extract_component_css(_HTML_WITH_BOTH_COMMENT_FORMS)
    # At least the base selector and state selectors must be present.
    assert ".btn" in result
    assert ".btn:hover" in result
    assert ".btn:focus-visible" in result
    assert ".btn[disabled]" in result


def test_extract_component_css_strips_css_comments() -> None:
    """CSS block comments inside <style> are removed from the returned text."""
    result = extract_component_css(_HTML_WITH_BOTH_COMMENT_FORMS)
    assert "/*" not in result
    assert "Proprietary colour palette" not in result


def test_extract_component_css_concatenates_multiple_style_blocks() -> None:
    """Multiple <style> blocks in one document are concatenated in document order."""
    result = extract_component_css(_HTML_MULTIPLE_STYLE_BLOCKS)
    assert ".a {" in result
    assert ".b {" in result
    assert ".b:hover" in result
    # The comment in block A must be stripped.
    assert "/* block A */" not in result


def test_extract_component_css_returns_empty_string_when_no_style_tags() -> None:
    """Documents with no <style> tags return an empty string, not None or a crash."""
    no_style = "<html><body><p>no styles here</p></body></html>"
    result = extract_component_css(no_style)
    assert result == ""


# ---------------------------------------------------------------------------
# extract_component_html
# ---------------------------------------------------------------------------


def test_extract_component_html_returns_body_inner_html() -> None:
    """The inner HTML of <body>...</body> is returned."""
    result = extract_component_html(_HTML_WITH_BOTH_COMMENT_FORMS)
    assert '<button class="btn">Click me</button>' in result
    assert '<button class="btn" disabled>Disabled</button>' in result


def test_extract_component_html_strips_html_comments_from_body() -> None:
    """HTML comments inside <body> are removed from the returned markup fragment."""
    result = extract_component_html(_HTML_WITH_BOTH_COMMENT_FORMS)
    assert "<!--" not in result
    assert "do not serve this brand name" not in result


def test_extract_component_html_does_not_include_head_content() -> None:
    """The <head>-level <style> block does not appear in the extracted body fragment."""
    result = extract_component_html(_HTML_WITH_BOTH_COMMENT_FORMS)
    assert "<style>" not in result
    assert "Proprietary colour palette" not in result


def test_extract_component_html_fallback_when_no_body() -> None:
    """When no <body> tag exists the function returns the doc minus <head> and logs a warning.

    The warning is captured with a handler attached directly to the
    ``seed_from_drl`` logger rather than pytest's ``caplog`` fixture. ``caplog``
    relies on log-record propagation to the root logger, which is unreliable
    under full-suite ordering: other tests in this suite mutate that logger's
    propagation/level state, so this assertion would fail only when the test
    runs after them. The pre-existing seed tests document this exact flakiness
    with ``@pytest.mark.xfail(strict=False)``. Attaching our own handler makes
    the assertion deterministic without weakening it.
    """
    import logging

    from scripts.seed_from_drl import LOG as seed_log

    records: list[logging.LogRecord] = []

    class _CaptureHandler(logging.Handler):
        """Append every emitted record so the test can assert on it directly."""

        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _CaptureHandler(level=logging.WARNING)
    seed_log.addHandler(handler)
    # Pin the logger level so the WARNING is not gated by a level a prior test
    # may have raised; restore it afterward to avoid leaking state.
    prior_level = seed_log.level
    seed_log.setLevel(logging.WARNING)
    try:
        result = extract_component_html(_HTML_NO_BODY)
    finally:
        seed_log.removeHandler(handler)
        seed_log.setLevel(prior_level)

    # <head> content must be absent; body-less content must survive.
    assert "<head>" not in result
    assert "<title>" not in result
    assert "Content without a body tag" in result
    # Operators must be alerted that this asset is structurally unusual.
    assert any("body" in record.getMessage().lower() for record in records), (
        f"expected a warning mentioning 'body'; got {[r.getMessage() for r in records]}"
    )


# ---------------------------------------------------------------------------
# derive_states_present
# ---------------------------------------------------------------------------


def test_derive_states_present_always_includes_rest() -> None:
    """'rest' is present in the result for every CSS input, including stateless CSS."""
    assert "rest" in derive_states_present(_CSS_ONLY_REST)


def test_derive_states_present_stateless_css_returns_only_rest() -> None:
    """CSS with no interactive selectors returns exactly ['rest']."""
    assert derive_states_present(_CSS_ONLY_REST) == ["rest"]


def test_derive_states_present_empty_css_returns_rest_only() -> None:
    """An empty string returns ['rest']; the function must not crash."""
    assert derive_states_present("") == ["rest"]


def test_derive_states_present_detects_all_four_states() -> None:
    """CSS carrying :hover, :focus-visible, :active, and [disabled] maps all four states."""
    result = derive_states_present(_CSS_ALL_STATES)
    assert "hover" in result
    assert "focus" in result
    assert "active" in result
    assert "disabled" in result
    assert "rest" in result


def test_derive_states_present_result_is_sorted_and_deduplicated() -> None:
    """The returned list is alphabetically sorted and contains no duplicates."""
    result = derive_states_present(_CSS_ALL_STATES)
    assert result == sorted(set(result))


def test_derive_states_present_detects_focus_and_aria_disabled_variants() -> None:
    """:focus (without -visible) and [aria-disabled='true'] are recognized."""
    result = derive_states_present(_CSS_FOCUS_AND_ARIA_DISABLED)
    assert "focus" in result
    assert "disabled" in result
    assert "rest" in result
    # active and hover are absent from the fixture CSS.
    assert "active" not in result
    assert "hover" not in result


def test_derive_states_present_deduplicates_focus_forms() -> None:
    """CSS with both :focus and :focus-visible does not produce duplicate 'focus' entries."""
    css = ".a:focus { color: red; } .b:focus-visible { outline: 1px solid blue; }"
    result = derive_states_present(css)
    assert result.count("focus") == 1


# ---------------------------------------------------------------------------
# load_asset_html
# ---------------------------------------------------------------------------


def test_load_asset_html_reads_file(tmp_path: Path) -> None:
    """load_asset_html returns the file contents when asset.html exists."""
    asset_dir = tmp_path / "assets" / "atoms" / "buttons" / "test-btn-001"
    asset_dir.mkdir(parents=True)
    content = "<html><body><p>test content</p></body></html>"
    (asset_dir / "asset.html").write_text(content, encoding="utf-8")
    asset: dict = {
        "slug": "test-btn-001",
        "class": "buttons",
        "path": "assets/atoms/buttons/test-btn-001",
        "tokens_path": "assets/atoms/buttons/test-btn-001/tokens.css",
    }
    result = load_asset_html(tmp_path, asset)
    assert result is not None
    assert "<p>test content</p>" in result


def test_load_asset_html_returns_none_when_file_absent(tmp_path: Path) -> None:
    """load_asset_html returns None gracefully when the asset.html file does not exist."""
    asset: dict = {
        "slug": "missing-btn-001",
        "class": "buttons",
        "path": "assets/atoms/buttons/missing-btn-001",
    }
    result = load_asset_html(tmp_path, asset)
    assert result is None


def test_load_asset_html_returns_none_when_path_field_absent(tmp_path: Path) -> None:
    """load_asset_html returns None when the asset dict has no 'path' field."""
    result = load_asset_html(tmp_path, {})
    assert result is None


# ---------------------------------------------------------------------------
# Persist-path integration: apply_seed writes asset_components rows
# ---------------------------------------------------------------------------


def _seed_user_for_component_test(session: Session) -> int:
    """Create a minimal user row and return its PK.

    Mirrors the pattern in test_seed_from_drl._seed_user; self-contained so
    this file does not import from another test module.
    """
    from app.crypto import hash_password
    from app.models import User

    user = User(
        email="component-test@resemblio.test",
        password_hash=hash_password("x"),
        status="active",
    )
    session.add(user)
    session.flush()
    return user.id


class _FakeStorageForComponentTest:
    """Minimal StorageClient stub for persist-path component tests.

    Discards uploads; tests only inspect the DB rows.
    """

    def put_object_at_key(self, key: str, body: bytes, content_type: str) -> None:
        """Accept and silently discard the upload."""


def _write_component_drl(root: Path) -> None:
    """Write a minimal synthetic DRL tree with one asset that has asset.html.

    The asset.html includes hover, focus-visible, and disabled state CSS plus
    both HTML and CSS comment forms so the persist-path test can verify that:
    - states_present is derived correctly from the CSS
    - no comment bytes survive into the asset_components row
    """
    corpus = {
        "schema_version": 1,
        "generated": "2026-06-15",
        "asset_count": 1,
        "system_count": 1,
        "systems": [
            {
                "slug": "testco",
                "name": "TestCo",
                "tier": "A",
                "category": "saas",
                "asset_count": 1,
                "assets": [
                    {
                        "slug": "testco-btn-001",
                        "class": "buttons",
                        "kind": "atom",
                        "path": "assets/atoms/buttons/testco-btn-001",
                        "tokens_path": "assets/atoms/buttons/testco-btn-001/tokens.css",
                        "tldr": "Primary button.",
                        "patterns": ["primary-square"],
                        "mood": ["confident"],
                        "applicable_to": ["saas"],
                        "tags": ["buttons"],
                        "provenance_score": "A",
                    }
                ],
            }
        ],
    }
    (root / "corpus.json").write_text(json.dumps(corpus), encoding="utf-8")

    asset_dir = root / "assets" / "atoms" / "buttons" / "testco-btn-001"
    asset_dir.mkdir(parents=True)

    (asset_dir / "tokens.css").write_text(
        ":root { --ds-bg: #ffffff; --ds-text: #111111; }\n",
        encoding="utf-8",
    )

    # asset.html: includes all three interactive states and comment forms with
    # a fake 'brand name' that must not survive into the stored component bytes.
    html = """\
<!-- Inspired by: TestCo Brand Name (this comment must be stripped) -->
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<style>
/* Proprietary TestCo palette - do not distribute */
.btn { background: var(--ds-bg); color: var(--ds-text); }
.btn:hover { background: #ddd; }
.btn:focus-visible { outline: 2px solid blue; }
.btn[disabled] { opacity: 0.5; cursor: not-allowed; }
</style>
</head>
<body>
<!-- Internal label: TestCo brand - strip me -->
<button class="btn">Submit</button>
<button class="btn" disabled>Disabled</button>
</body>
</html>
"""
    (asset_dir / "asset.html").write_text(html, encoding="utf-8")


def test_persist_path_writes_asset_components_row(
    session: Session, tmp_path: Path
) -> None:
    """apply_seed writes one asset_components row when asset.html is present.

    Primary acceptance-criteria test for issue #2: verifies that after seeding
    an asset whose asset.html carries hover/focus/disabled CSS, the DB holds
    exactly one asset_components row with the correct state inventory, cleaned
    CSS and HTML, and a DRL-relative (not absolute) source_asset_path.
    """
    _write_component_drl(tmp_path)
    user_id = _seed_user_for_component_test(session)
    session.commit()

    storage = _FakeStorageForComponentTest()
    apply_seed(
        iter_assets(load_corpus(tmp_path)),
        tmp_path,
        session,
        storage,
        seed_user_id=user_id,
        batch_size=DEFAULT_BATCH_SIZE,
    )

    components = session.execute(select(AssetComponent)).scalars().all()
    assert len(components) == 1, (
        f"Expected exactly one asset_components row; got {len(components)}"
    )
    comp = components[0]

    # Correct fragment key and state inventory.
    assert comp.fragment_key == "default"
    assert "hover" in comp.states_present
    assert "focus" in comp.states_present
    assert "disabled" in comp.states_present
    assert "rest" in comp.states_present

    # Interactive CSS rules survived the extraction.
    assert ":hover" in comp.component_css
    assert ":focus-visible" in comp.component_css
    assert "[disabled]" in comp.component_css

    # Markup survived extraction.
    assert 'class="btn"' in comp.component_html

    # Comment stripping: neither comment form may survive into the DB.
    assert "<!--" not in comp.component_html, "HTML comment bytes must not reach the DB"
    assert "/*" not in comp.component_css, "CSS comment bytes must not reach the DB"
    assert "TestCo Brand Name" not in comp.component_html
    assert "Proprietary TestCo palette" not in comp.component_css

    # source_asset_path must be DRL-relative, never an absolute OS path.
    assert comp.source_asset_path == "assets/atoms/buttons/testco-btn-001"
    assert not comp.source_asset_path.startswith("/")
    assert "\\" not in comp.source_asset_path


def test_persist_path_component_row_is_idempotent(
    session: Session, tmp_path: Path
) -> None:
    """Re-running apply_seed updates the component row in place; no duplicates created.

    Exercises the upsert path in insert_asset_component (#1): after two seed
    runs on the same asset, exactly one asset_components row should exist.
    """
    _write_component_drl(tmp_path)
    user_id = _seed_user_for_component_test(session)
    session.commit()

    storage = _FakeStorageForComponentTest()
    corpus = load_corpus(tmp_path)

    apply_seed(
        iter_assets(corpus),
        tmp_path,
        session,
        storage,
        seed_user_id=user_id,
        batch_size=DEFAULT_BATCH_SIZE,
    )
    first_count = len(session.execute(select(AssetComponent)).scalars().all())
    assert first_count == 1

    apply_seed(
        iter_assets(corpus),
        tmp_path,
        session,
        storage,
        seed_user_id=user_id,
        batch_size=DEFAULT_BATCH_SIZE,
    )
    second_count = len(session.execute(select(AssetComponent)).scalars().all())
    assert second_count == first_count, (
        f"Re-seed duplicated asset_components rows: {first_count} -> {second_count}"
    )


def test_persist_path_skips_gracefully_when_no_asset_html(
    session: Session, tmp_path: Path
) -> None:
    """apply_seed does not crash when asset.html is absent; other rows are still written.

    A missing asset.html is a log-and-skip; the Extraction and AssetVersion
    rows for that asset are created normally. No asset_components row is written.
    """
    corpus_data = {
        "schema_version": 1,
        "generated": "2026-06-15",
        "asset_count": 1,
        "system_count": 1,
        "systems": [
            {
                "slug": "nohtmlco",
                "name": "NoHtmlCo",
                "tier": "B",
                "category": "saas",
                "asset_count": 1,
                "assets": [
                    {
                        "slug": "nohtmlco-btn-001",
                        "class": "buttons",
                        "kind": "atom",
                        "path": "assets/atoms/buttons/nohtmlco-btn-001",
                        "tokens_path": "assets/atoms/buttons/nohtmlco-btn-001/tokens.css",
                        "tldr": "Button.",
                        "patterns": [],
                        "mood": [],
                        "applicable_to": [],
                        "tags": [],
                        "provenance_score": "B",
                    }
                ],
            }
        ],
    }
    (tmp_path / "corpus.json").write_text(json.dumps(corpus_data), encoding="utf-8")
    asset_dir = tmp_path / "assets" / "atoms" / "buttons" / "nohtmlco-btn-001"
    asset_dir.mkdir(parents=True)
    (asset_dir / "tokens.css").write_text(":root { --ds-bg: #fff; }\n", encoding="utf-8")
    # No asset.html written here intentionally.

    user_id = _seed_user_for_component_test(session)
    session.commit()

    storage = _FakeStorageForComponentTest()
    counts = apply_seed(
        iter_assets(load_corpus(tmp_path)),
        tmp_path,
        session,
        storage,
        seed_user_id=user_id,
        batch_size=DEFAULT_BATCH_SIZE,
    )

    # The extraction row is still created despite the missing asset.html.
    assert counts["inserted"] == 1
    assert counts["skipped"] == 0
    extractions = session.execute(select(Extraction)).scalars().all()
    assert len(extractions) == 1

    # No asset_components row - the missing asset.html was gracefully skipped.
    components = session.execute(select(AssetComponent)).scalars().all()
    assert len(components) == 0

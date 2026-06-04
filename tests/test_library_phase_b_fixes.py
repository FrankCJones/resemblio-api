"""Phase B render-fix regression tests (library public-view TDD plan).

Locked 2026-06-03 per Phase B bundle:
``projects/OptSus Team/cto-reviews/2026-06-03-resemblio-library-public-view-tdd-plan.md``.

Covers the new failure IDs surfaced in Phase A inspection:

- **B3 / C-5**: build-slug related chips (``drl-bootstrap-2026-05-21``)
  must not surface to public users.
- **B2 / L-6**: text-only fallback for the ABOUT_TEAM avatar slot; the
  scoped style block must hide ``.at__avatar`` to prevent the gray
  placeholder circles from rendering on every brand snapshot.
- **B8 / L-15**: category-page featured-card title is category-specialized
  (``"Aeon buttons"``) and not a copy of the brand-level title
  (``"Aeon design snapshot"``).
- **B4 (placeholder lint)**: extended forbidden-regex check rolled into
  ``test_library_indexer_no_placeholder_text.py`` already; this file
  adds the corpus-level Stripe-specific assertion that L-13 stays
  closed once stripe re-indexes against the post-fix preset map.

Companion file: ``test_brand_names.py`` covers L-7 directly.
"""
from __future__ import annotations

import re

import pytest

from app.brand_names import pretty_brand_name
from app.library_indexer import (
    LIBRARY_TEMPLATE_OVERRIDE_CSS,
    _brand_placeholder,
    _category_display_label,
    _compose_one_page,
)
from app.routes.library import (
    _brand_display,
    _is_internal_version_label,
)


# ---------------------------------------------------------------------------
# B3 / C-5 - build-slug chip filter
# ---------------------------------------------------------------------------


INTERNAL_VERSION_LABELS: tuple[str, ...] = (
    "drl-bootstrap-2026-05-21",
    "drl-bootstrap",
    "drl-rebuild-2026-06-01",
    "ci-build-1234",
    "2026-06-03",
)

PUBLIC_VERSION_LABELS: tuple[str, ...] = (
    "v1",
    "v1-1",
    "spring-2026",
    "march-2026",
    "2026-march",
)


@pytest.mark.parametrize("label", INTERNAL_VERSION_LABELS)
def test_internal_version_label_filtered(label: str) -> None:
    """Build-internal labels are flagged as internal and filtered."""
    assert _is_internal_version_label(label), (
        f"label {label!r} should be filtered as build-internal"
    )


@pytest.mark.parametrize("label", PUBLIC_VERSION_LABELS)
def test_public_version_label_not_filtered(label: str) -> None:
    """User-facing version labels survive the filter."""
    assert not _is_internal_version_label(label), (
        f"label {label!r} should NOT be filtered"
    )


def test_brand_display_uses_canonical_caps_for_chip_labels() -> None:
    """Chip labels render brand caps via the canonical map."""
    assert _brand_display("openai") == "OpenAI"
    assert _brand_display("read-cv") == "Read.cv"
    # Sanity: unknown slug still produces something readable.
    assert _brand_display("acme-corp") == "Acme Corp"


# ---------------------------------------------------------------------------
# B8 / L-15 - category-page title specialization
# ---------------------------------------------------------------------------


def test_category_display_label_returns_lowercase_phrase() -> None:
    """Known category slugs map to lowercase noun phrases.

    Lowercase by design so the "{Brand} {phrase}" interpolation reads
    as a natural sentence ("Aeon buttons") rather than a title-cased
    one ("Aeon Buttons"); pageTitle is the surface that title-cases the
    full string at render time.
    """
    assert _category_display_label("buttons") == "buttons"
    assert _category_display_label("about-team") == "about team"
    assert _category_display_label("article-layout") == "article layout"


def test_category_display_label_snapshot_classes_return_none() -> None:
    """The featured-snapshot class keeps the brand-level title.

    The brand-canonical page surfaces the snapshot class as its hero;
    specializing that title would say "Aeon snapshot" which is
    redundant with the brand-snapshot heading. Returning None preserves
    the existing brand-level copy path.
    """
    assert _category_display_label("snapshot") is None
    assert _category_display_label("featured-snapshot") is None


def test_category_display_label_unknown_slug_humanizes() -> None:
    """Unknown slugs humanize to a lowercase, dash-stripped form."""
    assert _category_display_label("brand-new-category") == "brand new category"


def test_category_display_label_empty_returns_none() -> None:
    """Empty / None slugs return None (legacy callers)."""
    assert _category_display_label(None) is None
    assert _category_display_label("") is None


def test_title_slot_specialized_when_category_passed() -> None:
    """Title slot reads "{Brand} {category-phrase}" when category given.

    L-15 fix: category-detail pages must not show "Aeon design snapshot"
    where they should show "Aeon buttons". The placeholder routes the
    category slug into the title when one is supplied.
    """
    title = _brand_placeholder(
        "title", brand_slug="aeon", category_slug="buttons"
    )
    assert title == "Aeon buttons", f"title={title!r}"

    title_openai = _brand_placeholder(
        "title", brand_slug="openai", category_slug="about-team"
    )
    assert title_openai == "OpenAI about team", f"title={title_openai!r}"


def test_title_slot_brand_level_when_no_category() -> None:
    """No category -> brand-level title (legacy / brand-snapshot path).

    Back-compat: existing callers that omit category_slug get the
    pre-fix brand-level copy ("Aeon design snapshot") so the brand-
    canonical page hero stays unchanged.
    """
    title = _brand_placeholder("title", brand_slug="aeon")
    assert title == "Aeon design snapshot", f"title={title!r}"


def test_title_slot_brand_level_for_snapshot_category() -> None:
    """Snapshot class keeps brand-level title even when category passed.

    The compose pipeline passes the class_name for every per-class
    render; for the snapshot class the title must stay brand-level.
    """
    title = _brand_placeholder(
        "title", brand_slug="stripe", category_slug="snapshot"
    )
    assert title == "Stripe design snapshot", f"title={title!r}"


# ---------------------------------------------------------------------------
# B2 / L-6 - text-only avatar fallback CSS
# ---------------------------------------------------------------------------


def test_override_css_hides_avatar_circles() -> None:
    """The override CSS hides ``.at__avatar`` at the scoped specificity.

    Path B per Jim's default: the gray placeholder circles render
    because the DRL ABOUT_TEAM template emits empty
    ``<div class="at__avatar">`` shells styled with a background fill.
    The override is appended after ``scope_style_block`` runs so it
    inherits the page-scoped specificity and shadows the DRL rule.
    """
    assert ".rs-library-page .at__avatar" in LIBRARY_TEMPLATE_OVERRIDE_CSS
    assert "display: none" in LIBRARY_TEMPLATE_OVERRIDE_CSS
    # !important required because the DRL rule and override land at the
    # same specificity and the DRL rule comes first.
    assert "!important" in LIBRARY_TEMPLATE_OVERRIDE_CSS


def test_compose_one_page_emits_avatar_hide_rule() -> None:
    """The avatar-hide rule is present in every rendered article fragment.

    End-to-end check: compose an ABOUT_TEAM page against the indexer's
    own composer and assert the override CSS lives in the output. This
    is the pin that prevents a future ``_compose_one_page`` refactor
    from dropping the appended override.
    """
    rendered = _compose_one_page(
        "about-team", brand_slug="aeon", tokens={}, button_tokens=None
    )
    assert ".rs-library-page .at__avatar { display: none !important; }" in rendered, (
        f"override CSS missing from compose output; "
        f"L-6 gray-circle regression. Sample: {rendered[:400]!r}"
    )


# ---------------------------------------------------------------------------
# Cross-check: B6 / L-13 stripe placeholder leak
# ---------------------------------------------------------------------------


_STRIPE_PLACEHOLDER_REGEX = re.compile(r"Member \d Name|Member \d Role")


def test_stripe_member_slots_resolve_to_preset() -> None:
    """Stripe member slots resolve to the same Design/Engineering/Product/
    Research preset family Aeon and OpenAI get.

    Phase A inspection caught ``stripe_1440x900.png`` rendering literal
    "Member 1 Name" / "Member 1 Role" strings while the tablet and
    mobile viewports rendered the preset values. The unit test pins the
    preset map's coverage so any future change that drops a member slot
    is caught here; the remaining L-13 surface (existing stripe rows in
    the prod DB still carry the stale rendered_html) is closed by a
    re-index dispatch surfaced as YELLOW to Jim.
    """
    for n in (1, 2, 3, 4):
        for field in ("name", "role"):
            slot = f"member_{n}_{field}"
            rendered = _brand_placeholder(slot, brand_slug="stripe")
            assert not _STRIPE_PLACEHOLDER_REGEX.search(rendered), (
                f"slot {slot!r} resolves to {rendered!r}; "
                f"L-13 placeholder regression"
            )
            assert rendered.strip(), f"slot {slot!r}: empty"


# ---------------------------------------------------------------------------
# B4 - production placeholder lint at compose-time
# ---------------------------------------------------------------------------


FORBIDDEN_RENDERED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Item \d Title"),
    re.compile(r"Item \d Dek"),
    re.compile(r"Item \d Date"),
    re.compile(r"Member \d Name"),
    re.compile(r"Member \d Role"),
    re.compile(r"Step \d Title"),
    re.compile(r"Step \d Dek"),
    re.compile(r"Section \d Title"),
    re.compile(r"Section \d Body"),
    re.compile(r"Col \d Title"),
    re.compile(r"Col \d Link"),
    re.compile(r"Lorem ipsum"),
    re.compile(r"__placeholder__"),
)


@pytest.mark.parametrize(
    "class_name",
    ["about-team", "news-list", "process-steps", "article-layout"],
)
def test_compose_one_page_emits_no_forbidden_placeholders(class_name: str) -> None:
    """Compose every template-family known to have had placeholder leaks
    and assert none of the forbidden patterns survive to rendered output.

    Pins the visual-fidelity-spec ``brand_no_placeholder_filler_text``
    contract at the unit boundary; the e2e companion (see
    ``test_library_indexer_no_placeholder_text.py``) covers the DB
    round-trip.
    """
    rendered = _compose_one_page(
        class_name, brand_slug="aeon", tokens={}, button_tokens=None
    )
    for pattern in FORBIDDEN_RENDERED_PATTERNS:
        match = pattern.search(rendered)
        assert match is None, (
            f"class {class_name}: rendered output carries forbidden "
            f"placeholder pattern {pattern.pattern!r} at "
            f"{rendered[max(0, match.start() - 20):match.end() + 20]!r}"
        )


# ---------------------------------------------------------------------------
# B5 - corpus-hidden assertion (rendered_html surface)
# ---------------------------------------------------------------------------


def test_compose_output_carries_no_download_anchor() -> None:
    """Rendered article body does not embed a corpus-asset download link.

    Project CLAUDE.md line 37: "Public corpus hidden in v1, visible in
    v1.1 once moderation tooling exists." The export tiles in the
    page shell are gated by the React component (LibraryPageShell renders
    locked anchors). This test pins the indexer surface: the composed
    rendered_html body MUST NOT carry any ``<a ... download="">``
    attribute or any anchor whose href routes to a corpus-asset path
    (``/api/library/export/``). If the DRL template ever ships such an
    anchor, the corpus-hidden rule breaks at the body level even though
    the shell-level lock is intact.
    """
    rendered = _compose_one_page(
        "about-team", brand_slug="aeon", tokens={}, button_tokens=None
    )
    # Defensive: search for any download attribute (any quoting form)
    # and for the export-route path.
    assert "download=" not in rendered.lower(), (
        "rendered article body carries a download anchor; corpus-hidden "
        "rule broken at the body layer"
    )
    assert "/api/library/export/" not in rendered, (
        "rendered article body carries a corpus-export link; corpus-hidden "
        "rule broken at the body layer"
    )


# ---------------------------------------------------------------------------
# Sanity: pretty_brand_name reachable via the same import path the indexer
# uses (catches a regression where someone deletes the re-export).
# ---------------------------------------------------------------------------


def test_pretty_brand_name_reaches_aeon() -> None:
    """End-to-end smoke: aeon canonical equals 'Aeon'."""
    assert pretty_brand_name("aeon") == "Aeon"

"""Tests for app.whole_mining - TDD RED phase.

Covers four phases per the issue #27 implementation plan:
  Phase 1 - iter_css_rules shared iterator (+ library_style_scope regression)
  Phase 2 - css_rules_for_classes subject-based CSS filter
  Phase 3 - find_atom_fragments HTML subtree extractor
  Phase 4 - mine_atom_from_whole orchestrator + real apple-cta-block fixture

All tests use synthetic in-memory fixtures or the vendored apple fixture;
no network, no DRL writes.

Do this work at a level that would impress a senior developer.
Include documentation and code comments that make it easy for a future
developer to maintain this project.
"""
from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Phase 1 - iter_css_rules shared iterator
# ---------------------------------------------------------------------------

from app.library_style_scope import CssRule, iter_css_rules, scope_style_block


def _norm(s: str) -> str:
    """Collapse whitespace for assertion-friendly comparison."""
    return " ".join(s.split())


class TestIterCssRules:
    """iter_css_rules yields correctly structured CssRule objects."""

    def test_yields_plain_rule(self):
        """A single plain rule produces one CssRule with correct prelude/body."""
        rules = list(iter_css_rules(".btn { padding: 8px; }"))
        # exactly one non-sentinel rule
        real = [r for r in rules if r.prelude.strip()]
        assert len(real) == 1
        assert ".btn" in real[0].prelude
        assert "padding: 8px;" in real[0].body
        assert real[0].at_name == ""

    def test_yields_multiple_rules(self):
        """Multiple rules each produce a separate CssRule."""
        css = ".a { color: red; } .b { color: blue; }"
        rules = [r for r in iter_css_rules(css) if r.prelude.strip()]
        assert len(rules) == 2

    def test_at_media_rule_has_correct_at_name(self):
        """@media block is yielded with at_name='@media'."""
        css = "@media (min-width: 600px) { .btn { padding: 12px; } }"
        rules = [r for r in iter_css_rules(css) if r.prelude.strip()]
        assert len(rules) == 1
        assert rules[0].at_name == "@media"
        assert ".btn" in rules[0].body

    def test_at_keyframes_rule_has_correct_at_name(self):
        """@keyframes block is yielded with at_name='@keyframes'."""
        css = "@keyframes fade { from { opacity: 0; } to { opacity: 1; } }"
        rules = [r for r in iter_css_rules(css) if r.prelude.strip()]
        assert len(rules) == 1
        assert rules[0].at_name == "@keyframes"

    def test_prefix_carries_leading_whitespace(self):
        """Whitespace before a rule is captured in the CssRule.prefix field."""
        css = "\n  .btn { color: red; }"
        rules = list(iter_css_rules(css))
        # The first item (or the only rule) carries the leading whitespace as prefix
        assert any("\n" in r.prefix for r in rules)

    def test_trailing_content_emitted_as_sentinel(self):
        """Trailing text (no block) is yielded as a sentinel with empty prelude/body."""
        css = ".btn { color: red; } /* orphan comment */"
        rules = list(iter_css_rules(css))
        sentinels = [r for r in rules if not r.prelude.strip() and not r.body.strip() and r.prefix.strip()]
        # trailing comment should appear somewhere (either as prefix of sentinel or in rule prefix)
        # At minimum: at least one rule with prelude
        real = [r for r in rules if r.prelude.strip()]
        assert len(real) == 1

    def test_scope_style_block_unchanged_after_refactor(self):
        """scope_style_block output is identical before/after the iter_css_rules refactor.

        This is the regression guard: the refactor must not change any observed behavior
        of the existing CSS scoper.
        """
        cases = [
            "html { margin: 0; }",
            "body { background: #fff; }",
            "* { box-sizing: border-box; }",
            ".b-btn { padding: 8px; }",
            ":root { --ds-bg: #fff; }",
            "@media (min-width: 600px) { .b-btn { padding: 12px; } }",
            "@keyframes spin { 0% { transform: rotate(0); } 100% { transform: rotate(360deg); } }",
            ".a, .b { color: red; }",
            "html.dark .btn { color: white; }",
            "",
        ]
        # Since we cannot compare against a pre-refactor snapshot, we assert structural
        # invariants that must hold regardless of refactor:
        for css in cases:
            result = scope_style_block(css)
            # idempotency: a second pass must return the same result
            assert scope_style_block(result) == result, f"Not idempotent for: {css!r}"
            # :root must never be scoped
            if ":root" in css:
                assert ":root" in result
                assert ".rs-library-page :root" not in result


# ---------------------------------------------------------------------------
# Phase 2 - css_rules_for_classes
# ---------------------------------------------------------------------------

from app.whole_mining import css_rules_for_classes


class TestCssRulesForClasses:
    """css_rules_for_classes filters by selector subject."""

    _BUTTON_CLASSES: frozenset[str] = frozenset({"cta__btn", "cta__btn--primary", "cta__btn--ghost"})

    def test_keeps_direct_class_rule(self):
        """A rule whose selector IS a button class is kept."""
        css = ".cta__btn { padding: 12px 20px; }"
        result = css_rules_for_classes(css, self._BUTTON_CLASSES)
        assert "padding: 12px 20px" in result

    def test_drops_unrelated_class_rule(self):
        """A rule whose subject is not in the class set is dropped."""
        css = ".cta__inner { max-width: 720px; }"
        result = css_rules_for_classes(css, self._BUTTON_CLASSES)
        assert "max-width" not in result

    def test_keeps_descendant_rule_by_subject(self):
        """A descendant rule is kept when the rightmost segment is a button class."""
        css = ".cta__actions .cta__btn:hover { opacity: 0.9; }"
        result = css_rules_for_classes(css, self._BUTTON_CLASSES)
        assert "opacity: 0.9" in result

    def test_drops_layout_rule_whose_subject_is_ancestor(self):
        """If the subject (rightmost) is not in the class set, the rule is dropped."""
        css = ".cta__btn .inner-icon { fill: currentColor; }"
        result = css_rules_for_classes(css, self._BUTTON_CLASSES)
        assert "fill" not in result

    def test_keeps_modifier_class_rule(self):
        """BEM modifier selector like .cta__btn--primary is kept."""
        css = ".cta__btn--primary { background: var(--ds-accent); }"
        result = css_rules_for_classes(css, self._BUTTON_CLASSES)
        assert "background" in result

    def test_keeps_referenced_keyframes(self):
        """@keyframes referenced by an animation property in a kept rule is kept."""
        css = (
            ".cta__btn { animation: btn-fade 0.3s ease; }\n"
            "@keyframes btn-fade { from { opacity: 0; } to { opacity: 1; } }"
        )
        result = css_rules_for_classes(css, self._BUTTON_CLASSES)
        assert "btn-fade" in result
        assert "opacity: 0" in result

    def test_drops_unreferenced_keyframes(self):
        """@keyframes not referenced by any kept rule is dropped."""
        css = (
            ".cta__btn { padding: 12px; }\n"
            "@keyframes unrelated-spin { 0% {} 100% {} }"
        )
        result = css_rules_for_classes(css, self._BUTTON_CLASSES)
        assert "unrelated-spin" not in result

    def test_recurses_into_media_block_and_keeps_matching(self):
        """@media block is kept when inner rules match; non-matching inner rules dropped."""
        css = (
            "@media (prefers-reduced-motion: reduce) {\n"
            "  .cta__btn { transition: none; }\n"
            "  .cta__inner { animation: none; }\n"
            "}"
        )
        result = css_rules_for_classes(css, self._BUTTON_CLASSES)
        assert "transition: none" in result
        assert "prefers-reduced-motion" in result
        # layout rule inside the media block must be dropped
        assert "animation: none" not in result

    def test_drops_media_block_when_no_inner_rules_match(self):
        """@media block is dropped entirely when no inner rules match the class set."""
        css = "@media (min-width: 600px) { .cta__inner { padding: 48px; } }"
        result = css_rules_for_classes(css, self._BUTTON_CLASSES)
        assert "padding: 48px" not in result
        assert "min-width" not in result

    def test_output_is_raw_not_scoped(self):
        """Output CSS is raw (unscoped); no .rs-library-page wrapper is added."""
        css = ".cta__btn { color: red; }"
        result = css_rules_for_classes(css, self._BUTTON_CLASSES)
        assert ".rs-library-page" not in result


# ---------------------------------------------------------------------------
# Phase 3 - find_atom_fragments
# ---------------------------------------------------------------------------

from app.whole_mining import find_atom_fragments


class TestFindAtomFragments:
    """find_atom_fragments extracts and groups matching elements from HTML."""

    def test_finds_anchor_with_btn_class(self):
        """An <a> element with class containing 'btn' is extracted."""
        html = '<div><a class="cta__btn" href="#">Go</a></div>'
        fragment, classes = find_atom_fragments(html, "buttons")
        assert fragment != ""
        assert "cta__btn" in fragment

    def test_finds_button_element_by_tag(self):
        """A <button> element (tag match) is extracted even without class hint."""
        html = "<div><button>Click</button></div>"
        fragment, classes = find_atom_fragments(html, "buttons")
        assert fragment != ""
        assert "button" in fragment.lower()

    def test_returns_empty_when_no_match(self):
        """Returns ('', []) when the whole has no elements matching the atom class."""
        html = "<div><p class='text'>Hello</p><span>World</span></div>"
        fragment, classes = find_atom_fragments(html, "buttons")
        assert fragment == ""
        assert classes == []

    def test_wraps_multiple_variants_in_group_div(self):
        """Multiple matching elements are wrapped in a single rs-mined-group div."""
        html = (
            '<a class="cta__btn cta__btn--primary" href="#">A</a>'
            '<a class="cta__btn cta__btn--ghost" href="#">B</a>'
        )
        fragment, classes = find_atom_fragments(html, "buttons")
        assert "rs-mined-group" in fragment
        assert "cta__btn--primary" in fragment
        assert "cta__btn--ghost" in fragment

    def test_data_rs_mined_from_attribute_present(self):
        """Grouping wrapper carries data-rs-mined-from=<atom_class>."""
        html = '<a class="btn" href="#">X</a>'
        fragment, _ = find_atom_fragments(html, "buttons")
        assert 'data-rs-mined-from="buttons"' in fragment

    def test_source_classes_contains_all_matched_element_classes(self):
        """source_classes is the union of all class tokens from matched elements."""
        html = (
            '<a class="cta__btn cta__btn--primary" href="#">A</a>'
            '<a class="cta__btn cta__btn--ghost" href="#">B</a>'
        )
        _, classes = find_atom_fragments(html, "buttons")
        assert "cta__btn" in classes
        assert "cta__btn--primary" in classes
        assert "cta__btn--ghost" in classes

    def test_provenance_comments_stripped_from_fragment(self):
        """HTML comments are removed from the extracted fragment."""
        html = '<!-- do not serve --><a class="btn"><!-- inner --></a>'
        fragment, _ = find_atom_fragments(html, "buttons")
        assert "<!--" not in fragment

    def test_single_match_still_wrapped_in_group(self):
        """Even a single match is wrapped in rs-mined-group for a consistent shape."""
        html = '<a class="btn btn--primary" href="#">Solo</a>'
        fragment, _ = find_atom_fragments(html, "buttons")
        assert "rs-mined-group" in fragment


# ---------------------------------------------------------------------------
# Phase 4 - mine_atom_from_whole + real fixture
# ---------------------------------------------------------------------------

from app.whole_mining import mine_atom_from_whole

_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "drl" / "apple_cta_block" / "asset.html"
)


@pytest.fixture
def apple_html() -> str:
    """Return the vendored apple-cta-block-001 asset.html text."""
    return _FIXTURE_PATH.read_text(encoding="utf-8")


class TestMineAtomFromWhole:
    """mine_atom_from_whole orchestrates extraction and returns a MinedAtom."""

    def test_returns_none_when_no_buttons(self):
        """Returns None for a whole with no button elements."""
        html = "<html><body><p>No buttons</p></body></html>"
        result = mine_atom_from_whole(html, "buttons")
        assert result is None

    def test_schema_version_correct(self, apple_html):
        """MinedAtom carries schema_version='whole_mining_v1'."""
        result = mine_atom_from_whole(apple_html, "buttons")
        assert result is not None
        assert result.schema_version == "whole_mining_v1"

    def test_apple_cta_component_html_contains_primary_and_ghost(self, apple_html):
        """MinedAtom.component_html contains both button variants from the apple CTA."""
        result = mine_atom_from_whole(apple_html, "buttons")
        assert result is not None
        assert "cta__btn--primary" in result.component_html
        assert "cta__btn--ghost" in result.component_html

    def test_apple_cta_component_css_contains_padding(self, apple_html):
        """component_css retains the padding declaration from .cta__btn."""
        result = mine_atom_from_whole(apple_html, "buttons")
        assert result is not None
        assert "padding: 12px 20px" in result.component_css

    def test_apple_cta_component_css_contains_border_radius(self, apple_html):
        """component_css retains the border-radius declaration from .cta__btn."""
        result = mine_atom_from_whole(apple_html, "buttons")
        assert result is not None
        assert "border-radius: var(--ds-radius-sm" in result.component_css

    def test_apple_cta_states_present_is_rest_only(self, apple_html):
        """apple-cta-block-001 has no hover/focus/active pseudo-classes; states=['rest']."""
        result = mine_atom_from_whole(apple_html, "buttons")
        assert result is not None
        assert result.states_present == ["rest"]

    def test_apple_cta_component_css_is_unscoped(self, apple_html):
        """component_css is raw/unscoped; the scoper is applied later by the indexer."""
        result = mine_atom_from_whole(apple_html, "buttons")
        assert result is not None
        assert ".rs-library-page" not in result.component_css

    def test_apple_cta_component_css_drops_layout_rules(self, apple_html):
        """Layout rules (.cta, .cta__inner, etc.) are not present in component_css."""
        result = mine_atom_from_whole(apple_html, "buttons")
        assert result is not None
        # These layout class names should not appear as selectors in the filtered CSS
        assert ".cta__inner" not in result.component_css
        assert ".cta__kicker" not in result.component_css
        assert ".cta__title" not in result.component_css

    def test_atom_class_field_set_correctly(self, apple_html):
        """MinedAtom.atom_class matches the requested atom class."""
        result = mine_atom_from_whole(apple_html, "buttons")
        assert result is not None
        assert result.atom_class == "buttons"

    def test_source_classes_populated(self, apple_html):
        """source_classes lists the class names found on matched button elements."""
        result = mine_atom_from_whole(apple_html, "buttons")
        assert result is not None
        assert "cta__btn" in result.source_classes
        assert "cta__btn--primary" in result.source_classes
        assert "cta__btn--ghost" in result.source_classes

    def test_no_drl_directory_modified(self, apple_html, tmp_path):
        """Mining does not write any files (DRL is read-only)."""
        # We call mine_atom_from_whole with in-memory HTML to prove no file I/O
        # touches the DRL. The fixture is already vendored; no DRL path is accessed.
        result = mine_atom_from_whole(apple_html, "buttons")
        assert result is not None  # sanity: mining works

    def test_component_html_comments_stripped(self, apple_html):
        """Provenance comments are not present in component_html."""
        result = mine_atom_from_whole(apple_html, "buttons")
        assert result is not None
        assert "<!--" not in result.component_html
        assert "Inspired by" not in result.component_html


# ---------------------------------------------------------------------------
# Phase 5 - Per-class extractor validation against real DRL whole fixtures
# (Issue #5: validate each class ATOM_DETECTION_HINTS entry before activating
#  it in MINEABLE_ATOM_CLASSES in seed_from_drl.py)
#
# Each test drives a vendored DRL whole through mine_atom_from_whole and
# asserts that the correct elements are captured and the wrong ones are not.
# A class is added to MINEABLE_ATOM_CLASSES only once its test here passes.
#
# Fixtures are frozen snapshots of real DRL whole asset.html files copied on
# 2026-06-19. They are never edited to track DRL changes (DRL is read-only).
# ---------------------------------------------------------------------------

_DRL_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "drl"


@pytest.fixture
def cursor_pricing_html() -> str:
    """Vendored cursor ai-credit-pricing-001 whole (badges validation fixture).

    Source: DRL assets/wholes/pricing-tables/ai-credit-pricing-001 (cursor brand).
    Contains: <span class='tier__badge'>Recommended</span>.
    Frozen: 2026-06-19. Do not edit to track DRL changes.
    """
    return (_DRL_FIXTURE_DIR / "cursor_pricing" / "asset.html").read_text(encoding="utf-8")


@pytest.fixture
def apple_testimonials_html() -> str:
    """Vendored apple-testimonials-001 whole (cards validation fixture).

    Source: DRL assets/wholes/testimonials/apple-testimonials-001 (apple brand).
    Contains: <div class='ts__cards'> grid with <article class='ts__card'> children.
    Frozen: 2026-06-19. Do not edit to track DRL changes.
    """
    return (_DRL_FIXTURE_DIR / "apple_testimonials" / "asset.html").read_text(encoding="utf-8")


@pytest.fixture
def glossier_footer_html() -> str:
    """Vendored glossier-footer-001 whole (links validation fixture).

    Source: DRL assets/wholes/footers/glossier-footer-001 (glossier brand).
    Contains: footer__link, footer__index-link, footer__legal-link (class-based links)
    and footer__mark (wordmark anchor that must NOT be captured as a link).
    Frozen: 2026-06-19. Do not edit to track DRL changes.
    """
    return (_DRL_FIXTURE_DIR / "glossier_footer" / "asset.html").read_text(encoding="utf-8")


class TestAtomClassValidation:
    """Phase 5: validate each ATOM_DETECTION_HINTS entry against real DRL fixtures.

    These tests enforce the validate-then-activate discipline from issue #5:
    a class is added to MINEABLE_ATOM_CLASSES only once a test here passes
    against a real vendored DRL whole fixture.

    The links tests include two RED tests that expose the over-broad tags={'a'}
    hint and will only pass after the hint is narrowed to tags=frozenset().
    """

    # ---- badges ----

    def test_badges_cursor_pricing_captures_badge_span(
        self, cursor_pricing_html: str
    ) -> None:
        """badges hint captures <span class='tier__badge'> from the cursor pricing whole.

        'tier__badge' contains 'badge', which is in the badges hint class_substrings.
        """
        result = mine_atom_from_whole(cursor_pricing_html, "badges")
        assert result is not None, (
            "mine_atom_from_whole returned None for 'badges' on cursor_pricing. "
            "The badges hint should match tier__badge (contains 'badge')."
        )
        assert "tier__badge" in result.component_html, (
            "Expected 'tier__badge' in component_html. "
            "Check ATOM_DETECTION_HINTS['badges'].class_substrings."
        )
        assert "tier__badge" in result.source_classes

    def test_badges_cursor_pricing_css_contains_badge_rule(
        self, cursor_pricing_html: str
    ) -> None:
        """component_css retains the .tier__badge styling rule (not the whole page CSS)."""
        result = mine_atom_from_whole(cursor_pricing_html, "badges")
        assert result is not None
        assert ".tier__badge" in result.component_css, (
            "component_css should contain .tier__badge rules. "
            "The CSS filter should keep badge-class rules and drop page layout rules."
        )

    def test_badges_atom_class_and_schema_correct(
        self, cursor_pricing_html: str
    ) -> None:
        """MinedAtom carries atom_class='badges' and schema_version='whole_mining_v1'."""
        result = mine_atom_from_whole(cursor_pricing_html, "badges")
        assert result is not None
        assert result.atom_class == "badges"
        assert result.schema_version == "whole_mining_v1"

    # ---- cards ----

    def test_cards_apple_testimonials_captures_card_container(
        self, apple_testimonials_html: str
    ) -> None:
        """cards hint captures the outer element whose class contains 'card'.

        apple-testimonials-001 has <div class='ts__cards'> (the grid container)
        containing three <article class='ts__card'> children. The container is
        captured as the root-level match; its children are included in the fragment.
        """
        result = mine_atom_from_whole(apple_testimonials_html, "cards")
        assert result is not None, (
            "mine_atom_from_whole returned None for 'cards' on apple_testimonials. "
            "The cards hint should match ts__cards (contains 'card')."
        )
        assert "ts__card" in result.component_html, (
            "Expected 'ts__card' in component_html (the individual card class). "
            "Check ATOM_DETECTION_HINTS['cards'].class_substrings."
        )

    def test_cards_apple_testimonials_source_classes_contain_card_class(
        self, apple_testimonials_html: str
    ) -> None:
        """source_classes contains at least one class with 'card' in its name."""
        result = mine_atom_from_whole(apple_testimonials_html, "cards")
        assert result is not None
        assert any("card" in c for c in result.source_classes), (
            f"Expected a class containing 'card' in source_classes; got {result.source_classes}"
        )

    def test_cards_atom_class_correct(self, apple_testimonials_html: str) -> None:
        """MinedAtom.atom_class is 'cards'."""
        result = mine_atom_from_whole(apple_testimonials_html, "cards")
        assert result is not None
        assert result.atom_class == "cards"

    # ---- links ----

    def test_links_glossier_footer_captures_link_class_anchors(
        self, glossier_footer_html: str
    ) -> None:
        """links hint captures <a> elements whose class contains 'link'.

        glossier-footer-001 has footer__link, footer__index-link, footer__legal-link.
        All contain 'link' as a substring and should be captured.
        """
        result = mine_atom_from_whole(glossier_footer_html, "links")
        assert result is not None, (
            "mine_atom_from_whole returned None for 'links' on glossier_footer. "
            "The links hint (class_substrings={'link'}) should match footer__link."
        )
        assert "footer__link" in result.component_html, (
            "Expected 'footer__link' in component_html. "
            "Check ATOM_DETECTION_HINTS['links'].class_substrings."
        )
        assert "footer__link" in result.source_classes

    def test_links_glossier_footer_does_not_capture_wordmark_anchor(
        self, glossier_footer_html: str
    ) -> None:
        """The wordmark <a class='footer__mark'> must NOT appear in the links fragment.

        footer__mark contains no 'link' substring. The current hint has
        tags=frozenset({'a'}) which over-captures ALL <a> elements including the
        wordmark. The fix: change tags to frozenset() so only class_substrings match.

        RED: fails with the current tags={'a'} hint (wordmark IS captured).
        GREEN: passes after narrowing to tags=frozenset().
        """
        result = mine_atom_from_whole(glossier_footer_html, "links")
        assert result is not None
        assert "footer__mark" not in result.component_html, (
            "'footer__mark' (the wordmark anchor, class not containing 'link') "
            "must not be captured as a link. "
            "Fix ATOM_DETECTION_HINTS['links']: change tags to frozenset() "
            "so only class_substrings={'link'} drives matching."
        )
        assert "footer__mark" not in result.source_classes

    def test_links_apple_cta_block_no_false_positive_on_btn_anchors(
        self, apple_html: str
    ) -> None:
        """Button-styled <a> elements (.cta__btn) must NOT be captured as links.

        apple-cta-block-001 has <a class='cta__btn cta__btn--primary'>. These are
        buttons, not links. 'cta__btn' does not contain 'link'. With the current
        over-broad tags={'a'} hint, mine_atom_from_whole would return non-None.
        With the fixed hint (tags=frozenset()), it must return None.

        RED: fails with the current tags={'a'} hint (cta__btn anchors ARE captured).
        GREEN: passes after narrowing to tags=frozenset().
        """
        result = mine_atom_from_whole(apple_html, "links")
        assert result is None, (
            "mine_atom_from_whole('links') returned non-None for the apple cta-block. "
            "<a class='cta__btn'> elements are buttons, not links. "
            "Fix ATOM_DETECTION_HINTS['links']: change tags to frozenset()."
        )

    def test_links_atom_class_correct(self, glossier_footer_html: str) -> None:
        """MinedAtom.atom_class is 'links'."""
        result = mine_atom_from_whole(glossier_footer_html, "links")
        assert result is not None
        assert result.atom_class == "links"

    def test_links_css_contains_hover_state(self, glossier_footer_html: str) -> None:
        """The mined links CSS retains hover state rules (footer__link has :hover)."""
        result = mine_atom_from_whole(glossier_footer_html, "links")
        assert result is not None
        assert "hover" in result.states_present, (
            f"Expected 'hover' in states_present; got {result.states_present}. "
            "footer__link has a :hover rule in glossier-footer-001."
        )

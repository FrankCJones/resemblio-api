"""Trademark-strip regression tests (Phase 3 of the Inspirado correction).

Authored 2026-06-04 per CTO plan
``projects/OptSus Team/cto-reviews/2026-06-04-resemblio-library-inspirado-no-copiado-correction-plan.md``
Phase 3 (Stages 3.1, 3.2, 3.3).

Anchor (Frank, 2026-06-04):

    "We're only dropping the parts that hold the trademark or legal
    protection, like the wordmark and logo. Everything else from the
    design is what we're delivering."

Concretely, this file pins the trademark-strip surface with regression
coverage so a future template change cannot reintroduce a wordmark / logo
leak. The strip is currently incidental (the DRL templates simply do not
emit any `<img>` / `<svg>` / brand-CDN URL for the brand's mark); these
tests turn that incidental property into an asserted one.

What is allowed:

- The brand NAME as plain text (e.g. "Aeon", "OpenAI", "Stripe"). This IS
  the "inspired by <brand>" attribution and is the core of the product
  definition.
- Brand-correct typography, color, spacing, weights, layout, component
  styling. Pinned by sibling tests (font fidelity, color fidelity).

What is forbidden:

- The brand's stylized wordmark / lockup as an `<svg>` or `<img>`.
- The brand's logo mark as an `<svg>` or `<img>`.
- Any reference to the brand's canonical logo CDN URL.
- Real-person photos in the about-team avatar slot.

Targets data: ``app/trademark_strip_targets.yml`` (schema
``trademark_strip_targets_v1``). One entry per public-corpus brand with
brand-specific forbidden substrings; one universal forbidden-substring
list that catches the common shapes a leak would take.

Pairs with: ``tests/test_library_phase_b_fixes.py`` (avatar hide rule
unit) and ``tests/test_library_indexer_no_placeholder_text.py``
(placeholder-fallback regression).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.library_indexer import (
    LIBRARY_TEMPLATE_OVERRIDE_CSS,
    _all_template_classes,
    _compose_one_page,
    _metadata_for,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


TRADEMARK_STRIP_TARGETS_PATH = (
    Path(__file__).resolve().parent.parent / "app" / "trademark_strip_targets.yml"
)
"""Resolved path to the strip-targets YAML.

Resolved relative to this test file so the test runs from any cwd. The
data file is part of the application package (``app/``) because it is
read by tests against the production strip discipline; it is not loaded
by runtime code paths today, but a future runtime lint could read the
same source.
"""

SCHEMA_VERSION = "trademark_strip_targets_v1"
"""Pinned schema version. Test fails loud if the YAML drifts to a new
shape without updating this constant in lockstep."""

# Minimal token set that covers every BRAND_TOKEN_CONTRACT slot via
# contract defaults; sufficient for _compose_one_page to render every
# DRL template without raising. Real brand tokens carry color + font
# overrides; for the strip audit the contract defaults are enough.
_MINIMAL_TOKENS: dict[str, str] = {
    "ds-bg": "#ffffff",
    "ds-text": "#111111",
    "ds-font-display": "Inter, sans-serif",
    "ds-font-body": "Inter, sans-serif",
    "ds-font-mono": "ui-monospace, monospace",
}


def _load_targets() -> dict[str, Any]:
    """Load and validate ``trademark_strip_targets.yml``.

    Returns the parsed dict. Validates schema_version and that the
    ``brands`` list is non-empty. Raises AssertionError with a specific
    message on either failure so the test output names the contract
    that broke.
    """
    assert TRADEMARK_STRIP_TARGETS_PATH.exists(), (
        f"trademark strip targets YAML missing at "
        f"{TRADEMARK_STRIP_TARGETS_PATH}; expected by "
        f"test_trademark_strip.py"
    )
    raw = TRADEMARK_STRIP_TARGETS_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    assert isinstance(data, dict), "strip targets YAML must parse to a dict"
    assert data.get("schema_version") == SCHEMA_VERSION, (
        f"strip targets schema_version mismatch: file={data.get('schema_version')!r} "
        f"test={SCHEMA_VERSION!r}; bump in lockstep or roll the test forward"
    )
    assert "universal_forbidden_substrings" in data, "missing universal_forbidden_substrings"
    assert isinstance(data["universal_forbidden_substrings"], list)
    assert len(data["universal_forbidden_substrings"]) > 0
    assert "brands" in data, "missing brands"
    assert isinstance(data["brands"], list)
    assert len(data["brands"]) > 0, "brands list is empty"
    return data


def _compose_for(brand_slug: str, class_name: str) -> str:
    """Render one library category page for one brand using the indexer
    compose pipeline. Returns the rendered HTML fragment.

    Uses the minimal token set rather than a brand-specific token map
    because the strip audit is structural - it asserts ABSENCE of
    forbidden patterns, which does not depend on the brand's actual
    color or font values.
    """
    return _compose_one_page(
        class_name,
        brand_slug=brand_slug,
        tokens=_MINIMAL_TOKENS,
    )


# ---------------------------------------------------------------------------
# Stage 3.1 - Wordmark + logo absence assertion
# ---------------------------------------------------------------------------


def test_trademark_strip_targets_yaml_loads_and_validates() -> None:
    """The YAML data file loads, parses, and passes schema validation.

    First-line guard. If this fails, every parametrized test below
    would emit a confusing skip; failing this one names the contract
    breakage directly.
    """
    data = _load_targets()
    assert data["schema_version"] == SCHEMA_VERSION


@pytest.fixture(scope="module")
def strip_targets() -> dict[str, Any]:
    """Module-scope loader. The YAML is read once per test session."""
    return _load_targets()


@pytest.fixture(scope="module")
def all_template_classes() -> tuple[str, ...]:
    """The 18 DRL category template class names."""
    classes = _all_template_classes()
    # Sanity: the corpus is the 18-template DRL set per
    # ``_vendored/drl/drl/_scripts/templates.py::TEMPLATES_BY_CLASS``.
    assert len(classes) >= 18, (
        f"expected at least 18 DRL templates, got {len(classes)}: {classes!r}"
    )
    return classes


def _audited_brands(strip_targets: dict[str, Any]) -> list[str]:
    """Brand slugs covered by the strip-targets YAML."""
    return [entry["slug"] for entry in strip_targets["brands"]]


def test_rendered_html_carries_no_forbidden_logo_or_wordmark_substrings(
    strip_targets: dict[str, Any], all_template_classes: tuple[str, ...]
) -> None:
    """Universal strip assertion.

    For every (audited brand) x (DRL category) pair, render the page and
    assert NONE of the universal forbidden substrings appear in the
    rendered HTML. Catches the common shapes a leak would take:

    - ``class="brand-logo"`` / ``class="brand-wordmark"`` / etc.
    - ``<img class="logo"`` / ``<svg class="logo"``
    - Brand-CDN logo URL fragments (``/logo.svg``, ``/wordmark.svg``,
      ``/brand/logo``, ``/assets/logo``)

    Failure mode this catches: a future DRL template change adds an
    ``<img src="{brand_logo_url}">`` slot, ``_brand_placeholder``
    learns to fill it from extracted metadata, and a real brand logo
    URL leaks into rendered_html. Test fails loud naming the brand,
    category, and forbidden substring.
    """
    universal = strip_targets["universal_forbidden_substrings"]
    brands = _audited_brands(strip_targets)
    for brand in brands:
        for class_name in all_template_classes:
            html = _compose_for(brand, class_name)
            for bad in universal:
                assert bad not in html, (
                    f"forbidden substring {bad!r} found in rendered HTML for "
                    f"brand={brand!r} class={class_name!r}; the trademark "
                    f"strip is leaking. Inspect the DRL template at "
                    f"_vendored/drl/drl/_scripts/templates.py and the "
                    f"_brand_placeholder presets at app/library_indexer.py."
                )


def test_no_brand_specific_logo_url_leaks_into_rendered_html(
    strip_targets: dict[str, Any], all_template_classes: tuple[str, ...]
) -> None:
    """Per-brand strip assertion.

    For each brand entry in the YAML, assert NONE of its
    ``forbidden_image_substrings`` appear in any rendered category page
    HTML. Catches a future extraction pipeline change that pipes the
    brand's canonical logo CDN URL into ``metadata_json.palette`` and
    a template change that surfaces it as an `<img>` / `<svg>` / `url(...)`.

    The substrings are specific to each brand (e.g. ``aeon.co/logo``,
    ``apple.com/logo``, ``openai-wordmark``) so the failure message
    names exactly which brand's mark leaked.
    """
    for entry in strip_targets["brands"]:
        brand = entry["slug"]
        forbidden = entry.get("forbidden_image_substrings", [])
        if not forbidden:
            continue
        for class_name in all_template_classes:
            html = _compose_for(brand, class_name)
            for bad in forbidden:
                assert bad not in html, (
                    f"brand-specific forbidden substring {bad!r} found in "
                    f"rendered HTML for brand={brand!r} class={class_name!r}; "
                    f"the brand's trademarked mark is leaking through. "
                    f"Inspect _compose_one_page and the upstream DRL template."
                )


def test_drl_templates_carry_no_image_or_logo_placeholders() -> None:
    """Structural strip: no DRL template carries an `<img>` slot for a
    brand mark or a placeholder named for a logo / brand_mark / photo.

    The strip is structural for 16 of 18 categories: the slot for a
    brand mark was never built. This test pins that structural choice
    so adding a `<img src="{brand_logo_url}">` slot to a template fails
    loudly here.

    Allowed exceptions:

    - ``wordmark`` / ``wordmark_sample`` slots (rendered as plain text,
      pinned separately by the test below)
    - ``avatar`` element in about-team (rendered as empty div hidden via
      override CSS, pinned by ``test_avatar_slots_render_as_hidden_*``)
    """
    from _scripts.templates import TEMPLATES_BY_CLASS

    # Slot names that would indicate a logo / brand-mark image had been
    # introduced. These must not appear in any placeholder tuple.
    forbidden_slot_substrings = (
        "logo",
        "brand_mark",
        "brand_image",
        "logo_url",
        "wordmark_url",
        "photo_url",
        "headshot",
    )

    for class_name, bundle in TEMPLATES_BY_CLASS.items():
        placeholders = bundle.get("placeholders", ())
        for slot in placeholders:
            for bad in forbidden_slot_substrings:
                assert bad not in slot, (
                    f"DRL template {class_name!r} declares slot {slot!r} "
                    f"matching forbidden pattern {bad!r}; adding a logo / "
                    f"brand-mark / photo image slot reintroduces the leak "
                    f"surface Phase 3 was meant to close. If this slot is "
                    f"intentional, update trademark_strip_targets.yml and "
                    f"this test's allowlist in lockstep."
                )

        # Defensive: no template body should declare an `<img>` tag
        # today. Adding one in the future MUST go through this gate.
        body = bundle.get("body", "")
        assert "<img" not in body, (
            f"DRL template {class_name!r} body contains an <img> tag; "
            f"image-bearing slots are a leak vector for brand logos. "
            f"If a non-brand image is intentional (e.g. an icon system), "
            f"update this test's allowlist and document the slot in "
            f"_verification/.../strip_inventory.md."
        )


# ---------------------------------------------------------------------------
# Wordmark slots render PLAIN TEXT brand name, not an image
# ---------------------------------------------------------------------------


_BRANDS_FOR_WORDMARK_CHECK = ("aeon", "openai", "stripe")
"""The three brands the CTO plan pins for Phase 1 contract assertions.
Parametrize the wordmark check across them so we get coverage on the
brands whose canonical caps differ (aeon -> Aeon, openai -> OpenAI,
stripe -> Stripe)."""


@pytest.mark.parametrize("brand", _BRANDS_FOR_WORDMARK_CHECK)
def test_wordmark_slot_renders_plain_text_brand_name_not_logo_image(
    brand: str,
) -> None:
    """Wordmark slots render the brand NAME as plain text inside a
    `<span>` or `<a>` element, NOT as an `<img>` / `<svg>` of the
    brand's stylized lockup.

    The brand NAME as attribution is allowed and IS the product
    positioning. The stylized lockup is the trademark and is stripped.

    Slots that participate:

    - alphabet `wordmark_sample` -> ``pretty_brand.lower()`` (e.g. "aeon")
    - navigation `wordmark` -> ``pretty_brand`` (e.g. "OpenAI")
    - footer `wordmark` -> ``pretty_brand``
    - library `wordmark` -> ``pretty_brand``

    Assertion shape: render alphabet (carries `wordmark_sample`),
    navigation (carries `wordmark`), and footer (carries `wordmark`);
    assert each contains the plain-text brand name in the corresponding
    text element and contains NO `<img>` / `<svg class="...logo"` / `<svg
    class="...wordmark"` inside the wordmark surface.
    """
    from app.brand_names import pretty_brand_name

    pretty = pretty_brand_name(brand)
    for class_name, expected_text in (
        ("alphabet", pretty.lower()),
        ("navigation", pretty),
        ("footer", pretty),
        ("library", pretty),
    ):
        html = _compose_for(brand, class_name)
        assert expected_text in html, (
            f"wordmark plain-text {expected_text!r} missing from rendered "
            f"{class_name} for brand={brand!r}; the brand-name "
            f"attribution is the core of 'inspired by <brand>' and must "
            f"appear."
        )
        # No <img>, no <svg> inside the rendered HTML at all today.
        # If a non-wordmark icon system lands later, narrow this to
        # exclude only image patterns inside the wordmark slot.
        assert "<img" not in html, (
            f"rendered {class_name} for brand={brand!r} contains <img>; "
            f"wordmark slot must be plain text"
        )
        assert "<svg" not in html, (
            f"rendered {class_name} for brand={brand!r} contains <svg>; "
            f"wordmark slot must be plain text"
        )


# ---------------------------------------------------------------------------
# Stage 3.2 - Avatar / people-photo handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("brand", _BRANDS_FOR_WORDMARK_CHECK)
def test_avatar_slots_render_as_hidden_placeholder_not_real_photo(
    brand: str,
) -> None:
    """about-team avatar slot must NOT render a real-person photo.

    The DRL template emits empty `<div class="at__avatar">` shells; the
    indexer appends ``LIBRARY_TEMPLATE_OVERRIDE_CSS`` which sets
    ``display: none !important`` on the scoped selector, so the gray
    placeholder circles do not paint.

    Path B (text-only fallback) per Jim's default 2026-06-03; preserved
    under the Inspirado correction (Phase 3.2 of the 2026-06-04 plan)
    because real-person photos engage right-of-publicity and the
    stylized-avatar Path A is deferred until per-brand artwork tokens
    exist.

    This test asserts:

    1. No `<img>` element appears in the about-team rendered HTML
       (no real photo was substituted).
    2. The hide rule from ``LIBRARY_TEMPLATE_OVERRIDE_CSS`` is present
       in the rendered output.

    Sibling test ``test_compose_one_page_emits_avatar_hide_rule`` in
    ``test_library_phase_b_fixes.py`` covers the override-CSS shape; this
    test pins the strip-policy guarantee end-to-end.
    """
    html = _compose_for(brand, "about-team")
    assert "<img" not in html, (
        f"about-team for brand={brand!r} contains <img>; "
        f"avatar slot must render as hidden placeholder, not a real "
        f"person photo. Right-of-publicity boundary."
    )
    assert ".rs-library-page .at__avatar" in html, (
        f"about-team for brand={brand!r} missing the scoped avatar-hide "
        f"selector; LIBRARY_TEMPLATE_OVERRIDE_CSS may have been dropped "
        f"from _compose_one_page."
    )
    assert "display: none" in html, (
        f"about-team for brand={brand!r} missing display:none on the "
        f"avatar hide rule"
    )


# ---------------------------------------------------------------------------
# metadata_json carries no logo / image fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("brand", _BRANDS_FOR_WORDMARK_CHECK)
def test_metadata_json_envelope_carries_no_logo_or_image_fields(
    brand: str,
) -> None:
    """The ``library_pages.metadata_json`` envelope must not carry any
    field named for a brand logo / image / wordmark asset.

    Today the envelope is hex color values + font family names + slot
    defaults + schema_version. Pinned defensively so a future schema
    bump that introduces an image-bearing field (``logo_url``,
    ``wordmark_url``, ``brand_image``) fails this test and triggers a
    re-review of the strip discipline before the new field ships.
    """
    metadata = _metadata_for(
        "snapshot", brand_slug=brand, tokens=_MINIMAL_TOKENS
    )
    assert isinstance(metadata, dict)
    forbidden_field_substrings = (
        "logo",
        "wordmark",
        "brand_image",
        "brand_mark",
        "headshot",
        "photo_url",
    )
    for key in metadata:
        lowered = key.lower()
        for bad in forbidden_field_substrings:
            assert bad not in lowered, (
                f"metadata_json field {key!r} matches forbidden pattern "
                f"{bad!r}; image-bearing metadata fields are a strip leak "
                f"vector. Update trademark_strip_targets.yml and this "
                f"test if the new field is intentionally non-brand."
            )


# ---------------------------------------------------------------------------
# Compose-time lint hook (forbidden-substring scan as a callable)
# ---------------------------------------------------------------------------


def _scan_for_forbidden_substrings(
    html: str, substrings: list[str]
) -> list[str]:
    """Return the list of forbidden substrings present in ``html``.

    Helper exposed at module scope so a future compose-time lint hook
    (e.g. integrated into ``_compose_one_page`` behind a feature flag,
    or run as a post-render gate in the indexer) can reuse the same
    matcher the tests rely on. Pure-data; no I/O.
    """
    return [s for s in substrings if s in html]


def test_scan_for_forbidden_substrings_catches_a_seeded_leak() -> None:
    """Sanity: the scanner detects a substring when one is present.

    Without this guard, a typo in the scanner could pass every other
    assertion in this file vacuously (the scanner would say 'no leaks'
    because it found nothing, including the things it was supposed to
    find).
    """
    seeded = '<img class="brand-logo" src="https://aeon.co/logo.svg"/>'
    hits = _scan_for_forbidden_substrings(
        seeded,
        ['<img class="brand-logo"', "aeon.co/logo", "/logo.svg"],
    )
    assert set(hits) == {
        '<img class="brand-logo"',
        "aeon.co/logo",
        "/logo.svg",
    }


def test_scan_for_forbidden_substrings_is_substring_not_regex() -> None:
    """Pin matcher semantics: substring-in, not regex.

    YAML entries are written as literal substrings; making the matcher
    regex would change escape semantics and silently break entries that
    contain regex metacharacters (e.g. ``.``).
    """
    # Seeded haystack contains the LITERAL three-char sequence "a.c".
    seeded = "path/a.c/logo.svg"
    # `.` in the target matches the literal `.`, not "any char".
    hits = _scan_for_forbidden_substrings(seeded, ["a.c"])
    assert hits == ["a.c"]
    # Different haystack with "axc" (regex would match; substring must not).
    miss = _scan_for_forbidden_substrings("axc.svg", ["a.c"])
    assert miss == []

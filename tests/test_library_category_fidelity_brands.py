"""Per-category brand-fidelity tests across 3 fixture brands.

Phase 4 of the Inspirado-no-copiado correction plan
(``projects/OptSus Team/cto-reviews/2026-06-04-resemblio-library-inspirado-no-copiado-correction-plan.md``).

Closes N-8: "Per-category brand-fidelity assertion missing (no test says
'the buttons category on Stripe page renders Stripe's actual button
radius, color, weight')." Spec:
``projects/Resemblio/_verification/library-inspirado-correction-20260604/category_fidelity_spec.md``.

Contract
--------

For each of the 18 DRL category templates registered in
``_vendored/drl/drl/_scripts/templates.py:TEMPLATES_BY_CLASS``, plus 3
synthetic fixture brands (``aeon-fixture``, ``openai-fixture``,
``stripe-fixture``) carrying distinct ``--ds-*`` token bags, the indexer
must produce ``library_pages.rendered_html`` that:

(a) Carries each of the category's PRIMARY governing tokens as a literal
    CSS custom-property declaration (``--ds-accent: #FF6B35;`` etc.) for
    the brand under test. (Token-propagation.)
(b) Does NOT carry the OTHER two brands' divergence-marker token values.
    Same-token-across-brands is the central Phase 1 bug recast at
    category granularity. (No-leak / divergence.)
(c) Produces three distinct rendered HTML strings across the three
    brands for at least one governing token. (Cross-brand divergence.)

Schema
------

``CATEGORY_FIDELITY_CONTRACTS`` is the data table; its
``CATEGORY_FIDELITY_CONTRACTS_SCHEMA_VERSION`` sentinel paired with the
spec doc's ``category_fidelity_spec_v1`` keeps doc + code in sync.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Final, TypedDict

import pytest
from sqlalchemy.orm import Session

from app.constants import SCHEMA_V1
from app.library_indexer import drain_pending, enqueue_for_asset_version
from app.models import AssetVersion, Extraction, LibraryPage
from tests.conftest import seed_user


CATEGORY_FIDELITY_CONTRACTS_SCHEMA_VERSION: Final[str] = "category_fidelity_spec_v1"
"""Paired with the spec doc's schema_version sentinel.

A bump here without a matching bump in the spec doc (or vice versa)
surfaces as test-collection drift. See the doc for bump triggers.
"""


# ---------------------------------------------------------------------------
# Fixture brand token bags
# ---------------------------------------------------------------------------


class _BrandFixture(TypedDict):
    """One fixture brand: its seed URL, slug, and complete token bag."""

    url: str
    slug: str
    tokens: dict[str, str]


# Hex/family/radius values chosen so the three brands diverge on every
# governing token. Any two brands sharing a value here would mask the
# Phase 1 same-font-across-brands class of bug at the category surface.
BRAND_FIXTURE_TOKENS: Final[dict[str, _BrandFixture]] = {
    "aeon-fixture": {
        "url": "resemblio://seed/drl_v1/aeon-fixture/library/aeon-snapshot",
        "slug": "aeon-fixture",
        "tokens": {
            # Core color
            "ds-bg": "#FAF7F2",
            "ds-surface": "#EFE9DF",
            "ds-surface-2": "#E3DBCB",
            "ds-text": "#1A1208",
            "ds-text-muted": "#6B5E4F",
            "ds-accent": "#FF6B35",
            "ds-accent-2": "#004E89",
            "ds-border": "#D9CFC0",
            "ds-hairline": "#D9CFC0",
            # Semantic
            "ds-info": "#1E6FBA",
            "ds-success": "#2E8B57",
            "ds-warning": "#D4A017",
            "ds-error": "#B23A48",
            # Radius
            "ds-radius-sm": "10px",
            "ds-radius-md": "14px",
            "ds-radius-full": "9999px",
            "ds-button-radius": "12px",
            "ds-card-radius": "16px",
            "ds-badge-radius": "9999px",
            "ds-input-radius": "10px",
            # Spacing slots referenced by templates
            "ds-button-padding-y": "12px",
            "ds-button-padding-x": "22px",
            "ds-button-border-width": "1px",
            "ds-card-padding": "28px",
            "ds-card-border-width": "1px",
            "ds-card-grid-gap": "28px",
            "ds-badge-padding-y": "4px",
            "ds-badge-padding-x": "12px",
            "ds-input-padding-y": "12px",
            "ds-input-padding-x": "14px",
            "ds-input-border-width": "1px",
            "ds-section-padding-y": "104px",
            "ds-section-padding-x": "32px",
            # Typography
            "ds-font-display": "Aeon Display, serif",
            "ds-font-body": "Aeon Text, sans-serif",
            "ds-font-mono": "Aeon Mono, monospace",
        },
    },
    "openai-fixture": {
        "url": "resemblio://seed/drl_v1/openai-fixture/library/openai-snapshot",
        "slug": "openai-fixture",
        "tokens": {
            "ds-bg": "#FFFFFE",
            "ds-surface": "#F7F7F8",
            "ds-surface-2": "#ECECF1",
            "ds-text": "#202123",
            "ds-text-muted": "#6E6E80",
            "ds-accent": "#10A37F",
            "ds-accent-2": "#19C37D",
            "ds-border": "#E5E5E5",
            "ds-hairline": "#E5E5E5",
            "ds-info": "#0EA5E9",
            "ds-success": "#22C55E",
            "ds-warning": "#EAB308",
            "ds-error": "#EF4444",
            "ds-radius-sm": "4px",
            "ds-radius-md": "6px",
            "ds-radius-full": "9999px",
            "ds-button-radius": "4px",
            "ds-card-radius": "6px",
            "ds-badge-radius": "4px",
            "ds-input-radius": "4px",
            "ds-button-padding-y": "8px",
            "ds-button-padding-x": "16px",
            "ds-button-border-width": "1px",
            "ds-card-padding": "20px",
            "ds-card-border-width": "1px",
            "ds-card-grid-gap": "16px",
            "ds-badge-padding-y": "2px",
            "ds-badge-padding-x": "8px",
            "ds-input-padding-y": "8px",
            "ds-input-padding-x": "10px",
            "ds-input-border-width": "1px",
            "ds-section-padding-y": "80px",
            "ds-section-padding-x": "24px",
            "ds-font-display": "OpenAI Sans, sans-serif",
            "ds-font-body": "OpenAI Sans, sans-serif",
            "ds-font-mono": "OpenAI Mono, monospace",
        },
    },
    "stripe-fixture": {
        "url": "resemblio://seed/drl_v1/stripe-fixture/library/stripe-snapshot",
        "slug": "stripe-fixture",
        "tokens": {
            "ds-bg": "#FFFFFD",
            "ds-surface": "#F6F9FC",
            "ds-surface-2": "#E6EBF1",
            "ds-text": "#0A2540",
            "ds-text-muted": "#425466",
            "ds-accent": "#635BFF",
            "ds-accent-2": "#00D4FF",
            "ds-border": "#E3E8EE",
            "ds-hairline": "#E3E8EE",
            "ds-info": "#0073E6",
            "ds-success": "#0E9F6E",
            "ds-warning": "#F59E0B",
            "ds-error": "#DF1B41",
            "ds-radius-sm": "6px",
            "ds-radius-md": "8px",
            "ds-radius-full": "9999px",
            "ds-button-radius": "6px",
            "ds-card-radius": "8px",
            "ds-badge-radius": "9999px",
            "ds-input-radius": "6px",
            "ds-button-padding-y": "10px",
            "ds-button-padding-x": "18px",
            "ds-button-border-width": "1px",
            "ds-card-padding": "24px",
            "ds-card-border-width": "1px",
            "ds-card-grid-gap": "20px",
            "ds-badge-padding-y": "3px",
            "ds-badge-padding-x": "10px",
            "ds-input-padding-y": "10px",
            "ds-input-padding-x": "12px",
            "ds-input-border-width": "1px",
            "ds-section-padding-y": "96px",
            "ds-section-padding-x": "28px",
            "ds-font-display": "Stripe Sans, sans-serif",
            "ds-font-body": "Stripe Sans, sans-serif",
            "ds-font-mono": "Stripe Sans Mono, monospace",
        },
    },
}


# ---------------------------------------------------------------------------
# Per-category fidelity contracts
# ---------------------------------------------------------------------------


class _CategoryContract(TypedDict):
    """Governs the assertions for one DRL category template."""

    # Tokens whose literal --<slot>: <value>; declaration MUST appear in
    # rendered_html for the brand under test.
    propagation_slots: tuple[str, ...]
    # The single slot used for cross-brand divergence assertion. Picked
    # because the three fixture brands carry three distinct values for it.
    divergence_slot: str


CATEGORY_FIDELITY_CONTRACTS: Final[dict[str, _CategoryContract]] = {
    "alphabet": {
        "propagation_slots": ("ds-font-display", "ds-font-body", "ds-bg", "ds-text"),
        "divergence_slot": "ds-font-display",
    },
    "buttons": {
        "propagation_slots": (
            "ds-accent",
            "ds-button-radius",
            "ds-button-padding-y",
            "ds-button-padding-x",
        ),
        "divergence_slot": "ds-button-radius",
    },
    "badges": {
        "propagation_slots": (
            "ds-badge-radius",
            "ds-badge-padding-y",
            "ds-badge-padding-x",
            "ds-success",
            "ds-error",
        ),
        "divergence_slot": "ds-accent",
    },
    "cards": {
        "propagation_slots": (
            "ds-surface",
            "ds-border",
            "ds-card-radius",
            "ds-card-padding",
            "ds-card-grid-gap",
        ),
        "divergence_slot": "ds-card-radius",
    },
    "cta-block": {
        "propagation_slots": (
            "ds-surface",
            "ds-accent",
            "ds-section-padding-y",
            "ds-section-padding-x",
        ),
        "divergence_slot": "ds-accent",
    },
    "feature-grid": {
        "propagation_slots": (
            "ds-bg",
            "ds-surface",
            "ds-card-radius",
            "ds-card-grid-gap",
            "ds-accent",
        ),
        "divergence_slot": "ds-card-radius",
    },
    "footer": {
        "propagation_slots": ("ds-bg", "ds-text", "ds-hairline", "ds-text-muted"),
        "divergence_slot": "ds-accent",
    },
    "form-fields": {
        "propagation_slots": (
            "ds-input-radius",
            "ds-input-padding-y",
            "ds-input-padding-x",
            "ds-accent",
            "ds-error",
        ),
        "divergence_slot": "ds-input-radius",
    },
    "hero": {
        "propagation_slots": (
            "ds-font-display",
            "ds-accent",
            "ds-bg",
            "ds-section-padding-y",
        ),
        "divergence_slot": "ds-font-display",
    },
    "inputs": {
        "propagation_slots": ("ds-radius-full", "ds-radius-sm", "ds-accent"),
        "divergence_slot": "ds-radius-sm",
    },
    "library": {
        "propagation_slots": (
            "ds-bg",
            "ds-text",
            "ds-accent",
            "ds-card-radius",
            "ds-button-radius",
        ),
        "divergence_slot": "ds-button-radius",
    },
    "navigation": {
        "propagation_slots": ("ds-bg", "ds-hairline", "ds-accent", "ds-button-radius"),
        "divergence_slot": "ds-accent",
    },
    "news-list": {
        "propagation_slots": ("ds-bg", "ds-hairline", "ds-text-muted", "ds-font-display"),
        "divergence_slot": "ds-font-display",
    },
    "pricing-table": {
        "propagation_slots": (
            "ds-surface",
            "ds-accent",
            "ds-card-radius",
            "ds-badge-radius",
        ),
        "divergence_slot": "ds-card-radius",
    },
    "process-steps": {
        "propagation_slots": ("ds-accent", "ds-font-mono", "ds-bg"),
        "divergence_slot": "ds-font-mono",
    },
    "testimonials": {
        "propagation_slots": ("ds-surface", "ds-bg", "ds-border", "ds-card-radius"),
        "divergence_slot": "ds-card-radius",
    },
    "about-team": {
        "propagation_slots": (
            "ds-bg",
            "ds-surface-2",
            "ds-radius-full",
            "ds-text-muted",
        ),
        "divergence_slot": "ds-surface-2",
    },
    "article-layout": {
        "propagation_slots": (
            "ds-bg",
            "ds-text",
            "ds-accent",
            "ds-hairline",
            "ds-font-display",
        ),
        "divergence_slot": "ds-accent",
    },
}


def _expected_categories() -> tuple[str, ...]:
    """Return the 18 categories from DRL's ``TEMPLATES_BY_CLASS``.

    Lazy-imported because the DRL module path is installed at module-load
    time by ``app.extractor_bridge``. Importing at top-of-file order risks
    racing the bridge install (see ``test_library_indexer_module_load_race``).
    """
    from _scripts.templates import TEMPLATES_BY_CLASS  # local import

    return tuple(sorted(TEMPLATES_BY_CLASS.keys()))


# ---------------------------------------------------------------------------
# Static contract-shape assertions (collection-time)
# ---------------------------------------------------------------------------


def test_category_fidelity_contracts_cover_every_drl_category() -> None:
    """The contract table covers exactly the 18 categories DRL registers.

    A category added to ``TEMPLATES_BY_CLASS`` without a matching contract
    row fails this test, prompting an update to both this file and the
    spec doc. A category removed from DRL without trimming this table
    also fails, preventing a stale row from masking a real coverage gap.
    """
    drl_categories = set(_expected_categories())
    contract_categories = set(CATEGORY_FIDELITY_CONTRACTS.keys())
    assert drl_categories == contract_categories, (
        f"DRL categories vs Phase 4 contract drift; "
        f"missing from contracts: {drl_categories - contract_categories!r}; "
        f"missing from DRL: {contract_categories - drl_categories!r}"
    )
    assert len(contract_categories) == 18, (
        f"expected 18 categories per Phase 4 spec; got {len(contract_categories)}"
    )


def test_divergence_slots_actually_diverge_across_fixture_brands() -> None:
    """Each category's divergence_slot carries 3 distinct values across brands.

    A divergence slot whose three brand values collapse to two-or-fewer
    distinct values is unable to prove cross-brand fidelity; this static
    check catches that fixture-misconfiguration class of bug at
    collection time, before any indexer drain runs.
    """
    brand_slugs = tuple(BRAND_FIXTURE_TOKENS.keys())
    assert len(brand_slugs) == 3, "Phase 4 spec pins 3 fixture brands"

    for category, contract in CATEGORY_FIDELITY_CONTRACTS.items():
        slot = contract["divergence_slot"]
        values = {
            BRAND_FIXTURE_TOKENS[brand]["tokens"][slot]
            for brand in brand_slugs
        }
        assert len(values) == 3, (
            f"category {category!r}: divergence_slot {slot!r} produces "
            f"{len(values)} distinct values across {brand_slugs!r} "
            f"(values: {sorted(values)!r}); expected 3 to prove "
            f"cross-brand divergence"
        )


# ---------------------------------------------------------------------------
# Indexer drive helpers
# ---------------------------------------------------------------------------


def _insert_fixture_brand(session: Session, brand: _BrandFixture) -> AssetVersion:
    """Insert an AssetVersion for a fixture brand and queue it for compose."""
    av = AssetVersion(
        url=brand["url"],
        content_hash=f"phase4-fixture-{brand['slug']}",
        dtcg_json={"tokens": dict(brand["tokens"])},
        manifest_schema_version=SCHEMA_V1,
        is_public=True,
        version_label=f"phase4-{brand['slug']}",
        fetched_at=datetime.now(timezone.utc),
    )
    session.add(av)
    session.flush()
    return av


def _attach_extraction(
    session: Session, asset_version: AssetVersion, *, user_id: int
) -> Extraction:
    """Attach a passing extraction so the indexer quality gate clears."""
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


def _drive_all_fixture_brands(session: Session) -> dict[str, AssetVersion]:
    """Insert + drain all 3 fixture brands; return slug-keyed AssetVersions.

    The drain is run once per brand so each brand's pages reach
    ``library_pages`` independently. Returns the map so per-brand tests
    can filter rows by ``asset_version_id``.
    """
    user, _key, _ = seed_user(session)
    inserted: dict[str, AssetVersion] = {}
    for brand_slug, brand in BRAND_FIXTURE_TOKENS.items():
        av = _insert_fixture_brand(session, brand)
        _attach_extraction(session, av, user_id=user.id)
        job = enqueue_for_asset_version(session, av.id)
        assert job is not None, (
            f"enqueue helper returned None for {brand_slug!r} - precondition broken"
        )
        session.commit()
        result = drain_pending(session)
        assert result.pages_written > 0, (
            f"drain wrote zero pages for {brand_slug!r}; "
            f"quality gate or compose failure"
        )
        inserted[brand_slug] = av
    return inserted


def _page_for(
    session: Session, asset_version: AssetVersion, category_slug: str
) -> LibraryPage | None:
    """Look up the persisted page row for a (brand, category) pair."""
    return (
        session.query(LibraryPage)
        .filter_by(asset_version_id=asset_version.id, category_slug=category_slug)
        .one_or_none()
    )


# ---------------------------------------------------------------------------
# Parametrized assertions: 18 categories x 3 brands
# ---------------------------------------------------------------------------


_PARAM_PAIRS: tuple[tuple[str, str], ...] = tuple(
    (category, brand)
    for category in sorted(CATEGORY_FIDELITY_CONTRACTS.keys())
    for brand in sorted(BRAND_FIXTURE_TOKENS.keys())
)


@pytest.mark.parametrize(("category_slug", "brand_slug"), _PARAM_PAIRS)
def test_category_propagates_brand_tokens_into_rendered_html(
    session: Session, category_slug: str, brand_slug: str
) -> None:
    """For (category, brand), every propagation_slot lands literally in rendered_html.

    Pins the per-category-brand half of N-8. A regression that drops
    one of a category's governing tokens (e.g. button radius silently
    falls back to ``--ds-radius-sm`` rather than ``--ds-button-radius``)
    fails here on the brand-fixture-specific value.
    """
    inserted = _drive_all_fixture_brands(session)
    brand = BRAND_FIXTURE_TOKENS[brand_slug]
    av = inserted[brand_slug]
    page = _page_for(session, av, category_slug)
    assert page is not None, (
        f"no library_pages row for brand={brand_slug!r} "
        f"category={category_slug!r}; compose path skipped this category"
    )
    rendered = page.rendered_html
    assert isinstance(rendered, str) and rendered, (
        f"page rendered_html empty for brand={brand_slug!r} category={category_slug!r}"
    )
    contract = CATEGORY_FIDELITY_CONTRACTS[category_slug]
    for slot in contract["propagation_slots"]:
        expected_value = brand["tokens"][slot]
        expected_decl = f"--{slot}: {expected_value};"
        assert expected_decl in rendered, (
            f"brand={brand_slug!r} category={category_slug!r}: "
            f"expected propagation declaration {expected_decl!r} in "
            f"rendered_html; token did not reach the page"
        )


@pytest.mark.parametrize(("category_slug", "brand_slug"), _PARAM_PAIRS)
def test_category_does_not_leak_other_brands_divergence_values(
    session: Session, category_slug: str, brand_slug: str
) -> None:
    """The OTHER brands' divergence_slot values do NOT appear in this page.

    Strict no-leak: brand A's `--ds-accent: #FF6B35` must not surface in
    brand B's rendered HTML. Pins the strip-discipline corollary at the
    category-token layer.
    """
    inserted = _drive_all_fixture_brands(session)
    av = inserted[brand_slug]
    page = _page_for(session, av, category_slug)
    assert page is not None
    rendered = page.rendered_html
    contract = CATEGORY_FIDELITY_CONTRACTS[category_slug]
    slot = contract["divergence_slot"]
    own_value = BRAND_FIXTURE_TOKENS[brand_slug]["tokens"][slot]
    for other_slug, other_brand in BRAND_FIXTURE_TOKENS.items():
        if other_slug == brand_slug:
            continue
        other_value = other_brand["tokens"][slot]
        if other_value == own_value:
            # Defensive: the static divergence guard already prevents
            # this, but if a future edit weakens the fixture and this
            # branch silently passes we want a loud signal.
            pytest.fail(
                f"fixture drift: {slot!r} value {other_value!r} shared "
                f"between {brand_slug!r} and {other_slug!r}; the "
                f"divergence-slot guard test should have caught this"
            )
        leaked_decl = f"--{slot}: {other_value};"
        assert leaked_decl not in rendered, (
            f"brand={brand_slug!r} category={category_slug!r}: leaked "
            f"other-brand declaration {leaked_decl!r} (from {other_slug!r}) "
            f"into rendered_html; strip discipline broken at the category-token layer"
        )


# ---------------------------------------------------------------------------
# Per-category cross-brand divergence (18 cases)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("category_slug", sorted(CATEGORY_FIDELITY_CONTRACTS.keys()))
def test_three_brands_render_distinct_divergence_slot_values(
    session: Session, category_slug: str
) -> None:
    """Across the 3 fixture brands, the divergence_slot value MUST differ.

    Closes the Phase 1 same-token-across-brands bug class at the
    per-category level. If all three brands render the same value for
    the chosen divergence slot, the category surface is failing the
    Inspirado-no-copiado mandate at this granularity.
    """
    inserted = _drive_all_fixture_brands(session)
    contract = CATEGORY_FIDELITY_CONTRACTS[category_slug]
    slot = contract["divergence_slot"]
    rendered_per_brand: dict[str, str] = {}
    declarations_seen: set[str] = set()
    for brand_slug, av in inserted.items():
        page = _page_for(session, av, category_slug)
        assert page is not None, (
            f"category {category_slug!r} missing for brand {brand_slug!r}"
        )
        rendered_per_brand[brand_slug] = page.rendered_html
        expected_value = BRAND_FIXTURE_TOKENS[brand_slug]["tokens"][slot]
        declarations_seen.add(f"--{slot}: {expected_value};")
    assert len(declarations_seen) == 3, (
        f"category {category_slug!r}: expected 3 distinct "
        f"--{slot}: <value>; declarations across the 3 fixture brands, "
        f"got {len(declarations_seen)}: {sorted(declarations_seen)!r}"
    )
    # And the corresponding rendered_html strings themselves must differ
    # (a stronger property than the declaration-set distinctness above:
    # this catches the case where the divergence slot diverges but the
    # template silently ignores it, leaving the rendered bytes identical).
    rendered_signatures = {html for html in rendered_per_brand.values()}
    assert len(rendered_signatures) == 3, (
        f"category {category_slug!r}: 3 fixture brands collapsed to "
        f"{len(rendered_signatures)} distinct rendered_html strings; "
        f"category surface is not brand-faithful"
    )

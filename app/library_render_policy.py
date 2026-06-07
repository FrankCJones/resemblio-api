"""Render-real-or-hide decision for Library v2 component categories.

This module implements the D2 decision from the Library v2 plan (2026-06-07):
when a brand's BrandCaptureManifest shows that a component group is NOT
captured, the corresponding component-showcase category is OMITTED from the
rendered page. Page-pattern categories render regardless.

The D2 line
-----------
Two things happen with uncaptured component groups:

1. Token-level cascade-safety fallback (ALLOWED):
   ``_emit_brand_root`` still emits the contract-default value for every slot
   into the ``:root`` block (e.g. ``--ds-button-padding-y: 10px``). This keeps
   ``var(--ds-button-padding-y)`` resolving to a defined value everywhere it
   appears in any template CSS - even on page-pattern templates that incidentally
   reference button slots. The CSS variable exists and resolves; the cascade is safe.

2. Component body fabrication (FORBIDDEN):
   Rendering the full HTML body of the ``buttons`` template (or ``cards``,
   ``badges``, ``form-fields``, ``inputs``) when the brand has no real geometry
   data. That body, rendered at contract defaults, looks like a brand-design
   representation but is entirely generic - every uncaptured brand would render
   identically. This is the fabrication D2 prohibits.

``evaluate_category_render`` is the gating function: the indexer calls it for
each template class before composing HTML, and omits the HTML body when the
decision is ``should_render=False``.

Category-to-group mapping
--------------------------
Only component-showcase categories are gated. Page-pattern categories
(hero, navigation, footer, etc.) incidentally reference button/card tokens
via ``var(--ds-button-*)``, but their purpose is layout demonstration, not
component-geometry showcase. The cascade-safety fallback makes those references
render at contract defaults, which is honest - the page is demonstrating layout,
not claiming to show the brand's button design.

CATEGORY_CAPTURE_REQUIREMENTS maps showcase category slug -> frozenset of
component group names that must ALL be captured for the category to render.
An empty frozenset means "render always" (equivalent to not being in the map).
Only category slugs that map to a non-empty requirement are stored; everything
else is treated as unconditionally renderable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.brand_capture_manifest import BrandCaptureManifest


# ---------------------------------------------------------------------------
# Category -> required component groups
# ---------------------------------------------------------------------------
#
# Maps template class_name (category_slug) -> frozenset of component group names
# that must ALL be captured for the category to render.
#
# Only component-showcase categories appear here. Page-pattern categories
# (hero, navigation, footer, cta-block, feature-grid, about-team, etc.) are
# intentionally absent - they render regardless and are NOT hidden when button
# or card geometry is uncaptured.
#
# Rationale per entry:
#   buttons: The sole purpose of this template is to showcase button geometry
#            at every size variant. Without brand-captured button data it would
#            render a generic DRL chiclet layout as if it were the brand's design.
#   cards:   Same rationale for card geometry (padding, border, radius).
#   badges:  Same rationale for badge geometry (padding, border, radius).
#   form-fields: Same rationale for input geometry (field padding, border, radius).
#   inputs:  The inputs template showcases search bars, tags, and toggle controls
#            that also depend on input geometry (same group as form-fields).
#   library: Composite showcase: buttons + cards + badges. Hidden when ALL
#            three are uncaptured; renders when ANY required group is captured.
#            (Note: evaluate_category_render uses ANY-captured semantics for
#            library so the page appears when SOME components are ready.)

CATEGORY_CAPTURE_REQUIREMENTS: dict[str, frozenset[str]] = {
    "buttons": frozenset({"button"}),
    "cards": frozenset({"card"}),
    "badges": frozenset({"badge"}),
    "form-fields": frozenset({"input"}),
    "inputs": frozenset({"input"}),
    # library is composite: requires ALL three to render faithfully.
    # When NONE are captured, the page would be entirely empty - hide it.
    # The ANY-vs-ALL choice is encoded in evaluate_category_render below.
    "library": frozenset({"button", "card", "badge"}),
}


# ---------------------------------------------------------------------------
# Decision type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CategoryRenderDecision:
    """The render decision for one (category, manifest) pair.

    Immutable. ``should_render=True`` means the indexer composes the HTML body.
    ``should_render=False`` means the body is omitted and the gap is recorded
    in the manifest's missing_groups for the acknowledgment surface.

    ``missing_groups`` is always a sorted tuple of the group names that are
    required but NOT captured. Empty when ``should_render=True``.
    """

    should_render: bool
    missing_groups: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Core decision function
# ---------------------------------------------------------------------------


def evaluate_category_render(
    category_slug: str,
    manifest: BrandCaptureManifest,
) -> CategoryRenderDecision:
    """Return the render decision for one category given a brand's capture manifest.

    Args:
        category_slug: the DRL template class name (e.g. 'buttons', 'hero').
        manifest: the brand's ``BrandCaptureManifest`` from
            ``brand_capture_manifest.build_capture_manifest``.

    Returns:
        ``CategoryRenderDecision`` with ``should_render`` and ``missing_groups``.

    Decision logic:
    - If ``category_slug`` is not in ``CATEGORY_CAPTURE_REQUIREMENTS``: render always.
    - If it IS in the map: check whether all required groups are captured.
      For all categories except 'library': ALL required groups must be captured.
      For 'library': any one of the required groups being captured is sufficient
      (the page shows whatever is available; it is itself a composite index).
    """
    requirements = CATEGORY_CAPTURE_REQUIREMENTS.get(category_slug)
    if requirements is None:
        # Page-pattern category or unknown future category: render unconditionally.
        return CategoryRenderDecision(should_render=True, missing_groups=())

    captured_groups = {
        group
        for group, detail in manifest["groups"].items()
        if detail["captured"]
    }

    missing = sorted(requirements - captured_groups)

    if category_slug == "library":
        # Composite showcase: render if ANY required group is captured.
        # The page-level content adapts to show what is available.
        any_captured = bool(requirements & captured_groups)
        return CategoryRenderDecision(
            should_render=any_captured,
            missing_groups=tuple(missing),
        )

    # All other showcase categories: ALL required groups must be captured.
    should_render = len(missing) == 0
    return CategoryRenderDecision(
        should_render=should_render,
        missing_groups=tuple(missing),
    )


# ---------------------------------------------------------------------------
# Batch helper
# ---------------------------------------------------------------------------


def filter_captured_categories(
    category_slugs: list[str],
    manifest: BrandCaptureManifest,
) -> dict[str, CategoryRenderDecision]:
    """Return a render decision for each category in ``category_slugs``.

    Pure helper: calls ``evaluate_category_render`` for each slug and returns
    a ``{category_slug: decision}`` dict. Deterministic ordering follows the
    input list.

    Args:
        category_slugs: all template class names to evaluate.
        manifest: the brand's ``BrandCaptureManifest``.

    Returns:
        Dict mapping each slug to its ``CategoryRenderDecision``.
    """
    return {slug: evaluate_category_render(slug, manifest) for slug in category_slugs}

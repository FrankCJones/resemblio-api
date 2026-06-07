"""Honest gap acknowledgment for Library v2 (Phase 3).

Generates the structured missing-data notice for any brand whose BrandCaptureManifest
shows uncaptured showcase categories. The notice is:
  - Factual and neutral: "not captured yet" not "coming soon" or "we're sorry".
  - Non-fabricated: no invented brand claims, no generic placeholder content.
  - On-brand: consistent with Resemblio's "Inspirado, no copiado" honesty.
  - Pure-data: no HTML here; the web component renders from the structured payload.

Two surfaces:
  1. Brand page (MissingDataSummary): the full list of uncaptured showcase
     categories with their display names, used to render the missing-data section
     at the bottom of a brand library page.
  2. Hub card (HubCaptureSignal): a coarse "N of M captured" count for the brand
     card grid on the /library/ hub page. Shows all brands regardless of count.

Both outputs are schema-versioned so downstream consumers can detect shape drift.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.brand_capture_manifest import BrandCaptureManifest
from app.library_render_policy import CATEGORY_CAPTURE_REQUIREMENTS

MISSING_DATA_NOTICE_SCHEMA_VERSION = "missing_data_notice_v1"
"""Bumped when MissingDataSummary or MissingItem shape changes."""

HUB_CAPTURE_SIGNAL_SCHEMA_VERSION = "hub_capture_signal_v1"
"""Bumped when HubCaptureSignal shape changes."""


# ---------------------------------------------------------------------------
# Display name map for showcase categories
# ---------------------------------------------------------------------------
#
# Human-readable labels for each showcase category slug. These surface in:
#   - The brand page's missing-data section ("Buttons, Cards, Badges not captured yet")
#   - The hub card's tooltip or subtitle
#
# Order here does NOT control render order (sorted alphabetically by display name
# in build_missing_notice). Only display name values are user-visible.

SHOWCASE_CATEGORY_DISPLAY_NAMES: dict[str, str] = {
    "badges": "Badges",
    "buttons": "Buttons",
    "cards": "Cards",
    "form-fields": "Form Fields",
    "inputs": "Inputs",
    "library": "Component Library",
}

# Showcase categories with a definite "this is one component group" identity.
# 'library' is composite and handled separately by the hub signal count.
# The hub capture signal counts the five primary showcase groups (not 'library')
# to give a clean "N of 5 captured" reading.
_PRIMARY_SHOWCASE_CATEGORIES: tuple[str, ...] = (
    "badges",
    "buttons",
    "cards",
    "form-fields",
    "inputs",
)


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MissingItem:
    """One uncaptured showcase category in the missing-data notice."""

    category_slug: str
    """The template class name (e.g. 'buttons')."""

    display_name: str
    """Human-readable label for the UI (e.g. 'Buttons')."""


@dataclass(frozen=True)
class MissingDataSummary:
    """Full missing-data notice for a brand page.

    ``missing_items`` is a sorted tuple (alphabetically by display_name) of
    showcase categories that are NOT captured for this brand. Empty when all
    showcase categories are captured.
    """

    schema_version: str
    missing_items: tuple[MissingItem, ...]


@dataclass(frozen=True)
class HubCaptureSignal:
    """Coarse capture count for a hub card.

    ``captured_count`` is the number of primary showcase categories whose
    required component groups ARE captured. ``total_showcase_groups`` is the
    total number of primary showcase categories (5 for the current set).

    The hub card UI reads this as "N of M components captured" without gating
    visibility - all brands show on the hub regardless of the count (D4).
    """

    schema_version: str
    captured_count: int
    total_showcase_groups: int


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_missing_notice(manifest: BrandCaptureManifest) -> MissingDataSummary:
    """Return the missing-data notice for a brand's capture manifest.

    Iterates the showcase categories with capture requirements and returns an
    ordered list of those whose required groups are NOT all captured. Sorted
    alphabetically by display name for deterministic output.

    Pure: no I/O, no side effects.

    Args:
        manifest: the brand's ``BrandCaptureManifest``.

    Returns:
        ``MissingDataSummary`` with ``missing_items`` tuple (empty if all captured).
    """
    captured_groups = frozenset(
        group
        for group, detail in manifest["groups"].items()
        if detail["captured"]
    )

    missing: list[MissingItem] = []
    for category_slug in _PRIMARY_SHOWCASE_CATEGORIES:
        requirements = CATEGORY_CAPTURE_REQUIREMENTS.get(category_slug)
        if requirements is None:
            continue
        # The category is "missing" when ANY required group is absent.
        if not requirements.issubset(captured_groups):
            display_name = SHOWCASE_CATEGORY_DISPLAY_NAMES.get(
                category_slug, category_slug.replace("-", " ").title()
            )
            missing.append(MissingItem(category_slug=category_slug, display_name=display_name))

    # Sort by display name for determinism; this is the order the UI renders.
    missing.sort(key=lambda item: item.display_name)
    return MissingDataSummary(
        schema_version=MISSING_DATA_NOTICE_SCHEMA_VERSION,
        missing_items=tuple(missing),
    )


def build_hub_capture_signal(manifest: BrandCaptureManifest) -> HubCaptureSignal:
    """Return the coarse capture count for a hub card.

    Counts how many of the primary showcase categories have ALL their required
    component groups captured. Does NOT count 'library' (composite; would
    double-count button/card/badge already in the primary set).

    Pure: no I/O, no side effects.

    Args:
        manifest: the brand's ``BrandCaptureManifest``.

    Returns:
        ``HubCaptureSignal`` with counts.
    """
    captured_groups = frozenset(
        group
        for group, detail in manifest["groups"].items()
        if detail["captured"]
    )

    captured_count = sum(
        1
        for category_slug in _PRIMARY_SHOWCASE_CATEGORIES
        if (
            (reqs := CATEGORY_CAPTURE_REQUIREMENTS.get(category_slug)) is not None
            and reqs.issubset(captured_groups)
        )
    )

    return HubCaptureSignal(
        schema_version=HUB_CAPTURE_SIGNAL_SCHEMA_VERSION,
        captured_count=captured_count,
        total_showcase_groups=len(_PRIMARY_SHOWCASE_CATEGORIES),
    )

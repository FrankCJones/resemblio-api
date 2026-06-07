"""Per-brand provenance: which component groups have REAL captured data.

This is the new primitive introduced by Library v2 (2026-06-07). It drives
the render-real-or-hide decision in the indexer (Phase 2) and the honest
missing-data acknowledgment on the page (Phase 3).

Background
----------
The BRAND_TOKEN_CONTRACT enumerates every ``--ds-*`` slot the DRL templates
consume. For each slot, ``_emit_brand_root`` emits either the brand's real
value (if present in the token bag) or the contract default. The contract
default is intentionally the same value baked into the original template CSS,
so an empty token bag produces byte-identical CSS to the pre-contract state.

The problem this module solves: when a brand's token bag does NOT include
button/card/badge/input geometry slots, the rendered component looks identical
to a brand that was explicitly captured with the same defaults. There is no
signal distinguishing "brand chose these exact values" from "we have no data
for this brand." The BrandCaptureManifest IS that signal.

The "captured" rule per group
-----------------------------
Each component group has a named rule table (``_CAPTURE_RULES``) that maps
to a check function: given the brand-supplied override dict (normalized slot
names) and the optional ButtonTokens snapshot, return True only when REAL
brand-specific data is present. The rule table is the heart of this module;
the rationale per group is documented inline.

The contract for "captured"
---------------------------
"Captured" means the brand supplied brand-specific values for this component
group's GEOMETRY-defining slots (not just its color, and not just defaults).
The exception is color, typography, spacing, radius, layout, and section:
for these groups, ANY brand-supplied value is a real signal (the groups'
whole point is the scale itself, not per-component geometry).

Key normalization
-----------------
Token dicts arrive in multiple formats (DRL seed: ``ds-bg``, ``ds-font-body``;
organic: ``bg``, ``font_body``; underscored: ``font_display``). This module
applies the same normalization as ``_ds_var_name`` in ``library_indexer.py``
so all three forms map to the same ``--ds-<name>`` canonical form, and then
to the slot name without the leading ``--``.

Schema versioning
-----------------
``CAPTURE_MANIFEST_SCHEMA_VERSION`` is bumped when ``BrandCaptureManifest``
or ``GroupCaptureDetail`` changes shape. Downstream consumers (indexer,
routes, web contract) key off this string for shape detection.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TypedDict

from extractor.button_tokens import ButtonTokens
from extractor.token_contract import BRAND_TOKEN_CONTRACT, slots_for_group

CAPTURE_MANIFEST_SCHEMA_VERSION = "capture_manifest_v1"
"""Bumped when BrandCaptureManifest or GroupCaptureDetail shape changes."""

# All component groups the manifest covers (alphabetical; drives iteration order).
# Any group added to BRAND_TOKEN_CONTRACT should appear here; tests will catch drift.
COMPONENT_GROUPS: tuple[str, ...] = (
    "badge",
    "button",
    "card",
    "color",
    "input",
    "layout",
    "motion",
    "radius",
    "section",
    "shadow",
    "spacing",
    "typography",
)


class GroupCaptureDetail(TypedDict):
    """Per-group capture status with source-field provenance."""

    captured: bool
    """True when REAL brand-specific data for this group is present."""

    present_source_fields: tuple[str, ...]
    """Contract source_field values (e.g. 'button.padding-y') confirmed present
    in the brand token bag. Sorted for determinism."""

    absent_source_fields: tuple[str, ...]
    """Contract source_field values confirmed absent (would fall to default).
    Sorted for determinism. Used by the DATA_CONTRACT_HANDOFF to tell the
    separate data project exactly which fields to populate."""


class BrandCaptureManifest(TypedDict):
    """Per-brand provenance for the Library v2 render-real-or-hide decision."""

    schema_version: str
    """Always ``CAPTURE_MANIFEST_SCHEMA_VERSION``. Downstream consumers key
    off this for shape detection."""

    groups: dict[str, GroupCaptureDetail]
    """Keyed by component group name (one of ``COMPONENT_GROUPS``)."""


# ---------------------------------------------------------------------------
# Key normalization (mirrors library_indexer._ds_var_name)
# ---------------------------------------------------------------------------

def _normalize_token_key(raw_key: str) -> str:
    """Return the slot name (without leading '--') for any token key format.

    Three input formats all produce the same slot name:
      - DRL seed:  'ds-bg'        -> 'ds-bg'
      - Organic:   'bg'           -> 'ds-bg'
      - Underscored: 'font_body'  -> 'ds-font-body'
    """
    normalized = raw_key.replace("_", "-")
    if normalized.startswith("ds-"):
        return normalized
    return f"ds-{normalized}"


def _build_overrides(tokens: dict[str, str]) -> dict[str, str]:
    """Return a {slot_name: value} dict keyed by canonical slot names.

    Applies the same normalization as ``_emit_brand_root`` in
    ``library_indexer.py`` so the manifest's understanding of "which slots
    are brand-supplied" is identical to the indexer's.
    """
    overrides: dict[str, str] = {}
    for raw_key, value in (tokens or {}).items():
        slot_name = _normalize_token_key(raw_key)
        overrides[slot_name] = value
    return overrides


# ---------------------------------------------------------------------------
# Per-group capture rules
# ---------------------------------------------------------------------------
#
# Each rule function receives (overrides, button_tokens) and returns bool.
# The rule table is the source of truth for what "faithfully renderable" means
# per group. See plan Section 8 for the Opus-tunable overrides.
#
# General principle: a group is captured when its GEOMETRY-defining slots
# have brand-supplied values (not contract defaults). Color, typography, spacing,
# radius, layout, and section are the exceptions - for these groups the scale
# itself IS the brand signal, so any brand-supplied value counts.


def _any_slot_in_group(group: str, overrides: dict[str, str]) -> bool:
    """Return True if any contract slot for ``group`` is brand-supplied."""
    for slot_name in slots_for_group(group):
        if slot_name in overrides:
            return True
    return False


def _color_captured(overrides: dict[str, str], _bt: ButtonTokens | None) -> bool:
    """Color is captured when any of the three primary palette slots are present.

    ds-bg, ds-accent, and ds-text are the minimum viable palette. A brand
    that supplies all three renders with a distinct, recognizable palette.
    Requiring all three (not just one) prevents an accidentally-present default
    value from triggering a false positive, while not requiring the full
    14-slot set (rare for any brand type).
    """
    minimum = {"ds-bg", "ds-accent", "ds-text"}
    return minimum.issubset(overrides.keys())


def _typography_captured(overrides: dict[str, str], _bt: ButtonTokens | None) -> bool:
    """Typography is captured when font families or weight/tracking are brand-supplied.

    Two paths:
    1. Font-family extras (ds-font-body or ds-font-display in overrides). These
       are NOT in BRAND_TOKEN_CONTRACT but are passed through as extras by
       _emit_brand_root. They are the primary signal for DRL seed brands - a
       brand that supplies its own font families renders distinct typography.
    2. At least one contract weight/tracking slot is brand-supplied (organic
       brands that provide weights but not family names).

    Either path is sufficient; a brand rendering with its own families OR its
    own weight/tracking system is faithfully distinct from a generic default.
    """
    font_family_extras = {"ds-font-body", "ds-font-display"}
    if font_family_extras & overrides.keys():
        return True
    # Check contract typography slots (weights + tracking)
    return _any_slot_in_group("typography", overrides)


def _spacing_captured(overrides: dict[str, str], _bt: ButtonTokens | None) -> bool:
    """Spacing is captured when any spacing slot is brand-supplied."""
    return _any_slot_in_group("spacing", overrides)


def _radius_captured(overrides: dict[str, str], _bt: ButtonTokens | None) -> bool:
    """Radius is captured when any of the scale slots (not the per-component
    aliases) are brand-supplied. The aliases (ds-radius-button etc.) chain
    to the scale slots; a brand that supplies the scale drives all radii.
    """
    scale_slots = {"ds-radius-xs", "ds-radius-sm", "ds-radius-md", "ds-radius-lg", "ds-radius-full"}
    return bool(scale_slots & overrides.keys())


def _layout_captured(overrides: dict[str, str], _bt: ButtonTokens | None) -> bool:
    """Layout is captured when any layout slot is brand-supplied."""
    return _any_slot_in_group("layout", overrides)


def _section_captured(overrides: dict[str, str], _bt: ButtonTokens | None) -> bool:
    """Section is captured when any section slot is brand-supplied."""
    return _any_slot_in_group("section", overrides)


def _motion_captured(overrides: dict[str, str], _bt: ButtonTokens | None) -> bool:
    """Motion is captured when any motion slot is brand-supplied."""
    return _any_slot_in_group("motion", overrides)


def _shadow_captured(overrides: dict[str, str], _bt: ButtonTokens | None) -> bool:
    """Shadow is captured when any shadow slot is brand-supplied."""
    return _any_slot_in_group("shadow", overrides)


def _button_captured(overrides: dict[str, str], bt: ButtonTokens | None) -> bool:
    """Button is captured when a computed-style snapshot exists OR the core
    geometry slots are brand-supplied.

    Two paths:
    1. ``button_tokens`` is not None - a real computed-style snapshot was taken
       for this brand. This is the highest-fidelity signal (R3.1 path B).
    2. Core geometry slots in the token bag: ds-button-padding-y AND
       ds-button-padding-x AND ds-button-border-width. Requiring all three
       prevents a single-field coincidence from triggering a false capture;
       together they define the button's spatial footprint.

    Rationale for the geometry threshold (tunable by Opus review): button shape
    is defined by padding (the spatial footprint), border (the visual boundary),
    and radius (inherited from the scale if not explicit). Requiring padding + border
    without radius is intentionally lenient - a brand that only specifies
    ds-button-radius but not the padding is likely inheriting from the scale and
    has not captured a real button shape.
    """
    if bt is not None:
        return True
    core_geometry = {"ds-button-padding-y", "ds-button-padding-x", "ds-button-border-width"}
    return core_geometry.issubset(overrides.keys())


def _card_captured(overrides: dict[str, str], _bt: ButtonTokens | None) -> bool:
    """Card is captured when border-width AND at least one padding slot are brand-supplied.

    Card shape is defined by its border (visual boundary) and its interior
    padding (spatial footprint). A brand that supplies both has real card geometry.
    """
    has_border = "ds-card-border-width" in overrides
    has_padding = bool({"ds-card-padding", "ds-card-padding-y"} & overrides.keys())
    return has_border and has_padding


def _badge_captured(overrides: dict[str, str], _bt: ButtonTokens | None) -> bool:
    """Badge is captured when both padding slots are brand-supplied.

    Badge chips are defined primarily by their padding (the pill's spatial
    footprint). Both y and x are required to avoid a partial match triggering
    a false positive.
    """
    return bool({"ds-badge-padding-y", "ds-badge-padding-x"} <= overrides.keys())


def _input_captured(overrides: dict[str, str], _bt: ButtonTokens | None) -> bool:
    """Input is captured when padding-y AND border-width are brand-supplied.

    Input fields are defined by their interior padding and their border. A
    brand that supplies both has a real input geometry distinct from defaults.
    """
    return bool({"ds-input-padding-y", "ds-input-border-width"} <= overrides.keys())


# Dispatch table: component_group -> capture rule function.
# Adding a new group requires both a rule function and an entry here.
_CAPTURE_RULES: dict[str, Callable[[dict[str, str], ButtonTokens | None], bool]] = {
    "badge": _badge_captured,
    "button": _button_captured,
    "card": _card_captured,
    "color": _color_captured,
    "input": _input_captured,
    "layout": _layout_captured,
    "motion": _motion_captured,
    "radius": _radius_captured,
    "section": _section_captured,
    "shadow": _shadow_captured,
    "spacing": _spacing_captured,
    "typography": _typography_captured,
}


# ---------------------------------------------------------------------------
# Source-field presence helpers
# ---------------------------------------------------------------------------

def _group_source_field_status(
    group: str, overrides: dict[str, str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (present, absent) source_field tuples for a contract group.

    Iterates the contract slots for ``group`` and checks whether each slot's
    canonical slot name is in ``overrides``. Returns source_field strings
    (e.g. 'button.padding-y') not slot names (e.g. 'ds-button-padding-y')
    so the output reads as the DATA_CONTRACT_HANDOFF spec format.
    """
    present: list[str] = []
    absent: list[str] = []
    for slot_name in slots_for_group(group):
        slot = BRAND_TOKEN_CONTRACT["slots"][slot_name]
        source_field = slot["source_field"]
        if slot_name in overrides:
            present.append(source_field)
        else:
            absent.append(source_field)
    return tuple(sorted(present)), tuple(sorted(absent))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_capture_manifest(
    tokens: dict[str, str],
    *,
    button_tokens: ButtonTokens | None = None,
) -> BrandCaptureManifest:
    """Build a BrandCaptureManifest for the given brand token bag.

    Pure: no I/O, no side effects. Deterministic: same inputs -> same output.

    Args:
        tokens: flat ``{key: value}`` dict as returned by
            ``library_indexer.tokens_for_compose``. Keys may be in any format
            (DRL seed ``ds-*``, organic bare, underscored). Values are CSS
            strings.
        button_tokens: the brand's R3.1 ``ButtonTokens`` snapshot if one exists
            on disk, else None. A non-None value captures the button group
            regardless of whether button geometry slots appear in ``tokens``.

    Returns:
        A ``BrandCaptureManifest`` with ``schema_version`` and per-group detail.

    Example:
        >>> manifest = build_capture_manifest({"ds-bg": "#fff", "ds-accent": "#0f0", "ds-text": "#000"})
        >>> manifest["groups"]["color"]["captured"]
        True
        >>> manifest["groups"]["button"]["captured"]
        False
    """
    overrides = _build_overrides(tokens)
    groups: dict[str, GroupCaptureDetail] = {}
    for group in COMPONENT_GROUPS:
        rule = _CAPTURE_RULES[group]
        captured = rule(overrides, button_tokens)
        present, absent = _group_source_field_status(group, overrides)
        groups[group] = GroupCaptureDetail(
            captured=captured,
            present_source_fields=present,
            absent_source_fields=absent,
        )
    return BrandCaptureManifest(
        schema_version=CAPTURE_MANIFEST_SCHEMA_VERSION,
        groups=groups,
    )


def captured_group_names(manifest: BrandCaptureManifest) -> tuple[str, ...]:
    """Return the names of groups where ``captured=True``, sorted."""
    return tuple(sorted(
        group for group, detail in manifest["groups"].items()
        if detail["captured"]
    ))


def uncaptured_group_names(manifest: BrandCaptureManifest) -> tuple[str, ...]:
    """Return the names of groups where ``captured=False``, sorted."""
    return tuple(sorted(
        group for group, detail in manifest["groups"].items()
        if not detail["captured"]
    ))

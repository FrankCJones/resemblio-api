"""Path C token contract: every --ds-* slot the DRL templates consume.

The contract is data, not code. ``BRAND_TOKEN_CONTRACT`` enumerates every
custom property a templates.py block references; each entry carries its
default value (which doubles as the ``var()`` fallback in the rewritten
template) and the brand-JSON source field that supplies a real brand's
value at compose time.

Reference: ``projects/OptSus Team/cto-reviews/2026-06-03-resemblio-path-c-tokens-to-vars.md``
Architectural lesson:
    ``projects/OptSus Team/miner-output/candidate-skills/2026-06-02-late-sweep/brand-json-as-css-vars-not-per-page-override.md``

The shape
---------
``TokenContract`` is a TypedDict with two keys: ``schema_version`` (string
sentinel; bump when the slot inventory changes shape, not when values
change) and ``slots`` (mapping of slot name -> ``TokenSlot``).

Each ``TokenSlot`` carries:

- ``default``: the CSS value baked into ``templates.py`` today; preserved
  as the ``var()`` fallback so any template still renders correctly when
  a brand JSON is missing a slot. Back-compat invariant: an empty token
  bag produces byte-identical CSS to today.
- ``source_field``: the brand-JSON path that supplies this slot's value
  (e.g. ``"button.radius"``, ``"radius.full"``, ``"font.weight.display"``).
  This is the contract the brand JSON expansion (Phase 3) honors.
- ``component_group``: coarse grouping (``"spacing"``, ``"radius"``,
  ``"button"``, ``"card"``, ``"badge"``, ``"input"``, ``"section"``,
  ``"layout"``, ``"typography"``, ``"motion"``, ``"shadow"``, ``"color"``).
  Used by the inventory test and by Phase 3 derivation modules to know
  which slots a derivation function is allowed to populate.
- ``docs``: one-line explanation. Read by the inventory generator; should
  read as English, not as a slot-name restatement.

Lookup helpers
--------------
``slot_default(name)`` returns the CSS value to use as the ``var()``
fallback. ``slots_for_group(group)`` returns the subset for a component
group, used by Phase 3 derivation modules. Both are deterministic; callers
that need stable ordering should sort the result.
"""
from __future__ import annotations

from typing import TypedDict

# Sentinel; bump when the slot inventory shape changes (adding a key to
# TokenSlot, for instance). Adding or removing slots inside the existing
# shape is NOT a schema-version bump; tests cover slot presence directly.
TOKEN_CONTRACT_SCHEMA_VERSION = "token_contract_v1"


class TokenSlot(TypedDict):
    """One --ds-* slot the templates consume."""

    default: str
    source_field: str
    component_group: str
    docs: str


class TokenContract(TypedDict):
    """The full inventory of --ds-* slots, plus a schema-version sentinel."""

    schema_version: str
    slots: dict[str, TokenSlot]


def _slot(default: str, source_field: str, component_group: str, docs: str) -> TokenSlot:
    """Compact constructor so the contract reads as a table, not a dict-of-dicts."""
    return {
        "default": default,
        "source_field": source_field,
        "component_group": component_group,
        "docs": docs,
    }


# ---------------------------------------------------------------------------
# The contract.
#
# Sources: every hardcoded numeric/length value present in
# ``_vendored/drl/drl/_scripts/templates.py`` as of the Path C audit
# (see Section 1 of the CTO packet). Color slots (existing today) are
# included so a single contract drives everything; Phase 2 leaves their
# templates.py references untouched (they are already var()-wrapped).
# ---------------------------------------------------------------------------

BRAND_TOKEN_CONTRACT: TokenContract = {
    "schema_version": TOKEN_CONTRACT_SCHEMA_VERSION,
    "slots": {
        # ----- Spacing scale (cross-cutting) ---------------------------
        "ds-space-1": _slot("4px", "space.1", "spacing", "Base spacing unit."),
        "ds-space-2": _slot("8px", "space.2", "spacing", "2x base; tight gaps."),
        "ds-space-3": _slot("12px", "space.3", "spacing", "3x base; small gaps + paddings."),
        "ds-space-4": _slot("16px", "space.4", "spacing", "4x base; default item gap."),
        "ds-space-5": _slot("20px", "space.5", "spacing", "5x base."),
        "ds-space-6": _slot("24px", "space.6", "spacing", "6x base; card padding default."),
        "ds-space-8": _slot("32px", "space.8", "spacing", "8x base; section side padding default."),
        "ds-space-12": _slot("48px", "space.12", "spacing", "12x base; head-block bottom margin."),
        "ds-space-16": _slot("64px", "space.16", "spacing", "16x base; section rhythm."),
        "ds-space-24": _slot("96px", "space.24", "spacing", "24x base; page padding default."),

        # ----- Radius family -------------------------------------------
        "ds-radius-xs": _slot("4px", "radius.xs", "radius", "Smallest radius; chips, segs."),
        "ds-radius-sm": _slot("6px", "radius.sm", "radius", "Small radius; default for buttons + inputs."),
        "ds-radius-md": _slot("8px", "radius.md", "radius", "Medium radius; cards, tiles."),
        "ds-radius-lg": _slot("12px", "radius.lg", "radius", "Large radius; larger surfaces."),
        "ds-radius-full": _slot("9999px", "radius.full", "radius", "Pill / circle radius; badges, avatars."),
        "ds-radius-button": _slot(
            "var(--ds-radius-sm, 6px)", "radius.button", "radius",
            "Per-component button radius; brands override for pill buttons.",
        ),
        "ds-radius-card": _slot(
            "var(--ds-radius-md, 8px)", "radius.card", "radius",
            "Per-component card radius.",
        ),
        "ds-radius-input": _slot(
            "var(--ds-radius-sm, 6px)", "radius.input", "radius",
            "Per-component input radius.",
        ),
        "ds-radius-badge": _slot(
            "var(--ds-radius-full, 9999px)", "radius.badge", "radius",
            "Per-component badge radius; default is pill.",
        ),

        # ----- Buttons -------------------------------------------------
        "ds-button-radius": _slot(
            "var(--ds-radius-button, var(--ds-radius-sm, 6px))",
            "button.radius", "button",
            "Button corner radius. Chains through component alias to family.",
        ),
        "ds-button-padding-y": _slot("10px", "button.padding-y", "button", "Button block padding (default size)."),
        "ds-button-padding-x": _slot("16px", "button.padding-x", "button", "Button inline padding (default size)."),
        "ds-button-font-size": _slot(
            "var(--ds-text-sm)", "button.font-size", "button",
            "Button label font size.",
        ),
        "ds-button-font-weight": _slot("500", "button.font-weight", "button", "Button label font weight."),
        "ds-button-font-family": _slot(
            "var(--ds-font-body)", "button.font-family", "button",
            "Button label font family.",
        ),
        "ds-button-border-width": _slot("1px", "button.border-width", "button", "Button border width."),
        "ds-button-sm-padding-y": _slot("6px", "button.sm.padding-y", "button", "Small button block padding."),
        "ds-button-sm-padding-x": _slot("12px", "button.sm.padding-x", "button", "Small button inline padding."),
        "ds-button-lg-padding-y": _slot("14px", "button.lg.padding-y", "button", "Large button block padding."),
        "ds-button-lg-padding-x": _slot("22px", "button.lg.padding-x", "button", "Large button inline padding."),

        # ----- Cards ---------------------------------------------------
        "ds-card-radius": _slot(
            "var(--ds-radius-card, var(--ds-radius-md, 8px))",
            "card.radius", "card",
            "Card corner radius.",
        ),
        "ds-card-padding": _slot("24px", "card.padding", "card", "Card padding (shorthand)."),
        "ds-card-padding-y": _slot("24px", "card.padding-y", "card", "Card block padding."),
        "ds-card-padding-x": _slot("24px", "card.padding-x", "card", "Card inline padding."),
        "ds-card-border-width": _slot("1px", "card.border-width", "card", "Card border width."),
        "ds-card-gap": _slot("12px", "card.gap", "card", "Inner card flex/grid gap."),
        "ds-card-grid-gap": _slot("20px", "card.grid-gap", "card", "Card grid container gap."),

        # ----- Badges / pills ------------------------------------------
        "ds-badge-radius": _slot(
            "var(--ds-radius-badge, var(--ds-radius-full, 9999px))",
            "badge.radius", "badge",
            "Badge corner radius. Pill by default.",
        ),
        "ds-badge-padding-y": _slot("3px", "badge.padding-y", "badge", "Badge block padding (default size)."),
        "ds-badge-padding-x": _slot("10px", "badge.padding-x", "badge", "Badge inline padding (default size)."),
        "ds-badge-font-size": _slot(
            "var(--ds-text-xs)", "badge.font-size", "badge",
            "Badge label font size.",
        ),
        "ds-badge-font-weight": _slot("500", "badge.font-weight", "badge", "Badge label font weight."),
        "ds-badge-border-width": _slot("1px", "badge.border-width", "badge", "Badge border width."),
        "ds-badge-sm-padding-y": _slot("2px", "badge.sm.padding-y", "badge", "Small badge block padding."),
        "ds-badge-sm-padding-x": _slot("8px", "badge.sm.padding-x", "badge", "Small badge inline padding."),
        "ds-badge-lg-padding-y": _slot("5px", "badge.lg.padding-y", "badge", "Large badge block padding."),
        "ds-badge-lg-padding-x": _slot("12px", "badge.lg.padding-x", "badge", "Large badge inline padding."),

        # ----- Inputs / form fields ------------------------------------
        "ds-input-radius": _slot(
            "var(--ds-radius-input, var(--ds-radius-sm, 6px))",
            "input.radius", "input",
            "Input corner radius.",
        ),
        "ds-input-padding-y": _slot("10px", "input.padding-y", "input", "Input block padding."),
        "ds-input-padding-x": _slot("12px", "input.padding-x", "input", "Input inline padding."),
        "ds-input-font-size": _slot(
            "var(--ds-text-base)", "input.font-size", "input",
            "Input text font size.",
        ),
        "ds-input-font-family": _slot(
            "var(--ds-font-body)", "input.font-family", "input",
            "Input text font family.",
        ),
        "ds-input-border-width": _slot("1px", "input.border-width", "input", "Input border width."),
        "ds-input-line-height": _slot("1.4", "input.line-height", "input", "Input line-height."),

        # ----- Section / layout ----------------------------------------
        "ds-section-padding-y": _slot("96px", "section.padding-y", "section", "Section vertical padding."),
        "ds-section-padding-x": _slot("32px", "section.padding-x", "section", "Section horizontal padding."),
        "ds-section-divider-width": _slot("1px", "section.divider-width", "section", "Section top divider width."),
        "ds-page-max": _slot("880px", "layout.page-max", "layout", "Default page max-width."),
        "ds-page-max-narrow": _slot("720px", "layout.page-max-narrow", "layout", "Narrow page wrappers (articles, forms)."),
        "ds-page-max-default": _slot("880px", "layout.page-max-default", "layout", "Default page max-width."),
        "ds-page-max-wide": _slot("1100px", "layout.page-max-wide", "layout", "Wider page wrappers."),
        "ds-page-max-full": _slot("1200px", "layout.page-max-full", "layout", "Widest page wrappers (nav, footer)."),
        "ds-page-pad-y": _slot("96px", "layout.page-pad-y", "layout", "Page top/bottom padding."),
        "ds-page-pad-x": _slot("32px", "layout.page-pad-x", "layout", "Page side padding."),

        # ----- Typography weights + tracking ---------------------------
        "ds-font-weight-display": _slot("600", "font.weight.display", "typography", "Display-typeface weight."),
        "ds-font-weight-body": _slot("400", "font.weight.body", "typography", "Body-typeface weight."),
        "ds-font-weight-medium": _slot("500", "font.weight.medium", "typography", "Medium emphasis weight."),
        "ds-tracking-tight": _slot("-0.02em", "tracking.tight", "typography", "Tightest letter-spacing (display)."),
        "ds-tracking-snug": _slot("-0.018em", "tracking.snug", "typography", "Snug letter-spacing."),
        "ds-tracking-normal": _slot("0", "tracking.normal", "typography", "Default letter-spacing."),
        "ds-tracking-wide": _slot("0.06em", "tracking.wide", "typography", "Wide letter-spacing (kickers, eyebrows)."),
        "ds-tracking-wider": _slot("0.08em", "tracking.wider", "typography", "Wider letter-spacing (large kickers)."),

        # ----- Motion + ease + shadow ----------------------------------
        "ds-duration-fast": _slot("150ms", "motion.duration-fast", "motion", "Fast transition duration."),
        "ds-duration-base": _slot("250ms", "motion.duration-base", "motion", "Base transition duration."),
        "ds-ease-standard": _slot("ease", "motion.ease-standard", "motion", "Standard easing curve."),
        "ds-shadow-xs": _slot(
            "0 1px 1px rgba(0,0,0,0.06)", "shadow.xs", "shadow",
            "Hairline shadow.",
        ),
        "ds-shadow-sm": _slot(
            "0 1px 3px rgba(0,0,0,0.08)", "shadow.sm", "shadow",
            "Small elevation shadow.",
        ),
        "ds-shadow-md": _slot(
            "0 4px 12px rgba(0,0,0,0.10)", "shadow.md", "shadow",
            "Medium elevation shadow.",
        ),

        # ----- Colors (already var-wrapped in templates today) ---------
        # No template rewrite required; included so the contract is the
        # single source of truth for "what slot does this brand JSON key
        # populate?" and so Phase 3 derivation modules can target them.
        "ds-bg": _slot("#ffffff", "color.bg", "color", "Page background."),
        "ds-surface": _slot("#fafafa", "color.surface", "color", "Surface color (cards, panels)."),
        "ds-surface-2": _slot("#f3f3f3", "color.surface-2", "color", "Secondary surface."),
        "ds-text": _slot("#111111", "color.text", "color", "Default text color."),
        "ds-text-muted": _slot("#666666", "color.text-muted", "color", "Muted text color."),
        "ds-border": _slot("#e5e5e5", "color.border", "color", "Default border."),
        "ds-hairline": _slot("#eeeeee", "color.hairline", "color", "Hairline divider."),
        "ds-accent": _slot("#0070f3", "color.accent", "color", "Primary accent / brand color."),
        "ds-accent-2": _slot(
            "var(--ds-surface-2, var(--ds-surface))", "color.accent-2", "color",
            "Secondary accent.",
        ),
        "ds-focus-ring": _slot(
            "var(--ds-accent)", "color.focus-ring", "color",
            "Focus-ring color.",
        ),
        "ds-info": _slot("#3b82f6", "color.info", "color", "Info semantic color."),
        "ds-success": _slot("#22c55e", "color.success", "color", "Success semantic color."),
        "ds-warning": _slot("#f59e0b", "color.warning", "color", "Warning semantic color."),
        "ds-error": _slot("#ef4444", "color.error", "color", "Error semantic color."),
    },
}


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


def slot_default(name: str) -> str:
    """Return the CSS value to use as the ``var()`` fallback for ``name``.

    ``name`` is the slot key WITHOUT the leading ``--`` (e.g. ``"ds-bg"``).
    Raises ``KeyError`` if the slot is not in the contract; callers asking
    for an unknown slot are bugs, not silent fallthroughs.
    """
    return BRAND_TOKEN_CONTRACT["slots"][name]["default"]


def slots_for_group(group: str) -> tuple[str, ...]:
    """Return slot names whose ``component_group`` equals ``group``, sorted."""
    matched = [
        name
        for name, slot in BRAND_TOKEN_CONTRACT["slots"].items()
        if slot["component_group"] == group
    ]
    return tuple(sorted(matched))


def all_slot_names() -> tuple[str, ...]:
    """Return every slot name in deterministic sorted order."""
    return tuple(sorted(BRAND_TOKEN_CONTRACT["slots"].keys()))

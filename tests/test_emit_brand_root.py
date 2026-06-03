"""Per-slot population tests for ``_emit_brand_root``.

Path C Phase 2 (per CTO sign-off
``projects/OptSus Team/cto-reviews/2026-06-03-resemblio-path-c-phase2-contract-signoff.md``):
the new emitter must populate every contract slot, prefer brand-supplied
values over contract defaults, pass extra brand keys through, and emit
deterministically. These tests pin those guarantees independent of the
templates.py rewrite.
"""
from __future__ import annotations

from app.library_indexer import _emit_brand_root
from extractor.token_contract import BRAND_TOKEN_CONTRACT


def test_empty_tokens_emit_every_contract_slot() -> None:
    """No brand override -> every contract slot present with its default."""
    css = _emit_brand_root({})
    for slot_name, slot in BRAND_TOKEN_CONTRACT["slots"].items():
        assert f"--{slot_name}: {slot['default']};" in css


def test_partial_brand_override_only_replaces_named_slots() -> None:
    """Brand supplies two slots -> those two replace, all others stay at default."""
    css = _emit_brand_root({"ds-bg": "#fafafa", "ds-accent": "#ff00aa"})
    assert "--ds-bg: #fafafa;" in css
    assert "--ds-accent: #ff00aa;" in css
    # An unrelated slot still holds its contract default.
    text_default = BRAND_TOKEN_CONTRACT["slots"]["ds-text"]["default"]
    assert f"--ds-text: {text_default};" in css


def test_already_namespaced_brand_key_overrides_correctly() -> None:
    """Brand key supplied with the ``ds-`` prefix overrides its matching slot."""
    css = _emit_brand_root({"ds-accent": "#abcdef"})
    assert "--ds-accent: #abcdef;" in css
    # The contract default for ds-accent must NOT also appear.
    default_value = BRAND_TOKEN_CONTRACT["slots"]["ds-accent"]["default"]
    assert f"--ds-accent: {default_value};" not in css


def test_bare_brand_key_normalizes_then_overrides() -> None:
    """Brand key supplied without the ``ds-`` prefix still hits the right slot."""
    css = _emit_brand_root({"bg": "#000000"})
    assert "--ds-bg: #000000;" in css


def test_underscored_brand_key_normalizes_dashes() -> None:
    """``font_weight_display`` (underscore form) overrides ``--ds-font-weight-display``."""
    css = _emit_brand_root({"font_weight_display": "700"})
    assert "--ds-font-weight-display: 700;" in css


def test_extra_brand_key_passes_through_alongside_contract_slots() -> None:
    """Brand-supplied key not in the contract (e.g. font-body) still emits.

    Keeps existing DRL templates that reference ``var(--ds-font-body)``
    resolving even though the contract has not formally adopted
    font-family slots yet.
    """
    css = _emit_brand_root({"ds-font-body": "Georgia, serif"})
    assert "--ds-font-body: Georgia, serif;" in css


def test_output_is_a_single_root_block() -> None:
    """Output is one ``:root { ... }`` block (scope_style_block scopes it downstream)."""
    css = _emit_brand_root({})
    assert css.startswith(":root {")
    assert css.endswith("}")
    # No nested braces.
    assert css.count("{") == 1
    assert css.count("}") == 1


def test_output_lines_are_sorted_by_slot_name() -> None:
    """Slot declarations emit in sorted order within the contract-slots section."""
    css = _emit_brand_root({})
    # Pick three well-known contract slots that are alphabetically ordered.
    bg_pos = css.index("--ds-bg:")
    surface_pos = css.index("--ds-surface:")
    text_pos = css.index("--ds-text:")
    assert bg_pos < surface_pos < text_pos

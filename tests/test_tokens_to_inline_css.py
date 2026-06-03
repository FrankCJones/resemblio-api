"""Tests for ``_tokens_to_inline_css`` + ``_ds_var_name`` token-key normalization.

Root-cause regression coverage for the 2026-06-02 library visual-fidelity
audit: ``scripts/seed_from_drl.py:parse_tokens_css`` captures the full token
identifier including the ``ds-`` namespace prefix, so keys arrive as
``ds-bg`` rather than ``bg``. The previous CSS emitter blindly prefixed
``--ds-`` to every key and produced ``--ds-ds-bg``, which DRL templates do
not reference; every brand variable fell through to browser defaults.

These tests pin the normalization contract so the bug cannot silently
reappear: both bare and already-namespaced keys must produce a single
``--ds-<name>`` form.
"""
from __future__ import annotations

from app.library_indexer import _ds_var_name, _tokens_to_inline_css


def test_already_namespaced_key_is_not_double_prefixed() -> None:
    """``ds-bg`` must become ``--ds-bg``, NOT ``--ds-ds-bg`` (root-cause regression)."""
    css = _tokens_to_inline_css({"ds-bg": "#fff"})
    assert "--ds-bg: #fff;" in css
    assert "--ds-ds-bg" not in css


def test_bare_key_gets_ds_prefix() -> None:
    """``bg`` (no namespace) must become ``--ds-bg`` (backward compatible)."""
    css = _tokens_to_inline_css({"bg": "#000"})
    assert "--ds-bg: #000;" in css


def test_namespaced_dashed_key() -> None:
    """``ds-font-display`` must become ``--ds-font-display`` (no double prefix)."""
    assert _ds_var_name("ds-font-display") == "--ds-font-display"


def test_bare_underscored_key_normalizes_underscores() -> None:
    """``font_display`` must become ``--ds-font-display`` (underscore -> dash + prefix)."""
    assert _ds_var_name("font_display") == "--ds-font-display"


def test_namespaced_underscored_key() -> None:
    """``ds_bg`` normalizes underscores BEFORE the prefix check (``ds-bg`` -> ``--ds-bg``)."""
    assert _ds_var_name("ds_bg") == "--ds-bg"


def test_empty_tokens_dict_returns_contract_default_block() -> None:
    """No brand tokens -> ``:root`` block populated with contract defaults.

    Path C Phase 2 (CTO sign-off 2026-06-03): the emitter is no longer a
    raw projection of the brand dict; it always emits every contract slot
    so templates have a defined value to resolve. With an empty brand
    dict every slot pulls its ``default`` from ``BRAND_TOKEN_CONTRACT``.
    """
    from extractor.token_contract import BRAND_TOKEN_CONTRACT

    css = _tokens_to_inline_css({})
    assert css.startswith(":root {")
    assert css.endswith("}")
    # At least one well-known slot with its contract default.
    bg_default = BRAND_TOKEN_CONTRACT["slots"]["ds-bg"]["default"]
    assert f"--ds-bg: {bg_default};" in css


def test_mixed_keys_produce_no_double_prefix() -> None:
    """Mixed bare + namespaced keys all collapse to single ``--ds-*`` form."""
    css = _tokens_to_inline_css(
        {
            "ds-bg": "#111",
            "text": "#eee",
            "ds-font-display": "Inter, sans-serif",
            "font_body": "Georgia, serif",
        }
    )
    assert "--ds-bg: #111;" in css
    assert "--ds-text: #eee;" in css
    assert "--ds-font-display: Inter, sans-serif;" in css
    assert "--ds-font-body: Georgia, serif;" in css
    assert "--ds-ds-" not in css


def test_sorted_output_is_deterministic() -> None:
    """Tokens emit in sorted-key order so library_pages.rendered_html diffs stay reviewable."""
    css = _tokens_to_inline_css({"ds-z": "1", "ds-a": "2", "ds-m": "3"})
    a_pos = css.index("--ds-a")
    m_pos = css.index("--ds-m")
    z_pos = css.index("--ds-z")
    assert a_pos < m_pos < z_pos

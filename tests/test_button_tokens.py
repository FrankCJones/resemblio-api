"""Pure-data tests for `extractor.button_tokens.derive_button_tokens`.

Hybrid Path B fidelity fix per CTO decision packet
`projects/OptSus Team/cto-reviews/2026-06-02-resemblio-button-fidelity-fix.md`.

Layer 1 of the three-layer TDD shape: assert the derivation reads the
right values from a synthetic ComputedStyleReport with no real browser
involved. The Apple fixture is the headline case (980px pill + 17px SF
Pro 400). The synthetic chiclet case covers a small-radius brand. The
None-return cases pin the graceful-degrade contract.
"""
from __future__ import annotations

import json
from pathlib import Path

from extractor.button_tokens import (
    SCHEMA_VERSION,
    BUTTON_TOKEN_KEYS,
    ButtonTokens,
    derive_button_tokens,
)
from extractor.computed_styles import ComputedStyleReport, empty_report

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "button_fidelity"


def _load_apple_report() -> ComputedStyleReport:
    """Load the Apple computed-styles fixture as a ComputedStyleReport."""
    raw = json.loads((FIXTURE_DIR / "apple_computed.json").read_text(encoding="utf-8"))
    # Drop the `_README` key the fixture file carries for human review.
    raw.pop("_README", None)
    return raw  # type: ignore[return-value]


def _synthetic_report(cta_props: dict[str, str]) -> ComputedStyleReport:
    """Return a minimal report with one `cta` slot carrying `cta_props`."""
    return ComputedStyleReport(
        status="ok",
        signals=[
            {
                "slot": "cta",
                "selector": "button, .cta, [role=button]",
                "properties": cta_props,
            }
        ],
        error=None,
        schema_version=1,
    )


# ---------------------------------------------------------------------------
# The headline case: Apple's pill propagates byte-for-byte.
# ---------------------------------------------------------------------------


def test_derives_apple_pill_from_fixture() -> None:
    tokens = derive_button_tokens(_load_apple_report())
    assert tokens is not None
    assert tokens["border_radius"] == "980px"
    assert tokens["padding"] == "17px 28px"
    assert tokens["padding_block"] == "17px"
    assert tokens["padding_inline"] == "28px"
    assert tokens["font_size"] == "17px"
    assert tokens["font_weight"] == "400"
    assert "SF Pro Text" in tokens["font_family"]
    assert tokens["background_color"] == "rgb(0, 113, 227)"
    assert tokens["color"] == "rgb(255, 255, 255)"
    # `0px none ...` should collapse to a zero border.
    assert tokens["border_width"] == "0px"
    assert tokens["schema_version"] == SCHEMA_VERSION


def test_pill_radius_predicate() -> None:
    """Sanity guard: the 'is it a pill?' check parses the Apple radius cleanly."""
    tokens = derive_button_tokens(_load_apple_report())
    assert tokens is not None
    # Extract the leading numeric component for the >=100px assertion.
    px_value = float(tokens["border_radius"].rstrip("px"))
    assert px_value >= 100.0, "Apple's radius failed the pill predicate"


# ---------------------------------------------------------------------------
# Chiclet brand: small radius propagates verbatim with no clamping.
# ---------------------------------------------------------------------------


def test_handles_chiclet_brand() -> None:
    tokens = derive_button_tokens(
        _synthetic_report(
            {
                "color": "rgb(255, 255, 255)",
                "background-color": "rgb(13, 110, 253)",
                "font-family": "Inter, system-ui, sans-serif",
                "font-size": "14px",
                "font-weight": "500",
                "padding": "10px 16px",
                "border-radius": "6px",
                "border": "1px solid rgb(13, 110, 253)",
            }
        )
    )
    assert tokens is not None
    assert tokens["border_radius"] == "6px"  # no opinion, no clamping
    assert tokens["padding"] == "10px 16px"
    assert tokens["padding_block"] == "10px"
    assert tokens["padding_inline"] == "16px"
    assert tokens["font_size"] == "14px"
    assert tokens["font_weight"] == "500"
    assert tokens["border_width"] == "1px"


# ---------------------------------------------------------------------------
# Graceful-degrade contract: None on unusable input.
# ---------------------------------------------------------------------------


def test_returns_none_on_unavailable_report() -> None:
    report = empty_report("unavailable", "playwright is not installed")
    assert derive_button_tokens(report) is None


def test_returns_none_on_error_report() -> None:
    report = empty_report("error", "navigation failure")
    assert derive_button_tokens(report) is None


def test_returns_none_on_skipped_report() -> None:
    report = empty_report("skipped", "disabled by env")
    assert derive_button_tokens(report) is None


def test_returns_none_on_missing_cta_slot() -> None:
    report = ComputedStyleReport(
        status="ok",
        signals=[
            {
                "slot": "body",
                "selector": "body",
                "properties": {"color": "rgb(0, 0, 0)"},
            }
        ],
        error=None,
        schema_version=1,
    )
    assert derive_button_tokens(report) is None


def test_returns_none_on_empty_signals() -> None:
    report = ComputedStyleReport(
        status="ok", signals=[], error=None, schema_version=1
    )
    assert derive_button_tokens(report) is None


# ---------------------------------------------------------------------------
# Padding-shorthand split: 1/2/3/4-value forms.
# ---------------------------------------------------------------------------


def test_padding_split_single_value() -> None:
    tokens = derive_button_tokens(_synthetic_report({"padding": "10px"}))
    assert tokens is not None
    assert tokens["padding_block"] == "10px"
    assert tokens["padding_inline"] == "10px"


def test_padding_split_two_value() -> None:
    tokens = derive_button_tokens(_synthetic_report({"padding": "17px 28px"}))
    assert tokens is not None
    assert tokens["padding_block"] == "17px"
    assert tokens["padding_inline"] == "28px"


def test_padding_split_three_value() -> None:
    tokens = derive_button_tokens(_synthetic_report({"padding": "10px 20px 30px"}))
    assert tokens is not None
    assert tokens["padding_block"] == "10px"
    assert tokens["padding_inline"] == "20px"


def test_padding_split_four_value() -> None:
    tokens = derive_button_tokens(
        _synthetic_report({"padding": "10px 20px 30px 40px"})
    )
    assert tokens is not None
    assert tokens["padding_block"] == "10px"
    assert tokens["padding_inline"] == "20px"


def test_padding_split_missing_returns_empty_strings() -> None:
    tokens = derive_button_tokens(_synthetic_report({"color": "rgb(0,0,0)"}))
    assert tokens is not None
    assert tokens["padding"] == ""
    assert tokens["padding_block"] == ""
    assert tokens["padding_inline"] == ""


# ---------------------------------------------------------------------------
# Border-shorthand parsing.
# ---------------------------------------------------------------------------


def test_border_none_collapses_to_zero() -> None:
    tokens = derive_button_tokens(
        _synthetic_report({"border": "0px none rgb(255, 255, 255)"})
    )
    assert tokens is not None
    assert tokens["border_width"] == "0px"


def test_border_solid_picks_up_width() -> None:
    tokens = derive_button_tokens(
        _synthetic_report({"border": "2px solid rgb(0, 0, 0)"})
    )
    assert tokens is not None
    assert tokens["border_width"] == "2px"


def test_border_missing_defaults_to_zero() -> None:
    tokens = derive_button_tokens(_synthetic_report({"color": "rgb(0,0,0)"}))
    assert tokens is not None
    assert tokens["border_width"] == "0px"


# ---------------------------------------------------------------------------
# Contract surface: schema_version + BUTTON_TOKEN_KEYS shape.
# ---------------------------------------------------------------------------


def test_schema_version_stamped() -> None:
    tokens = derive_button_tokens(_load_apple_report())
    assert tokens is not None
    assert tokens["schema_version"] == SCHEMA_VERSION


def test_button_token_keys_contract_is_seven_slots() -> None:
    """The seven `--ds-button-*` slots are the cross-project contract."""
    assert len(BUTTON_TOKEN_KEYS) == 7
    assert all(k.startswith("--ds-button-") for k in BUTTON_TOKEN_KEYS)

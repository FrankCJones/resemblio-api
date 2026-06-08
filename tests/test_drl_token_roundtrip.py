"""Phase 2 regression suite: all DRL token slots survive seed -> compose -> emit.

Gap A was the suspicion that shadow, motion, and tracking slots from the DRL
``tokens.css`` were being dropped before they reached the rendered page. These
tests prove the full round-trip is correct for every slot category.

The DRL token contract has more slots than the Resemblio ``BRAND_TOKEN_CONTRACT``:

- DRL shadow: ds-shadow-none, xs, sm, md, lg, 2xl  (6 slots)
- Resemblio:  ds-shadow-xs, sm, md                  (3 contract slots)
- Outcome: xs/sm/md use brand values via contract path; none/lg/2xl emit via
  the pass-through extras path in ``_emit_brand_root``.

- DRL motion: 4 eases + 4 durations                (8 slots)
  Note: DRL uses ``ds-duration-normal`` (240ms) but Resemblio contract uses
  ``ds-duration-base``. DRL's ``ds-duration-normal`` emits as an extra (not a
  contract slot); ``ds-duration-base`` slot uses the contract default because
  no DRL source maps a value to that name.
- Resemblio:  ds-duration-fast, ds-duration-base, ds-ease-standard (3 slots)
- Outcome: brand values emitted for all DRL motion slots they supply.

- DRL tracking: tight, normal, wide, wider         (4 slots)
- Resemblio:  tight, snug, normal, wide, wider      (5 slots; snug is Resemblio-only)
- Outcome: brand values emitted for all 4 DRL tracking slots; snug uses default.

These tests use a synthetic token dict mirroring a real DRL alphabet
(all ds-* slots present). No DB or R2 dependency.
"""
from __future__ import annotations

from app.library_indexer import _emit_brand_root, tokens_for_compose

# ---------------------------------------------------------------------------
# Synthetic DRL alphabet: all slot categories represented
# ---------------------------------------------------------------------------

# Realistic brand values for a dark-mode B2B tool (mimics Linear's values)
_FULL_DRL_TOKENS: dict[str, str] = {
    # Colors
    "ds-bg": "#0A0A0B",
    "ds-surface": "#111114",
    "ds-surface-raised": "#1A1A1F",
    "ds-text": "#E8E8EC",
    "ds-text-muted": "#8B8B99",
    "ds-accent": "#5E6AD2",
    "ds-accent-text": "#FFFFFF",
    "ds-border": "#2D2D35",
    "ds-border-strong": "#4A4A55",
    "ds-danger": "#E5484D",
    "ds-success": "#30A46C",
    "ds-warning": "#F76B15",
    "ds-info": "#0091FF",
    "ds-overlay": "rgba(0,0,0,0.6)",
    "ds-scrim": "rgba(0,0,0,0.4)",
    # Typography families
    "ds-font-display": "'Inter', -apple-system, sans-serif",
    "ds-font-body": "'Inter', -apple-system, sans-serif",
    "ds-font-mono": "'JetBrains Mono', monospace",
    # Type sizes
    "ds-text-xs": "0.75rem",
    "ds-text-sm": "0.875rem",
    "ds-text-base": "1rem",
    "ds-text-lg": "1.125rem",
    "ds-text-xl": "1.25rem",
    "ds-text-2xl": "1.5rem",
    "ds-text-3xl": "1.875rem",
    "ds-text-4xl": "2.25rem",
    "ds-text-5xl": "3rem",
    "ds-text-6xl": "3.75rem",
    "ds-text-7xl": "4.5rem",
    "ds-text-8xl": "6rem",
    # Line heights
    "ds-leading-tight": "1.2",
    "ds-leading-snug": "1.35",
    "ds-leading-normal": "1.5",
    "ds-leading-relaxed": "1.65",
    "ds-leading-loose": "1.9",
    # Tracking (DRL has 4; Resemblio contract adds snug)
    "ds-tracking-tight": "-0.022em",
    "ds-tracking-normal": "0",
    "ds-tracking-wide": "0.04em",
    "ds-tracking-wider": "0.12em",
    # Font weights
    "ds-font-weight-display": "700",
    "ds-font-weight-body": "400",
    "ds-font-weight-medium": "500",
    # Spacing (12-step scale)
    "ds-space-1": "0.25rem",
    "ds-space-2": "0.5rem",
    "ds-space-3": "0.75rem",
    "ds-space-4": "1rem",
    "ds-space-5": "1.25rem",
    "ds-space-6": "1.5rem",
    "ds-space-8": "2rem",
    "ds-space-10": "2.5rem",
    "ds-space-12": "3rem",
    "ds-space-16": "4rem",
    "ds-space-20": "5rem",
    "ds-space-24": "6rem",
    # Radii
    "ds-radius-xs": "2px",
    "ds-radius-sm": "4px",
    "ds-radius-md": "6px",
    "ds-radius-lg": "8px",
    "ds-radius-xl": "12px",
    "ds-radius-badge": "4px",
    # Shadows (6 DRL slots; 3 are in Resemblio contract, 3 are extras)
    "ds-shadow-none": "none",           # EXTRA (not in Resemblio contract)
    "ds-shadow-xs": "0 1px 2px rgba(0,0,0,0.30)",   # CONTRACT slot
    "ds-shadow-sm": "0 2px 6px rgba(0,0,0,0.35)",   # CONTRACT slot
    "ds-shadow-md": "0 8px 20px rgba(0,0,0,0.40)",  # CONTRACT slot
    "ds-shadow-lg": "0 18px 40px rgba(0,0,0,0.50)", # EXTRA (not in Resemblio contract)
    "ds-shadow-2xl": "0 32px 72px rgba(0,0,0,0.60)", # EXTRA (not in Resemblio contract)
    # Motion eases (1 in Resemblio contract, 3 are extras)
    "ds-ease-standard": "cubic-bezier(0.4, 0, 0.2, 1)",    # CONTRACT slot
    "ds-ease-accelerate": "cubic-bezier(0.4, 0, 1, 1)",    # EXTRA
    "ds-ease-decelerate": "cubic-bezier(0, 0, 0.2, 1)",    # EXTRA
    "ds-ease-emphasize": "cubic-bezier(0.2, 0, 0, 1)",     # EXTRA
    # Motion durations (2 in Resemblio contract, 2 are extras; note name mismatch)
    "ds-duration-instant": "80ms",    # EXTRA
    "ds-duration-fast": "150ms",      # CONTRACT slot: ds-duration-fast
    "ds-duration-normal": "240ms",    # EXTRA (DRL name; Resemblio contract uses ds-duration-base)
    "ds-duration-slow": "400ms",      # EXTRA
}


def _round_trip(tokens: dict[str, str]) -> str:
    """Simulate the seed -> tokens_for_compose -> _emit_brand_root path.

    In production the tokens dict is stored in dtcg_json["tokens"], then
    retrieved via ``tokens_for_compose``, then passed to ``_emit_brand_root``.
    This helper collapses those three steps for tests.
    """
    dtcg = {"tokens": tokens}
    flat = tokens_for_compose(dtcg)
    return _emit_brand_root(flat)


# ---------------------------------------------------------------------------
# Shadow slot round-trip tests
# ---------------------------------------------------------------------------


def test_contract_shadow_slots_use_brand_values() -> None:
    """The three Resemblio contract shadow slots emit brand-true values.

    These are in BRAND_TOKEN_CONTRACT so they are emitted via the
    contract-first path in ``_emit_brand_root``.
    """
    css = _round_trip(_FULL_DRL_TOKENS)
    assert "--ds-shadow-xs: 0 1px 2px rgba(0,0,0,0.30);" in css
    assert "--ds-shadow-sm: 0 2px 6px rgba(0,0,0,0.35);" in css
    assert "--ds-shadow-md: 0 8px 20px rgba(0,0,0,0.40);" in css


def test_extra_shadow_slots_emit_via_passthrough() -> None:
    """Shadow slots outside the Resemblio contract emit via the extras path.

    ds-shadow-none, ds-shadow-lg, and ds-shadow-2xl are not Resemblio contract
    slots. They must still appear with brand-true values (not dropped) so the
    DRL templates that reference them resolve correctly.
    """
    css = _round_trip(_FULL_DRL_TOKENS)
    assert "--ds-shadow-none: none;" in css
    assert "--ds-shadow-lg: 0 18px 40px rgba(0,0,0,0.50);" in css
    assert "--ds-shadow-2xl: 0 32px 72px rgba(0,0,0,0.60);" in css


def test_shadow_values_are_brand_true_not_defaults() -> None:
    """Brand shadow values override the contract defaults (dark shadows vs light).

    The default contract values are light-theme (e.g. ``rgba(0,0,0,0.04)``).
    A dark-mode brand supplies much higher opacity. Verifying the brand value
    wins proves the override path works, not just that ANY value is present.
    """
    from extractor.token_contract import BRAND_TOKEN_CONTRACT

    css = _round_trip(_FULL_DRL_TOKENS)
    default_xs = BRAND_TOKEN_CONTRACT["slots"]["ds-shadow-xs"]["default"]
    # Brand value is distinctly different from the contract default.
    assert _FULL_DRL_TOKENS["ds-shadow-xs"] != default_xs
    assert f"--ds-shadow-xs: {_FULL_DRL_TOKENS['ds-shadow-xs']};" in css
    assert f"--ds-shadow-xs: {default_xs};" not in css


# ---------------------------------------------------------------------------
# Motion slot round-trip tests
# ---------------------------------------------------------------------------


def test_contract_motion_slots_use_brand_values() -> None:
    """The three Resemblio contract motion slots emit brand-true values."""
    css = _round_trip(_FULL_DRL_TOKENS)
    assert "--ds-ease-standard: cubic-bezier(0.4, 0, 0.2, 1);" in css
    assert "--ds-duration-fast: 150ms;" in css


def test_extra_motion_ease_slots_emit_via_passthrough() -> None:
    """Extra ease curves (accelerate, decelerate, emphasize) emit with brand values."""
    css = _round_trip(_FULL_DRL_TOKENS)
    assert "--ds-ease-accelerate: cubic-bezier(0.4, 0, 1, 1);" in css
    assert "--ds-ease-decelerate: cubic-bezier(0, 0, 0.2, 1);" in css
    assert "--ds-ease-emphasize: cubic-bezier(0.2, 0, 0, 1);" in css


def test_extra_motion_duration_slots_emit_via_passthrough() -> None:
    """Extra duration slots (instant, normal, slow) emit with brand values.

    Note: DRL uses ``ds-duration-normal`` (240ms) while the Resemblio contract
    uses ``ds-duration-base``. Both emit: ``ds-duration-normal`` via the extras
    pass-through; ``ds-duration-base`` via the contract path (using the contract
    default, since no DRL brand maps a value to that exact name).
    """
    css = _round_trip(_FULL_DRL_TOKENS)
    assert "--ds-duration-instant: 80ms;" in css
    assert "--ds-duration-normal: 240ms;" in css
    assert "--ds-duration-slow: 400ms;" in css


# ---------------------------------------------------------------------------
# Tracking slot round-trip tests
# ---------------------------------------------------------------------------


def test_tracking_slots_use_brand_values() -> None:
    """All four DRL tracking slots emit with brand-true values."""
    css = _round_trip(_FULL_DRL_TOKENS)
    assert "--ds-tracking-tight: -0.022em;" in css
    assert "--ds-tracking-normal: 0;" in css
    assert "--ds-tracking-wide: 0.04em;" in css
    assert "--ds-tracking-wider: 0.12em;" in css


def test_tracking_values_are_brand_true_not_defaults() -> None:
    """Tracking brand values override contract defaults where values differ."""
    from extractor.token_contract import BRAND_TOKEN_CONTRACT

    css = _round_trip(_FULL_DRL_TOKENS)
    # ds-tracking-tight: brand is -0.022em; default is -0.02em
    default_tight = BRAND_TOKEN_CONTRACT["slots"]["ds-tracking-tight"]["default"]
    assert _FULL_DRL_TOKENS["ds-tracking-tight"] != default_tight
    assert f"--ds-tracking-tight: {_FULL_DRL_TOKENS['ds-tracking-tight']};" in css
    assert f"--ds-tracking-tight: {default_tight};" not in css


# ---------------------------------------------------------------------------
# Full-dict completeness test
# ---------------------------------------------------------------------------


def test_all_full_drl_slots_survive_round_trip() -> None:
    """Every slot in _FULL_DRL_TOKENS appears in the emitted CSS.

    This is the Gap A closure test: no DRL slot is silently dropped by the
    tokens_for_compose -> _emit_brand_root path. The test is parameterized
    over every key in the synthetic token dict so a future contract change
    that drops a slot produces a clear per-slot failure message.
    """
    css = _round_trip(_FULL_DRL_TOKENS)
    missing: list[str] = []
    for key, value in _FULL_DRL_TOKENS.items():
        # Build the CSS property name: "ds-shadow-xs" -> "--ds-shadow-xs"
        prop = f"--{key}" if key.startswith("ds-") else f"--ds-{key}"
        expected_decl = f"{prop}: {value};"
        if expected_decl not in css:
            missing.append(f"{prop}: {value!r} not found")
    assert not missing, (
        f"{len(missing)} slot(s) dropped in round-trip:\n"
        + "\n".join(f"  {m}" for m in missing)
    )


def test_tokens_for_compose_passes_all_string_slots() -> None:
    """``tokens_for_compose`` retains every string-valued slot from dtcg['tokens'].

    List and dict values (patterns, mood lists) are filtered out by design;
    all scalar token values must survive.
    """
    dtcg: dict = {
        "tokens": dict(_FULL_DRL_TOKENS),
        "patterns": ["editorial-display-sans"],  # list: must NOT appear in output
        "mood": ["confident"],                   # list: must NOT appear in output
        "tier": "A",                             # string at top level, not in tokens
    }
    flat = tokens_for_compose(dtcg)
    # Every token key must survive.
    for key in _FULL_DRL_TOKENS:
        assert key in flat, f"token key {key!r} dropped by tokens_for_compose"
    # Top-level non-token keys must NOT appear (they're outside dtcg['tokens']).
    assert "patterns" not in flat
    assert "mood" not in flat
    assert "tier" not in flat

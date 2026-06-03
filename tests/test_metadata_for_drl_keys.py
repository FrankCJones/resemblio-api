"""Table-driven cross-shape parity tests for ``_metadata_for``.

CTO TDD recovery plan Phase 1 (``projects/OptSus Team/cto-reviews/
2026-06-02-resemblio-library-tdd-recovery.md``) Section 3.

Bug 11 (failure trail 2026-06-02): ``_metadata_for`` reads bare token keys
(``bg``, ``surface``, ``text``, ...) but the DRL parser emits already-
namespaced ``ds-``-prefixed keys (``ds-bg``, ``ds-surface``, ...). Without
shape normalization the OG envelope returns ``None`` for every field on a
DRL-seeded brand. This test pins the contract: every supported key shape
must produce the same envelope values.

The fixture (``tests/fixtures/drl/aeon_min/mixed_keys.json``) carries
three parallel token bags with the SAME source values for the six envelope
fields - only the key shape differs. Byte equality of the returned
envelope across all three variants is therefore a real assertion, not a
tautology.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.library_indexer import _metadata_for


# The six envelope fields ``_metadata_for`` is contracted to project.
# Mirrors ``test_library_indexer_render_fidelity.METADATA_ENVELOPE_FIELDS``;
# kept duplicated rather than imported because both tests pin the same
# contract from different angles and a future split should not be coupled.
METADATA_ENVELOPE_FIELDS: tuple[str, ...] = (
    "bg",
    "surface",
    "text",
    "accent",
    "font_display",
    "font_body",
)

FIXTURE_FILE = (
    Path(__file__).parent
    / "fixtures"
    / "drl"
    / "aeon_min"
    / "mixed_keys.json"
)


def _load_variants() -> dict[str, dict[str, str]]:
    """Return the three parallel token bags from ``mixed_keys.json``."""
    raw = json.loads(FIXTURE_FILE.read_text(encoding="utf-8"))
    return {
        "bare_keys": raw["bare_keys"],
        "ds_prefixed_keys": raw["ds_prefixed_keys"],
        "mixed_keys": raw["mixed_keys"],
    }


_VARIANTS = _load_variants()
_VARIANT_NAMES = tuple(_VARIANTS.keys())


@pytest.mark.parametrize("variant_name", _VARIANT_NAMES)
def test_metadata_for_projects_every_envelope_field(variant_name: str) -> None:
    """Every supported key shape projects every envelope field non-None.

    The DRL ds-prefixed variant is the bug 11 regression pin: before the
    ``_metadata_for`` fix it returned ``None`` for every field on this
    input because it reads only bare keys.
    """
    tokens = _VARIANTS[variant_name]
    envelope = _metadata_for(
        "navigation", brand_slug="aeon", tokens=tokens
    )
    for field_name in METADATA_ENVELOPE_FIELDS:
        value = envelope[field_name]
        assert value is not None, (
            f"variant {variant_name!r}: envelope field {field_name!r} is None "
            f"(bug 11 regression: _metadata_for fails to normalize key shape)"
        )
        assert isinstance(value, str), (
            f"variant {variant_name!r}: envelope field {field_name!r} is "
            f"{type(value).__name__}, expected str"
        )


def test_metadata_for_byte_identical_envelope_across_all_variants() -> None:
    """All three variants project byte-identical envelopes for the six fields.

    The three bags share source values; the only difference is key shape.
    After normalization the envelope must be byte-identical. This is the
    test that would have caught bug 11 in CI.
    """
    envelopes: dict[str, dict[str, Any]] = {
        name: _metadata_for("navigation", brand_slug="aeon", tokens=tokens)
        for name, tokens in _VARIANTS.items()
    }
    bare = envelopes["bare_keys"]
    for variant_name in ("ds_prefixed_keys", "mixed_keys"):
        other = envelopes[variant_name]
        for field_name in METADATA_ENVELOPE_FIELDS:
            assert other[field_name] == bare[field_name], (
                f"variant {variant_name!r} field {field_name!r} differs from "
                f"bare_keys: {other[field_name]!r} vs {bare[field_name]!r}"
            )

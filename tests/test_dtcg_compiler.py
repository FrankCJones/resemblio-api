"""Contract tests for deterministic strict DTCG library compilation."""
from __future__ import annotations

from copy import deepcopy
import hashlib

import pytest

from app.dtcg_compiler import (
    COLOR_NAMES,
    DIMENSION_NAMES,
    DURATION_NAMES,
    EASING_NAMES,
    FONT_SOURCE_NAMES,
    LEADING_NAMES,
    SHADOW_NAMES,
    TRACKING_NAMES,
    CompilePolicy,
    canonical_bytes,
    compile_projection,
    validate_document,
)

PHASE9_SHA256 = "2a083501b1247b0c9f5ce623e56f7ba8e4cf95269d28af801f679456d03e2244"


def policy() -> CompilePolicy:
    """Return a synthetic but valid Apple compilation policy."""
    return CompilePolicy(
        projection_version="test.1",
        source_manifest_sha256="a" * 64,
        predecessor_artifact_sha256=PHASE9_SHA256,
        font_stacks={
            "display": ("system-ui", "sans-serif"),
            "body": ("system-ui", "sans-serif"),
            "mono": ("ui-monospace", "Consolas", "monospace"),
        },
    )


def synthetic_css() -> str:
    """Build a complete in-memory source contract without workspace dependencies."""
    values = {name: "#112233" for name in COLOR_NAMES}
    values.update({name: "system-ui" for name in FONT_SOURCE_NAMES})
    values.update({name: "1px" for name in DIMENSION_NAMES})
    values.update({name: "1.2" for name in LEADING_NAMES})
    values.update({name: "0.01em" for name in TRACKING_NAMES})
    values.update({name: "0 1px 2px rgba(0,0,0,0.1)" for name in SHADOW_NAMES})
    values.update({name: "cubic-bezier(0.4,0,0.2,1)" for name in EASING_NAMES})
    values.update({name: "1ms" for name in DURATION_NAMES})
    values.update({
        "info": "var(--ds-accent)",
        "focus-ring": "var(--ds-accent)",
        "font-accent": "var(--ds-font-display)",
        "leading-loose": "1.85",
        "tracking-tight": "-0.02em",
        "tracking-normal": "0",
        "shadow-none": "none",
    })
    declarations = "\n".join(f"  --ds-{name}: {values[name]};" for name in sorted(values))
    return f":root {{\n{declarations}\n}}\n"


def compiled() -> dict:
    """Compile a complete synthetic source fixture."""
    return compile_projection(synthetic_css(), policy())


def test_compiler_is_deterministic_and_records_source_identity() -> None:
    """Repeated source and policy inputs produce identical projection bytes."""
    first = compiled()
    second = compiled()
    assert canonical_bytes(first) == canonical_bytes(second)
    assert first["token_count"] == 72
    assert first["predecessor_artifact_sha256"] == PHASE9_SHA256
    assert first["source_css_sha256"] == hashlib.sha256(synthetic_css().encode("utf-8")).hexdigest()


def test_leading_and_tracking_are_source_supported_number_tokens() -> None:
    """Unitless leading and em tracking retain their exact source ratios."""
    document = compiled()["token_document"]
    assert document["letterSpacing"]["tight"] == {"$type": "number", "$value": -0.02}
    assert document["letterSpacing"]["normal"] == {"$type": "number", "$value": 0}
    assert document["lineHeight"]["loose"] == {"$type": "number", "$value": 1.85}


def test_invalid_schema_and_unsupported_units_fail_closed() -> None:
    """Schema drift and noncontract dimensions cannot enter the artifact."""
    document = deepcopy(compiled()["token_document"])
    document["$schema"] = "https://example.invalid/schema"
    with pytest.raises(ValueError, match="schema"):
        validate_document(document)
    document = deepcopy(compiled()["token_document"])
    document["dimension"]["text-base"]["$value"]["unit"] = "em"
    with pytest.raises(ValueError, match="dimension"):
        validate_document(document)


def test_invalid_shadow_composite_fails_closed() -> None:
    """Every required shadow field and unit is validated."""
    document = deepcopy(compiled()["token_document"])
    del document["shadow"]["sm"]["$value"]["blur"]
    with pytest.raises(ValueError, match="shadow"):
        validate_document(document)


def test_missing_alias_alias_cycle_and_type_change_fail_closed() -> None:
    """Alias graphs must resolve, remain acyclic, and preserve type."""
    missing = deepcopy(compiled()["token_document"])
    missing["color"]["info"]["$value"] = "{color.absent}"
    with pytest.raises(ValueError, match="missing target"):
        validate_document(missing)
    cycle = deepcopy(compiled()["token_document"])
    cycle["color"]["info"]["$value"] = "{color.focus-ring}"
    cycle["color"]["focus-ring"]["$value"] = "{color.info}"
    with pytest.raises(ValueError, match="cycle"):
        validate_document(cycle)
    mismatch = deepcopy(compiled()["token_document"])
    mismatch["color"]["info"]["$value"] = "{dimension.text-base}"
    with pytest.raises(ValueError, match="changes type"):
        validate_document(mismatch)


def test_unsafe_font_reference_fails_closed() -> None:
    """Protected or proprietary font names cannot ship in public tokens."""
    document = deepcopy(compiled()["token_document"])
    document["fontFamily"]["body"]["$value"] = ["SF Pro Text", "sans-serif"]
    with pytest.raises(ValueError, match="fontFamily"):
        validate_document(document)


@pytest.mark.parametrize("value", [True, float("nan"), float("inf")])
def test_non_json_numeric_values_fail_closed(value: object) -> None:
    """Booleans and nonfinite numbers cannot masquerade as DTCG numbers."""
    document = deepcopy(compiled()["token_document"])
    document["dimension"]["text-base"]["$value"]["value"] = value
    with pytest.raises(ValueError, match="dimension"):
        validate_document(document)


def test_empty_document_reserved_names_and_font_urls_fail_closed() -> None:
    """Closed token documents reject empty, reserved, and remote-font shapes."""
    with pytest.raises(ValueError, match="empty"):
        validate_document({"$schema": "https://www.designtokens.org/schemas/2025.10/format.json"})
    reserved = deepcopy(compiled()["token_document"])
    reserved["color"]["$private"] = reserved["color"]["accent"]
    with pytest.raises(ValueError, match="token name"):
        validate_document(reserved)
    remote_font = deepcopy(compiled()["token_document"])
    remote_font["fontFamily"]["body"]["$value"] = ["url(https://example.invalid/font.woff2)"]
    with pytest.raises(ValueError, match="fontFamily"):
        validate_document(remote_font)


def test_canonical_bytes_reject_nonfinite_numbers() -> None:
    """Canonical serialization cannot emit JavaScript-invalid NaN literals."""
    with pytest.raises(ValueError):
        canonical_bytes({"value": float("nan")})

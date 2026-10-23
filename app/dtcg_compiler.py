"""Compile the shared DRL CSS token contract into strict DTCG 2025.10 JSON.

This module is the deterministic source compiler for public library artifacts.
It performs no network or filesystem I/O. Callers supply source text and an
explicit public-safe font policy, then persist the returned projection.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, TypedDict, cast

DTCG_SCHEMA_URL: Final = "https://www.designtokens.org/schemas/2025.10/format.json"
COMPILER_VERSION: Final = "resemblio_dtcg_compiler_1.0.0"
PROJECTION_SCHEMA_VERSION: Final = "resemblio_public_token_projection_v2"
NORMALIZATION_POLICY_VERSION: Final = "dtcg_2025_10_public_safe_v3"
TOKEN_SCHEMA_IDENTITY: Final = "w3c-dtcg-2025.10"

CSS_VARIABLE_RE: Final = re.compile(r"--ds-([a-z0-9-]+)\s*:\s*([^;]+);", re.I)
CSS_COMMENT_RE: Final = re.compile(r"/\*.*?\*/", re.S)
ALIAS_RE: Final = re.compile(r"^\{([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+)\}$")
TOKEN_NAME_RE: Final = re.compile(r"^[A-Za-z0-9_-]+$")
SAFE_FONT_FAMILY_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]*$")
CSS_ALIAS_RE: Final = re.compile(r"^var\(--ds-([a-z0-9-]+)\)$", re.I)
HEX_COLOR_RE: Final = re.compile(r"^#([0-9a-f]{6})$", re.I)
MEASURE_RE: Final = re.compile(r"^(-?(?:\d+(?:\.\d+)?|\.\d+))(px|ms|em)$", re.I)
CUBIC_BEZIER_RE: Final = re.compile(r"^cubic-bezier\(([^)]+)\)$", re.I)
RGBA_RE: Final = re.compile(r"^rgba?\(([^)]+)\)$", re.I)
UNSAFE_FONT_RE: Final = re.compile(
    r"(?:san francisco|sf pro|sf mono|new york font|myriad|product sans|menlo|monaco)",
    re.I,
)

COLOR_NAMES: Final = (
    "bg", "surface", "surface-2", "text", "text-muted", "text-strong",
    "border", "hairline", "accent", "accent-2", "success", "warning",
    "error", "info", "focus-ring",
)
FONT_NAMES: Final = ("display", "body", "mono", "accent")
FONT_SOURCE_NAMES: Final = tuple(f"font-{name}" for name in FONT_NAMES)
DIMENSION_NAMES: Final = (
    "text-2xs", "text-xs", "text-sm", "text-base", "text-lg", "text-xl",
    "text-2xl", "text-3xl", "text-4xl", "text-5xl", "text-6xl", "text-7xl",
    "space-0", "space-1", "space-2", "space-3", "space-4", "space-5",
    "space-6", "space-8", "space-10", "space-12", "space-16", "space-32",
    "radius-none", "radius-xs", "radius-sm", "radius-md", "radius-lg",
    "radius-full",
)
LEADING_NAMES: Final = ("leading-tight", "leading-snug", "leading-normal", "leading-relaxed", "leading-loose")
TRACKING_NAMES: Final = ("tracking-tight", "tracking-normal", "tracking-wide", "tracking-wider")
SHADOW_NAMES: Final = ("shadow-none", "shadow-xs", "shadow-sm", "shadow-md", "shadow-lg", "shadow-2xl")
EASING_NAMES: Final = ("ease-standard", "ease-emphasize", "ease-decelerate", "ease-accelerate")
DURATION_NAMES: Final = ("duration-instant", "duration-fast", "duration-normal", "duration-slow")
EXPECTED_SOURCE_NAMES: Final = frozenset(
    COLOR_NAMES + FONT_SOURCE_NAMES + DIMENSION_NAMES + LEADING_NAMES + TRACKING_NAMES
    + SHADOW_NAMES + EASING_NAMES + DURATION_NAMES
)


DtcgLeaf = TypedDict("DtcgLeaf", {"$type": str, "$value": Any})


class Projection(TypedDict):
    """Versioned compiler output consumed by API and web repositories."""

    schema_version: str
    projection_version: str
    compiler_version: str
    normalization_policy_version: str
    source_manifest_sha256: str
    source_css_sha256: str
    predecessor_artifact_sha256: str
    token_schema: str
    token_count: int
    schema_identity: str
    source_projection_sha256: str
    token_document: dict[str, Any]


@dataclass(frozen=True)
class CompilePolicy:
    """Public-safe normalization choices that are explicit per source system."""

    projection_version: str
    source_manifest_sha256: str
    predecessor_artifact_sha256: str
    font_stacks: Mapping[str, tuple[str, ...]]


def canonical_bytes(value: object) -> bytes:
    """Serialize JSON with sorted keys, compact separators, and one trailing LF."""
    return (json.dumps(_canonical_value(value), ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")


def _canonical_value(value: object) -> object:
    """Normalize mapping order and integral floats for Python and JavaScript parity."""
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def digest(value: object) -> str:
    """Return the canonical SHA-256 identity of a JSON value."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def parse_css_variables(css_text: str) -> dict[str, str]:
    """Parse the closed shared DRL custom-property contract."""
    scrubbed = CSS_COMMENT_RE.sub("", css_text)
    variables = {match.group(1).lower(): match.group(2).strip() for match in CSS_VARIABLE_RE.finditer(scrubbed)}
    missing = sorted(EXPECTED_SOURCE_NAMES - variables.keys())
    unknown = sorted(variables.keys() - EXPECTED_SOURCE_NAMES)
    if missing or unknown:
        raise ValueError(f"Token source contract mismatch: missing={missing}, unknown={unknown}")
    return variables


def _alias(value: str, group: str, source_names: tuple[str, ...]) -> str | None:
    match = CSS_ALIAS_RE.fullmatch(value.strip())
    if not match:
        return None
    target = match.group(1).lower()
    if target not in source_names:
        raise ValueError(f"Alias target is outside the {group} group: {target}")
    return f"{{{group}.{target}}}"


def _color(value: str) -> dict[str, Any]:
    match = HEX_COLOR_RE.fullmatch(value.strip())
    if not match:
        raise ValueError(f"Unsupported color value: {value}")
    raw = match.group(1)
    return {
        "colorSpace": "srgb",
        "components": [round(int(raw[index:index + 2], 16) / 255, 6) for index in (0, 2, 4)],
        "alpha": 1,
    }


def _measure(value: str, expected_unit: str, *, allow_negative: bool = False) -> dict[str, Any]:
    text = value.strip()
    if text == "0":
        return {"value": 0, "unit": expected_unit}
    match = MEASURE_RE.fullmatch(text)
    if not match or match.group(2).lower() != expected_unit:
        raise ValueError(f"Unsupported unit for {value}; expected {expected_unit}")
    amount = float(match.group(1))
    if not math.isfinite(amount) or (amount < 0 and not allow_negative):
        raise ValueError(f"Invalid measure: {value}")
    return {"value": int(amount) if amount.is_integer() else amount, "unit": expected_unit}


def _number(value: str, *, unit: str | None = None) -> int | float:
    text = value.strip()
    if unit is not None:
        if text == "0":
            return 0
        match = MEASURE_RE.fullmatch(text)
        if not match or match.group(2).lower() != unit:
            raise ValueError(f"Unsupported unit for {value}; expected {unit}")
        text = match.group(1)
    try:
        amount = float(text)
    except ValueError as exc:
        raise ValueError(f"Invalid number: {value}") from exc
    if not math.isfinite(amount):
        raise ValueError(f"Invalid number: {value}")
    return int(amount) if amount.is_integer() else amount


def _rgba(value: str) -> dict[str, Any]:
    match = RGBA_RE.fullmatch(value.strip())
    if not match:
        raise ValueError(f"Unsupported shadow color: {value}")
    parts = [part.strip() for part in match.group(1).split(",")]
    if len(parts) not in (3, 4):
        raise ValueError(f"Unsupported shadow color: {value}")
    channels = [int(part) for part in parts[:3]]
    alpha = float(parts[3]) if len(parts) == 4 else 1.0
    if any(channel < 0 or channel > 255 for channel in channels) or not 0 <= alpha <= 1:
        raise ValueError(f"Unsupported shadow color: {value}")
    return {
        "colorSpace": "srgb",
        "components": [round(channel / 255, 6) for channel in channels],
        "alpha": alpha,
    }


def _shadow(value: str) -> dict[str, Any]:
    if value.strip().lower() == "none":
        return {
            "color": {"colorSpace": "srgb", "components": [0, 0, 0], "alpha": 0},
            "offsetX": {"value": 0, "unit": "px"},
            "offsetY": {"value": 0, "unit": "px"},
            "blur": {"value": 0, "unit": "px"},
            "spread": {"value": 0, "unit": "px"},
        }
    parts = value.strip().split(maxsplit=3)
    if len(parts) != 4:
        raise ValueError(f"Unsupported shadow composite: {value}")
    return {
        "color": _rgba(parts[3]),
        "offsetX": _measure(parts[0], "px", allow_negative=True),
        "offsetY": _measure(parts[1], "px", allow_negative=True),
        "blur": _measure(parts[2], "px"),
        "spread": {"value": 0, "unit": "px"},
    }


def _cubic_bezier(value: str) -> list[int | float]:
    match = CUBIC_BEZIER_RE.fullmatch(value.strip())
    if not match:
        raise ValueError(f"Unsupported cubic bezier: {value}")
    points = [_number(part) for part in match.group(1).split(",")]
    if len(points) != 4 or not 0 <= points[0] <= 1 or not 0 <= points[2] <= 1:
        raise ValueError(f"Invalid cubic bezier: {value}")
    return points


def _leaf(kind: str, value: Any) -> DtcgLeaf:
    return {"$type": kind, "$value": value}


def compile_document(css_text: str, policy: CompilePolicy) -> dict[str, Any]:
    """Compile one complete shared-contract stylesheet into pure DTCG JSON."""
    source = parse_css_variables(css_text)
    document: dict[str, Any] = {"$schema": DTCG_SCHEMA_URL}
    document["color"] = {
        name: _leaf("color", _alias(source[name], "color", COLOR_NAMES) or _color(source[name]))
        for name in COLOR_NAMES
    }
    font_group: dict[str, DtcgLeaf] = {}
    for name in FONT_NAMES:
        source_name = f"font-{name}"
        source_value = source[source_name]
        alias_match = CSS_ALIAS_RE.fullmatch(source_value)
        if alias_match:
            target = alias_match.group(1).lower()
            if target not in FONT_SOURCE_NAMES:
                raise ValueError(f"Alias target is outside the fontFamily group: {target}")
            font_group[name] = _leaf("fontFamily", f"{{fontFamily.{target.removeprefix('font-')}}}")
            continue
        stack = policy.font_stacks.get(name)
        if not stack or any(UNSAFE_FONT_RE.search(item) for item in stack):
            raise ValueError(f"A public-safe font stack is required for {name}")
        font_group[name] = _leaf("fontFamily", list(stack))
    document["fontFamily"] = font_group
    document["dimension"] = {
        name: _leaf("dimension", _measure(source[name], "px")) for name in DIMENSION_NAMES
    }
    document["lineHeight"] = {
        name.removeprefix("leading-"): _leaf("number", _number(source[name])) for name in LEADING_NAMES
    }
    document["letterSpacing"] = {
        name.removeprefix("tracking-"): _leaf("number", _number(source[name], unit="em")) for name in TRACKING_NAMES
    }
    document["shadow"] = {
        name.removeprefix("shadow-"): _leaf("shadow", _shadow(source[name])) for name in SHADOW_NAMES
    }
    document["cubicBezier"] = {
        name.removeprefix("ease-"): _leaf("cubicBezier", _cubic_bezier(source[name])) for name in EASING_NAMES
    }
    document["duration"] = {
        name.removeprefix("duration-"): _leaf("duration", _measure(source[name], "ms")) for name in DURATION_NAMES
    }
    validate_document(document)
    return document


def _valid_color(value: object) -> bool:
    return (
        isinstance(value, dict) and set(value) == {"colorSpace", "components", "alpha"}
        and value.get("colorSpace") == "srgb" and isinstance(value.get("components"), list)
        and len(value["components"]) == 3
        and all(_finite_number(item) and 0 <= item <= 1 for item in value["components"])
        and _finite_number(value.get("alpha")) and 0 <= value["alpha"] <= 1
    )


def _valid_measure(value: object, unit: str, *, allow_negative: bool = False) -> bool:
    return (
        isinstance(value, dict) and set(value) == {"value", "unit"} and value.get("unit") == unit
        and _finite_number(value.get("value"))
        and (allow_negative or value["value"] >= 0)
    )


def _finite_number(value: object) -> bool:
    """Accept finite JSON numbers while rejecting Python booleans."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _valid_shadow(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) in ({"color", "offsetX", "offsetY", "blur", "spread"}, {"color", "offsetX", "offsetY", "blur", "spread", "inset"})
        and _valid_color(value.get("color"))
        and _valid_measure(value.get("offsetX"), "px", allow_negative=True)
        and _valid_measure(value.get("offsetY"), "px", allow_negative=True)
        and _valid_measure(value.get("blur"), "px")
        and _valid_measure(value.get("spread"), "px", allow_negative=True)
        and ("inset" not in value or isinstance(value["inset"], bool))
    )


def validate_document(document: object) -> dict[str, Any]:
    """Validate the closed DTCG subset and resolve every alias fail-closed."""
    if not isinstance(document, dict) or document.get("$schema") != DTCG_SCHEMA_URL:
        raise ValueError("DTCG document schema is unsupported")
    if any(key.startswith("$") for key in document if key != "$schema"):
        raise ValueError("DTCG document contains a prohibited root property")
    if len(document) == 1:
        raise ValueError("DTCG document is empty")
    types: dict[str, str] = {}
    aliases: dict[str, str] = {}
    for group, leaves in document.items():
        if group == "$schema":
            continue
        if not isinstance(group, str) or not TOKEN_NAME_RE.fullmatch(group):
            raise ValueError(f"DTCG group name is invalid: {group}")
        if not isinstance(leaves, dict) or not leaves:
            raise ValueError(f"DTCG group {group} is empty")
        for name, raw in leaves.items():
            if not isinstance(name, str) or not TOKEN_NAME_RE.fullmatch(name) or name.startswith("$"):
                raise ValueError(f"DTCG token name is invalid: {group}.{name}")
            if not isinstance(raw, dict) or set(raw) != {"$type", "$value"}:
                raise ValueError(f"DTCG token {group}.{name} is malformed")
            kind = raw["$type"]
            value = raw["$value"]
            if kind not in {"color", "fontFamily", "dimension", "number", "shadow", "cubicBezier", "duration"}:
                raise ValueError(f"DTCG token {group}.{name} has an unsupported type")
            path = f"{group}.{name}"
            types[path] = kind
            if isinstance(value, str) and (match := ALIAS_RE.fullmatch(value)):
                aliases[path] = match.group(1)
                continue
            valid = (
                _valid_color(value) if kind == "color" else
                isinstance(value, list) and bool(value) and all(isinstance(item, str) and SAFE_FONT_FAMILY_RE.fullmatch(item) and not UNSAFE_FONT_RE.search(item) for item in value) if kind == "fontFamily" else
                _valid_measure(value, "px") if kind == "dimension" else
                _finite_number(value) if kind == "number" else
                _valid_shadow(value) if kind == "shadow" else
                isinstance(value, list) and len(value) == 4 and all(_finite_number(item) for item in value) and 0 <= value[0] <= 1 and 0 <= value[2] <= 1 if kind == "cubicBezier" else
                _valid_measure(value, "ms")
            )
            if not valid:
                raise ValueError(f"DTCG token {path} has an invalid {kind} value")
    for source, target in aliases.items():
        if target not in types:
            raise ValueError(f"DTCG alias {source} has a missing target")
        if types[source] != types[target]:
            raise ValueError(f"DTCG alias {source} changes type")
    for source in aliases:
        seen: set[str] = set()
        target = source
        while target in aliases:
            if target in seen:
                raise ValueError(f"DTCG alias cycle includes {source}")
            seen.add(target)
            target = aliases[target]
    return cast(dict[str, Any], document)


def compile_projection(css_text: str, policy: CompilePolicy) -> Projection:
    """Compile source CSS plus reproducibility metadata into one projection."""
    for value in (policy.source_manifest_sha256, policy.predecessor_artifact_sha256):
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("Projection identity inputs must be SHA-256 digests")
    document = compile_document(css_text, policy)
    body: dict[str, Any] = {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "projection_version": policy.projection_version,
        "compiler_version": COMPILER_VERSION,
        "normalization_policy_version": NORMALIZATION_POLICY_VERSION,
        "source_manifest_sha256": policy.source_manifest_sha256,
        "source_css_sha256": hashlib.sha256(css_text.encode("utf-8")).hexdigest(),
        "predecessor_artifact_sha256": policy.predecessor_artifact_sha256,
        "token_schema": TOKEN_SCHEMA_IDENTITY,
        "token_count": sum(len(group) for name, group in document.items() if name != "$schema"),
        "schema_identity": DTCG_SCHEMA_URL,
        "token_document": document,
    }
    return cast(Projection, {**body, "source_projection_sha256": digest(body)})

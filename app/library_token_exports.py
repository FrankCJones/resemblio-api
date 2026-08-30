"""Library token export normalization and scrub helpers.

Phase I exposes token-native Library exports without leaking protected brand
identity or build provenance. The input can be either the nested DTCG shape
from organic extraction or the DRL seed shape where flat tokens live under
``dtcg_json["tokens"]``. The output is a normalized W3C-style DTCG token tree
plus safe source attribution for the export wrapper.
"""
from __future__ import annotations

import re
from typing import Any, Literal, TypedDict
from urllib.parse import urlparse


LIBRARY_TOKEN_PAYLOAD_SCHEMA_VERSION = "library_token_payload_v1"
TokenGroup = Literal[
    "color",
    "fontFamily",
    "dimension",
    "shadow",
    "duration",
    "cubicBezier",
    "number",
    "opacity",
]

_DTCG_GROUPS: frozenset[str] = frozenset(
    {"color", "fontFamily", "dimension", "shadow", "duration", "cubicBezier", "number", "opacity"}
)
_TYPE_BY_GROUP: dict[str, str] = {
    "color": "color",
    "fontFamily": "fontFamily",
    "dimension": "dimension",
    "shadow": "shadow",
    "duration": "duration",
    "cubicBezier": "cubicBezier",
    "number": "number",
    "opacity": "number",
}
_INTERNAL_PROVENANCE_RE = re.compile(r"(?:resemblio://|drl-bootstrap|drl-mined-from|drl-rebuild|urn:)", re.I)
_WORD_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_DOMAIN_STOPWORDS = frozenset({"www", "com", "co", "net", "org", "io", "app", "design", "studio"})
_INTERNAL_NAME_PARTS = frozenset({"resemblio", "drl", "urn", "seed", "bootstrap", "mined", "rebuild"})


class LibraryTokenSourceAttribution(TypedDict):
    """Public-safe source context carried outside token identifiers."""

    source_url: str
    inspired_by: str


class LibraryTokenPayload(TypedDict):
    """Scrubbed token payload safe for Library export routes."""

    schema_version: str
    token_schema: str
    source_attribution: LibraryTokenSourceAttribution
    tokens: dict[str, dict[str, dict[str, Any]]]
    token_count: int


def _display_source(source_url: str) -> str:
    """Return a compact source label for attribution metadata."""
    parsed = urlparse(source_url)
    if parsed.netloc:
        return parsed.netloc.lower().removeprefix("www.")
    return source_url.strip()


def _kebab(raw: str) -> str:
    """Normalize arbitrary token keys to lower-kebab token names."""
    return "-".join(part for part in _WORD_SPLIT_RE.split(raw.lower()) if part)


def _protected_parts(brand_slug: str, source_url: str) -> frozenset[str]:
    """Return brand/source words that must not appear inside token names."""
    candidates = [brand_slug, _display_source(source_url)]
    parts: set[str] = set()
    for candidate in candidates:
        for part in _WORD_SPLIT_RE.split(candidate.lower()):
            if part and part not in _DOMAIN_STOPWORDS:
                parts.add(part)
    return frozenset(parts)


def _contains_blocked_text(value: str, protected: frozenset[str]) -> bool:
    """Return true when a string value carries provenance or protected marks."""
    lowered = value.lower()
    if _INTERNAL_PROVENANCE_RE.search(lowered):
        return True
    return any(re.search(rf"\b{re.escape(part)}\b", lowered) for part in protected)


def _safe_leaf_name(raw: str, protected: frozenset[str], fallback: str) -> str | None:
    """Return a public-safe leaf token name, or None when it cannot be cleaned."""
    kebab = _kebab(raw)
    if not kebab or _INTERNAL_PROVENANCE_RE.search(kebab):
        return None
    raw_parts = kebab.split("-")
    if any(part in _INTERNAL_NAME_PARTS for part in raw_parts):
        return None
    parts = [part for part in raw_parts if part not in protected]
    cleaned = "-".join(parts).strip("-")
    return cleaned or fallback


def _composite_value_is_safe(raw: Any, protected: frozenset[str]) -> bool:
    """Return true when a composite token value has no unsafe strings."""
    if isinstance(raw, str):
        return not _contains_blocked_text(raw, protected)
    if isinstance(raw, list):
        return all(_composite_value_is_safe(item, protected) for item in raw)
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(key, str) and _contains_blocked_text(key, protected):
                return False
            if not _composite_value_is_safe(value, protected):
                return False
        return True
    return isinstance(raw, (int, float, bool)) or raw is None


def _safe_value(raw: Any, protected: frozenset[str]) -> Any | None:
    """Return a scrubbed token value, or None when the value itself is unsafe."""
    if isinstance(raw, str):
        value = raw.strip()
        if not value or _contains_blocked_text(value, protected):
            return None
        return value
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return raw
    if isinstance(raw, (dict, list)) and _composite_value_is_safe(raw, protected):
        return raw
    return None


def _classify_flat_token(name: str, value: Any) -> tuple[str, str] | None:
    """Map a flat DRL token key to a DTCG group and leaf name."""
    kebab = _kebab(name)
    if not kebab:
        return None
    if kebab.startswith("font-"):
        return "fontFamily", kebab.removeprefix("font-") or "body"
    if kebab.startswith("space-"):
        return "dimension", kebab
    if kebab.startswith("radius-"):
        return "dimension", kebab
    if kebab.startswith("text-") and isinstance(value, str) and value.endswith(("px", "rem", "em")):
        return "dimension", kebab
    if kebab.startswith("shadow-"):
        return "shadow", kebab.removeprefix("shadow-") or "default"
    if kebab.startswith("duration-"):
        return "duration", kebab.removeprefix("duration-") or "default"
    if kebab.startswith("ease-"):
        return "cubicBezier", kebab.removeprefix("ease-") or "default"
    if isinstance(value, str) and (_HEX_COLOR_RE.match(value.strip()) or value.strip().lower().startswith(("rgb", "hsl"))):
        leaf = kebab.removeprefix("color-") or "primary"
        return "color", leaf
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "number", kebab
    return None


def _add_token(
    out: dict[str, dict[str, dict[str, Any]]],
    *,
    group: str,
    leaf_name: str,
    value: Any,
    token_type: str | None,
    protected: frozenset[str],
) -> None:
    """Add one scrubbed token leaf to the output tree if it is safe."""
    if group not in _DTCG_GROUPS:
        return
    safe_value = _safe_value(value, protected)
    if safe_value is None:
        return
    fallback = f"token-{len(out.get(group, {})) + 1}"
    safe_leaf = _safe_leaf_name(leaf_name, protected, fallback)
    if safe_leaf is None:
        return
    group_tokens = out.setdefault(group, {})
    name = safe_leaf
    counter = 2
    while name in group_tokens:
        name = f"{safe_leaf}-{counter}"
        counter += 1
    group_tokens[name] = {"$value": safe_value, "$type": token_type or _TYPE_BY_GROUP[group]}


def _from_nested_dtcg(dtcg_json: dict[str, Any], protected: frozenset[str]) -> dict[str, dict[str, dict[str, Any]]]:
    """Normalize an already grouped DTCG payload while scrubbing token leaves."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for group, group_payload in dtcg_json.items():
        if group not in _DTCG_GROUPS or not isinstance(group_payload, dict):
            continue
        for leaf_name, leaf_payload in group_payload.items():
            if not isinstance(leaf_payload, dict) or "$value" not in leaf_payload:
                continue
            token_type = leaf_payload.get("$type") if isinstance(leaf_payload.get("$type"), str) else None
            _add_token(
                out,
                group=group,
                leaf_name=str(leaf_name),
                value=leaf_payload["$value"],
                token_type=token_type,
                protected=protected,
            )
    return out


def _from_flat_tokens(tokens: dict[str, Any], protected: frozenset[str]) -> dict[str, dict[str, dict[str, Any]]]:
    """Normalize DRL seed flat tokens into W3C-style DTCG groups."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for raw_name, value in tokens.items():
        classified = _classify_flat_token(str(raw_name), value)
        if classified is None:
            continue
        group, leaf_name = classified
        _add_token(
            out,
            group=group,
            leaf_name=leaf_name,
            value=value,
            token_type=None,
            protected=protected,
        )
    return out


def build_library_token_payload(
    dtcg_json: Any,
    *,
    brand_slug: str,
    source_url: str,
) -> LibraryTokenPayload | None:
    """Return a scrubbed token-native payload for a Library page.

    The function is fail-closed: malformed payloads, empty token sets, or token
    values that only resolve to blocked content return ``None`` so callers do
    not advertise a download that would be empty or unsafe.
    """
    if not isinstance(dtcg_json, dict):
        return None
    protected = _protected_parts(brand_slug, source_url)
    nested = _from_nested_dtcg(dtcg_json, protected)
    flat = dtcg_json.get("tokens")
    tokens = nested
    if not tokens and isinstance(flat, dict):
        tokens = _from_flat_tokens(flat, protected)
    token_count = sum(len(group) for group in tokens.values())
    if token_count == 0:
        return None
    return LibraryTokenPayload(
        schema_version=LIBRARY_TOKEN_PAYLOAD_SCHEMA_VERSION,
        token_schema="w3c-dtcg",
        source_attribution=LibraryTokenSourceAttribution(
            source_url=source_url,
            inspired_by=_display_source(source_url),
        ),
        tokens=tokens,
        token_count=token_count,
    )

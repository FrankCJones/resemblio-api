"""Build and validate deterministic, public-safe Apple DTCG export artifacts.

This module mirrors the web product compiler while taking its canonical
manifest exclusively from the API-owned artifact and its DTCG values from a
checked-in public projection. It performs no database writes, network calls,
or credit-ledger operations.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import deque
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Literal, TypedDict, cast

from app.apple_system_manifest import (
    AppleSystemManifest,
    load_public_manifest,
)

APPLE_EXPORT_SCHEMA_VERSION: Final = "resemblio_apple_export_product_v1"
APPLE_EXPORT_ARTIFACT_VERSION: Final = "2026.10.2"
APPLE_EXPORTER_VERSION: Final = "1.1.0"
APPLE_TOKEN_PROJECTION_SCHEMA_VERSION: Final = "resemblio_public_token_projection_v1"
APPLE_TOKEN_PROJECTION_VERSION: Final = "2026.10.2"
APPLE_TOKEN_NORMALIZATION_POLICY_VERSION: Final = "dtcg_2025_10_public_safe_v2"
DTCG_SCHEMA_URL: Final = "https://www.designtokens.org/schemas/2025.10/format.json"
TOKEN_PROJECTION_PATH: Final = Path(__file__).resolve().parent / "data" / "apple-export-tokens.json"
CLASSIFICATIONS: Final = ("foundation", "component", "composition")
TOKEN_TYPES: Final = ("color", "fontFamily", "dimension", "shadow", "cubicBezier", "duration")
DOMAIN_KEYS: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "color": {"color": ("bg", "surface", "surface-2", "text", "text-muted", "text-strong", "border", "hairline", "accent", "accent-2", "success", "warning", "error", "info", "focus-ring")},
    "typography": {"fontFamily": ("display", "body", "mono", "accent"), "dimension": ("text-2xs", "text-xs", "text-sm", "text-base", "text-lg", "text-xl", "text-2xl", "text-3xl", "text-4xl", "text-5xl", "text-6xl", "text-7xl")},
    "spacing": {"dimension": ("space-0", "space-1", "space-2", "space-3", "space-4", "space-5", "space-6", "space-8", "space-10", "space-12", "space-16", "space-32", "section-gap")},
    "layout": {"dimension": ("radius-none", "radius-xs", "radius-sm", "radius-md", "radius-lg", "radius-full", "container-sm", "container-md", "container-lg", "container-xl", "breakpoint-mobile", "breakpoint-tablet", "breakpoint-desktop", "section-padding-sm", "section-padding-md", "section-padding-lg"), "shadow": ("none", "xs", "sm", "md", "lg", "2xl")},
    "motion": {"cubicBezier": ("standard", "emphasize", "decelerate", "accelerate"), "duration": ("instant", "fast", "normal", "slow")},
}
HASH_RE: Final = re.compile(r"^[0-9a-f]{64}$")
RECORD_ID_RE: Final = re.compile(r"^(foundation|component|composition):[a-z0-9][a-z0-9-]*$")
ALIAS_RE: Final = re.compile(r"^\{[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+\}$")
INTERNAL_RE: Final = re.compile(r"(?:_vendored|design reference library|resemblio://|data-rs-source|assets[\\/]|[a-z]:\\|\.css\b|\.html\b)", re.I)
IDENTITY_RE: Final = re.compile(r"(?:iphone|ipad|macos|san francisco pro|sf pro|sf mono|new york font|myriad|product sans|cupertino|menlo|monaco)", re.I)
AFFILIATION_RE: Final = re.compile(r"(?:official apple|apple[- ]official|affiliated with apple|endorsed by apple|apple endorsed|authorized by apple|apple partner)", re.I)

AppleClassification = Literal["foundation", "component", "composition"]
AppleSelectionKind = Literal["complete", "classification", "record"]


class AppleSelection(TypedDict, total=False):
    """Normalized selector parsed from endpoint query parameters."""

    kind: AppleSelectionKind
    classification: AppleClassification
    record_ids: list[str]


class AppleSelectorError(TypedDict):
    """Stable public error returned for malformed export selectors."""

    code: str
    message: str


class Breakdown(TypedDict):
    """Selected record counts grouped by canonical classification."""

    foundations: int
    components: int
    compositions: int


class SelectionContract(TypedDict, total=False):
    """Self-hashed normalized selection plus direct and dependency counts."""

    kind: AppleSelectionKind
    classification: AppleClassification
    selected_record_ids: list[str]
    dependency_record_ids: list[str]
    selected_record_count: int
    dependency_record_count: int
    selected_breakdown: Breakdown
    dependency_breakdown: Breakdown
    selection_sha256: str


class ExportRecord(TypedDict):
    """Public-safe projection of one manifest record and its guidance."""

    record_id: str
    slug: str
    classification: AppleClassification
    route: str
    facet_evidence_sha256: str
    facets: dict[str, Any]
    relationships: dict[str, list[str]]
    guidance: dict[str, Any]


class ExportArtifact(TypedDict):
    """Complete deterministic DTCG export envelope."""

    schema_version: str
    artifact_id: str
    artifact_version: str
    exporter_version: str
    normalization_policy_version: str
    reproducibility: dict[str, str]
    artifact_sha256: str
    manifest_reference: dict[str, Any]
    token_payload_reference: dict[str, Any]
    selection: SelectionContract
    attribution: dict[str, str]
    scrub_policy: dict[str, Any]
    token_schema: str
    token_document: dict[str, Any]
    records: list[ExportRecord]
    dependencies: list[ExportRecord]


class AppleSelectorParseError(ValueError):
    """Selector error carrying the stable client-visible code and message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _canonical_value(value: Any) -> Any:
    """Normalize JSON numbers and nested mappings to JavaScript JSON parity."""
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _canonical_bytes(value: object) -> bytes:
    """Serialize with the web compiler's sorted ASCII JSON plus one LF."""
    normalized = _canonical_value(value)
    return (json.dumps(normalized, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")


def _digest(value: object) -> str:
    """Calculate a canonical SHA-256 digest matching the web compiler."""
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _public_safe(value: object) -> None:
    """Reject internal provenance, protected identity, or affiliation claims."""
    serialized = json.dumps(value, ensure_ascii=True, sort_keys=True)
    if INTERNAL_RE.search(serialized):
        raise ValueError("Apple export contains internal provenance")
    if IDENTITY_RE.search(serialized):
        raise ValueError("Apple export contains an unsafe identity")
    if AFFILIATION_RE.search(serialized):
        raise ValueError("Apple export contains an unsupported affiliation claim")


def _load_projection(path: Path = TOKEN_PROJECTION_PATH) -> dict[str, Any]:
    """Load and integrity-check the pinned public token projection."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Apple token projection is missing or invalid") from exc
    if not isinstance(raw, dict):
        raise ValueError("Apple token projection metadata is unsupported")
    expected = {
        "schema_version": APPLE_TOKEN_PROJECTION_SCHEMA_VERSION,
        "projection_version": APPLE_TOKEN_PROJECTION_VERSION,
        "normalization_policy_version": APPLE_TOKEN_NORMALIZATION_POLICY_VERSION,
        "source_manifest_sha256": load_public_manifest()["artifact_sha256"],
        "token_schema": "w3c-dtcg-2025.10",
        "token_count": 74,
    }
    if any(raw.get(key) != value for key, value in expected.items()):
        raise ValueError("Apple token projection metadata is unsupported")
    projection_digest = raw.get("source_projection_sha256")
    if not isinstance(projection_digest, str) or not HASH_RE.fullmatch(projection_digest):
        raise ValueError("Apple token projection metadata is unsupported")
    body = dict(raw)
    body.pop("source_projection_sha256", None)
    if _digest(body) != projection_digest:
        raise ValueError("Apple token projection digest does not match")
    document = raw.get("token_document")
    if not isinstance(document, dict) or document.get("$schema") != DTCG_SCHEMA_URL:
        raise ValueError("Apple token document schema is unsupported")
    count = sum(len(group) for name, group in document.items() if name != "$schema" and isinstance(group, dict))
    if count != 74:
        raise ValueError("Apple token document must contain exactly 74 tokens")
    _validate_document(document, expected=74)
    _public_safe(raw)
    return cast(dict[str, Any], raw)


def _valid_color(value: object) -> bool:
    """Check the closed DTCG sRGB color value shape."""
    return (
        isinstance(value, dict)
        and set(value) == {"colorSpace", "components", "alpha"}
        and value.get("colorSpace") == "srgb"
        and isinstance(value.get("components"), list)
        and len(cast(list[Any], value["components"])) == 3
        and all(isinstance(item, (int, float)) and math.isfinite(item) and 0 <= item <= 1 for item in cast(list[Any], value["components"]))
        and isinstance(value.get("alpha"), (int, float))
        and math.isfinite(value["alpha"])
        and 0 <= cast(float, value["alpha"]) <= 1
    )


def _valid_measure(value: object, unit: str) -> bool:
    """Check a non-negative finite DTCG measurement with its pinned unit."""
    return (
        isinstance(value, dict)
        and set(value) == {"value", "unit"}
        and value.get("unit") == unit
        and isinstance(value.get("value"), (int, float))
        and math.isfinite(value["value"])
        and value.get("value") >= 0
    )


def _validate_document(document: Mapping[str, Any], expected: int | None = None) -> None:
    """Validate token groups, leaves, types, units, and optional leaf count."""
    if document.get("$schema") != DTCG_SCHEMA_URL:
        raise ValueError("Apple token document schema is unsupported")
    token_count = 0
    for group, leaves in document.items():
        if group == "$schema":
            continue
        if not isinstance(leaves, dict) or not leaves:
            raise ValueError(f"Apple token group {group} is empty")
        for name, raw in leaves.items():
            token_count += 1
            if not isinstance(raw, dict) or set(raw) != {"$type", "$value"} or raw.get("$type") not in TOKEN_TYPES:
                raise ValueError(f"Apple token {group}.{name} is malformed")
            kind, value = raw["$type"], raw["$value"]
            if isinstance(value, str) and ALIAS_RE.fullmatch(value):
                continue
            valid = (
                _valid_color(value) if kind == "color" else
                _valid_measure(value, "px") if kind == "dimension" else
                _valid_measure(value, "ms") if kind == "duration" else
                isinstance(value, list) and bool(value) and all(isinstance(item, str) and item for item in value) if kind == "fontFamily" else
                isinstance(value, list) and len(value) == 4 and all(isinstance(item, (int, float)) and math.isfinite(item) for item in value) and 0 <= value[0] <= 1 and 0 <= value[2] <= 1 if kind == "cubicBezier" else
                _valid_shadow(value)
            )
            if not valid:
                raise ValueError(f"Apple token {group}.{name} has an invalid {kind} value")
    if expected is not None and token_count != expected:
        raise ValueError(f"Apple token document must contain exactly {expected} tokens")


def _valid_shadow(value: object) -> bool:
    """Validate the DTCG shadow shape and each typed measurement member."""
    if not isinstance(value, dict):
        return False
    allowed = {"color", "offsetX", "offsetY", "blur", "spread", "inset"}
    if not {"color", "offsetX", "offsetY", "blur", "spread"}.issubset(value) or set(value) - allowed:
        return False
    return _valid_color(value["color"]) and all(_valid_measure(value[key], "px") for key in ("offsetX", "offsetY", "blur", "spread")) and ("inset" not in value or isinstance(value["inset"], bool))


def parse_selection(params: Mapping[str, Sequence[str]]) -> AppleSelection:
    """Parse endpoint selector lists using the web contract's stable errors."""
    kinds = list(params.get("selection", ()))
    classes = list(params.get("classification", ()))
    ids = list(params.get("record", ()))
    if len(kinds) != 1:
        raise AppleSelectorParseError("invalid_selection", "Exactly one selection is required.")
    kind = kinds[0]
    if kind == "complete":
        if classes or ids:
            raise AppleSelectorParseError("mixed_selection_contracts", "Complete selection cannot include slice fields.")
        return {"kind": "complete"}
    if kind == "classification":
        if ids:
            raise AppleSelectorParseError("mixed_selection_contracts", "Classification selection cannot include record fields.")
        if len(classes) != 1 or classes[0] not in CLASSIFICATIONS:
            raise AppleSelectorParseError("invalid_classification", "Classification selection requires one supported classification.")
        return {"kind": "classification", "classification": cast(AppleClassification, classes[0])}
    if kind == "record":
        if classes:
            raise AppleSelectorParseError("mixed_selection_contracts", "Record selection cannot include classification fields.")
        if not ids:
            raise AppleSelectorParseError("missing_records", "Record selection requires one or more record values.")
        if any(not RECORD_ID_RE.fullmatch(value) for value in ids):
            raise AppleSelectorParseError("invalid_record_id", "Record values must use a supported classification and slug.")
        manifest = load_public_manifest()
        index = {record["record_id"]: number for number, record in enumerate(manifest["records"])}
        if any(value not in index for value in ids):
            raise AppleSelectorParseError("unknown_record", "Every selected record must exist in the canonical manifest.")
        return {"kind": "record", "record_ids": sorted(set(ids), key=index.__getitem__)}
    raise AppleSelectorParseError("invalid_selection", "Selection must be complete, classification, or record.")


def _counts(records: Sequence[Mapping[str, Any]]) -> Breakdown:
    """Count manifest records by the three stable public classifications."""
    return {
        "foundations": sum(record["classification"] == "foundation" for record in records),
        "components": sum(record["classification"] == "component" for record in records),
        "compositions": sum(record["classification"] == "composition" for record in records),
    }


def _related(record: Mapping[str, Any]) -> list[str]:
    """Flatten the canonical relationship targets in fixed classification order."""
    relationships = cast(dict[str, list[str]], record["relationships"])
    return relationships["foundations"] + relationships["components"] + relationships["compositions"]


def _dependencies(selected: Sequence[Mapping[str, Any]], manifest: AppleSystemManifest) -> list[dict[str, Any]]:
    """Compute transitive relationship closure excluding directly selected rows."""
    record_by_id = {record["record_id"]: cast(dict[str, Any], record) for record in manifest["records"]}
    direct = {record["record_id"] for record in selected}
    seen = set(direct)
    queue = deque(target for record in selected for target in _related(record))
    while queue:
        record_id = queue.popleft()
        if record_id in seen:
            continue
        record = record_by_id.get(record_id)
        if record is None:
            raise ValueError("Apple relationship target is absent from canonical manifest")
        seen.add(record_id)
        queue.extend(_related(record))
    return [record_by_id[record["record_id"]] for record in manifest["records"] if record["record_id"] in seen and record["record_id"] not in direct]


def _motion_guidance(value: Mapping[str, Any]) -> list[str]:
    """Translate observed motion facts to deterministic implementation guidance."""
    if "status" in value:
        return [str(value["reason"])]
    return [
        f"Use the observed {value['duration_ms']}ms timing only as a system constraint.",
        "Preserve the observed reduced-motion behavior." if value["reduced_motion"] == "supported" else "Reduced-motion behavior was not observed. Add and verify a reduced-motion alternative.",
        "Keep transitions tied to explicit component state changes." if value["state_driven"] else "Do not introduce state-driven motion without product evidence.",
    ]


def _export_record(record: Mapping[str, Any]) -> ExportRecord:
    """Project a canonical row into the public product record contract."""
    facets = cast(dict[str, Any], record["facets"])
    responsive = ["Preserve the observed responsive behavior across declared breakpoints."] if facets["responsive"] == "supported" else ["Responsive behavior was not observed. Treat viewport adaptation as an implementation decision that requires verification."]
    facet_type = facets["facet_type"]
    if facet_type == "component":
        guidance = {
            "intent": f"Rebuild the {facets['family']} family from its declared anatomy, states, and system tokens.",
            "accessibility_constraints": list(facets["accessibility"]),
            "responsive_constraints": responsive,
            "motion_constraints": _motion_guidance(facets["motion"]),
        }
    elif facet_type == "composition":
        guidance = {
            "intent": f"Rebuild the {facets['composition_kind']} composition without changing its declared hierarchy.",
            "accessibility_constraints": [f"Preserve semantic landmarks: {', '.join(facets['landmarks'])}."],
            "responsive_constraints": responsive,
            "motion_constraints": _motion_guidance(facets["motion"]),
        }
    else:
        guidance = {
            "intent": f"Apply the {facets['foundation_kind']} foundation through the declared token domains.",
            "accessibility_constraints": ["Verify accessible contrast, focus, and reading behavior where these foundation tokens are applied."],
            "responsive_constraints": responsive,
            "motion_constraints": _motion_guidance(facets["motion"]),
        }
    return {
        "record_id": str(record["record_id"]),
        "slug": str(record["slug"]),
        "classification": cast(AppleClassification, record["classification"]),
        "route": str(record["route"]),
        "facet_evidence_sha256": str(record["facet_evidence_sha256"]),
        "facets": facets,
        "relationships": cast(dict[str, list[str]], record["relationships"]),
        "guidance": guidance,
    }


def _token_slice(records: Sequence[Mapping[str, Any]], projection: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Select the declared token domains and complete token-key partitions."""
    wanted = {domain for record in records for domain in record["facets"]["token_domains"]}
    domains = [domain for domain in DOMAIN_KEYS if domain in wanted]
    source_document = cast(dict[str, Any], projection["token_document"])
    document: dict[str, Any] = {"$schema": DTCG_SCHEMA_URL}
    for domain in domains:
        for group, keys in DOMAIN_KEYS[domain].items():
            source = source_document.get(group)
            if not isinstance(source, dict):
                raise ValueError(f"Missing token source group: {group}")
            target = document.setdefault(group, {})
            for key in keys:
                if key not in source:
                    raise ValueError(f"Missing token source leaf: {group}.{key}")
                target[key] = source[key]
    _validate_document(document)
    return document, domains


def _assemble_export(
    selection: AppleSelection,
    manifest: AppleSystemManifest,
    projection: Mapping[str, Any],
) -> ExportArtifact:
    """Assemble the expected artifact from already validated source artifacts."""
    records_by_id = {record["record_id"]: cast(dict[str, Any], record) for record in manifest["records"]}
    kind = selection["kind"]
    if kind == "complete":
        selected = [cast(dict[str, Any], record) for record in manifest["records"]]
    elif kind == "classification":
        selected = [cast(dict[str, Any], record) for record in manifest["records"] if record["classification"] == selection["classification"]]
    else:
        ids = selection["record_ids"]
        if not ids:
            raise ValueError("Record selection cannot be empty")
        selected = [records_by_id[record_id] for record_id in ids]
    dependencies = _dependencies(selected, manifest)
    selected_ids = [record["record_id"] for record in selected]
    dependency_ids = [record["record_id"] for record in dependencies]
    identity: dict[str, Any] = {"kind": kind}
    if kind == "classification":
        identity["classification"] = selection["classification"]
    identity["selected_record_ids"] = selected_ids
    identity["dependency_record_ids"] = dependency_ids
    contract: SelectionContract = {
        **identity,
        "selected_record_ids": selected_ids,
        "dependency_record_ids": dependency_ids,
        "selected_record_count": len(selected),
        "dependency_record_count": len(dependencies),
        "selected_breakdown": _counts(selected),
        "dependency_breakdown": _counts(dependencies),
        "selection_sha256": _digest(identity),
    }
    token_document, domains = _token_slice(selected, projection)
    token_count = sum(len(group) for group in token_document.values() if isinstance(group, dict))
    manifest_reference = {
        "schema_version": manifest["schema_version"],
        "endpoint": "/v1/library/manifests/apple",
        "artifact_id": manifest["artifact_id"],
        "artifact_version": manifest["artifact_version"],
        "compiler_version": manifest["compiler_version"],
        "normalization_policy_version": manifest["normalization_policy_version"],
        "inventory_version": manifest["inventory_version"],
        "inventory_sha256": manifest["inventory_sha256"],
        "evidence_set_sha256": manifest["evidence_set_sha256"],
        "artifact_sha256": manifest["artifact_sha256"],
        "record_count": 41,
        "breakdown": {"foundations": 11, "components": 17, "compositions": 13},
    }
    payload_reference = {
        "schema_version": APPLE_TOKEN_PROJECTION_SCHEMA_VERSION,
        "projection_version": APPLE_TOKEN_PROJECTION_VERSION,
        "normalization_policy_version": APPLE_TOKEN_NORMALIZATION_POLICY_VERSION,
        "source_manifest_sha256": manifest["artifact_sha256"],
        "source_projection_sha256": projection["source_projection_sha256"],
        "payload_sha256": _digest(token_document),
        "token_count": token_count,
        "token_domains": domains,
    }
    body: dict[str, Any] = {
        "schema_version": APPLE_EXPORT_SCHEMA_VERSION,
        "artifact_id": "apple-system-dtcg",
        "artifact_version": APPLE_EXPORT_ARTIFACT_VERSION,
        "exporter_version": APPLE_EXPORTER_VERSION,
        "normalization_policy_version": APPLE_TOKEN_NORMALIZATION_POLICY_VERSION,
        "reproducibility": {"mode": "deterministic", "timestamp_policy": "omitted", "serialization": "canonical-json-lf-utf8"},
        "manifest_reference": manifest_reference,
        "token_payload_reference": payload_reference,
        "selection": contract,
        "attribution": manifest["attribution"],
        "scrub_policy": manifest["scrub_policy"],
        "token_schema": "w3c-dtcg-2025.10",
        "token_document": token_document,
        "records": [_export_record(record) for record in selected],
        "dependencies": [_export_record(record) for record in dependencies],
    }
    artifact: ExportArtifact = {**body, "artifact_sha256": _digest(body)}
    return artifact


def build_export(selection: AppleSelection) -> ExportArtifact:
    """Build, hash, and safety-check the deterministic Phase 7 DTCG artifact."""
    manifest = load_public_manifest()
    projection = _load_projection()
    artifact = _assemble_export(selection, manifest, projection)
    validate_export(artifact, manifest=manifest, projection=projection)
    return artifact


def build_apple_dtcg_export(
    selection_kind: AppleSelectionKind,
    *,
    classification: AppleClassification | None = None,
    record_ids: Sequence[str] = (),
) -> ExportArtifact:
    """Build through the route parser so direct callers get identical normalization."""
    fields: dict[str, list[str]] = {"selection": [selection_kind]}
    if classification is not None:
        fields["classification"] = [classification]
    if record_ids:
        fields["record"] = list(record_ids)
    selection = parse_selection(fields)
    return build_export(selection)


def validate_export(
    artifact: Mapping[str, Any],
    *,
    manifest: AppleSystemManifest | None = None,
    projection: Mapping[str, Any] | None = None,
) -> None:
    """Fail closed on contract drift, closure errors, invalid tokens, and leaks."""
    manifest = manifest or load_public_manifest()
    projection = projection or _load_projection()
    expected_keys = {"schema_version", "artifact_id", "artifact_version", "exporter_version", "normalization_policy_version", "reproducibility", "artifact_sha256", "manifest_reference", "token_payload_reference", "selection", "attribution", "scrub_policy", "token_schema", "token_document", "records", "dependencies"}
    if set(artifact) != expected_keys or artifact.get("schema_version") != APPLE_EXPORT_SCHEMA_VERSION or artifact.get("artifact_id") != "apple-system-dtcg":
        raise ValueError("Apple export artifact identity is unsupported")
    if artifact.get("artifact_version") != APPLE_EXPORT_ARTIFACT_VERSION or artifact.get("exporter_version") != APPLE_EXPORTER_VERSION or artifact.get("normalization_policy_version") != APPLE_TOKEN_NORMALIZATION_POLICY_VERSION:
        raise ValueError("Apple export reproducibility contract is unsupported")
    if artifact.get("reproducibility") != {"mode": "deterministic", "timestamp_policy": "omitted", "serialization": "canonical-json-lf-utf8"}:
        raise ValueError("Apple export reproducibility fields are unsupported")
    body = dict(artifact)
    recorded_hash = body.pop("artifact_sha256", None)
    if not isinstance(recorded_hash, str) or not HASH_RE.fullmatch(recorded_hash) or recorded_hash != _digest(body):
        raise ValueError("Apple export artifact digest does not match")
    _public_safe(artifact)
    if artifact.get("attribution") != manifest["attribution"] or artifact.get("scrub_policy") != manifest["scrub_policy"]:
        raise ValueError("Apple export attribution or scrub policy has drifted")
    if artifact.get("token_schema") != "w3c-dtcg-2025.10":
        raise ValueError("Apple export token schema is unsupported")
    token_document = artifact.get("token_document")
    if not isinstance(token_document, dict):
        raise ValueError("Apple export token document is malformed")
    _validate_document(token_document)
    raw_selection = artifact.get("selection")
    if not isinstance(raw_selection, dict):
        raise ValueError("Apple export selection is malformed")
    kind = raw_selection.get("kind")
    if kind == "complete":
        selection: AppleSelection = {"kind": "complete"}
    elif kind == "classification" and raw_selection.get("classification") in CLASSIFICATIONS:
        selection = {"kind": "classification", "classification": cast(AppleClassification, raw_selection["classification"])}
    elif kind == "record" and isinstance(raw_selection.get("selected_record_ids"), list) and all(isinstance(item, str) for item in raw_selection["selected_record_ids"]):
        selection = {"kind": "record", "record_ids": cast(list[str], raw_selection["selected_record_ids"])}
    else:
        raise ValueError("Apple export selection is invalid")
    try:
        expected = _assemble_export(selection, manifest, projection)
    except (KeyError, TypeError) as exc:
        raise ValueError("Apple export selection is invalid") from exc
    if _canonical_bytes(artifact) != _canonical_bytes(expected):
        raise ValueError("Apple export contract, dependency closure, token slice, or record projection has drifted")


def canonical_artifact_bytes(artifact: ExportArtifact) -> bytes:
    """Return the exact canonical JSON byte representation used for artifact hashes."""
    return _canonical_bytes(artifact)

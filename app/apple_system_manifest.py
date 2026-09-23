"""Compile and validate the canonical public Apple system manifest.

The compiler is deterministic and local. It reads the binding 41-record
matrix plus the pinned vendored corpus, verifies all 82 evidence files, and
emits a public content-addressed artifact and a separate private evidence
ledger. It never reads a database, calls a network service, or exposes source
paths and raw evidence through the public contract.
"""
from __future__ import annotations

import hashlib
import html.parser
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal, TypedDict, cast


SCHEMA_VERSION: Final = "resemblio_apple_system_manifest_v1"
PRIVATE_LEDGER_SCHEMA_VERSION: Final = "resemblio_apple_evidence_ledger_v1"
ARTIFACT_ID: Final = "apple-system"
ARTIFACT_VERSION: Final = "2026.09.1"
COMPILER_VERSION: Final = "2.0.0"
NORMALIZATION_POLICY_VERSION: Final = "text_lf_utf8_v1"
INVENTORY_VERSION: Final = "resemblio_apple_component_classification_matrix_v1"
EXPECTED_BINDING_INVENTORY_SHA256: Final = "9d6a03d6d6fc966b2a9a648b9cc0d1f66cfa657f11f746721d22fc7d5c0c84dc"
EXPECTED_PUBLIC_INVENTORY_SHA256: Final = "ec393780329578e479e9f5afbcdb79e7ca559d2da17970dd2addbe3a9c55d941"

API_ROOT: Final = Path(__file__).resolve().parents[1]
RESEMBLIO_ROOT: Final = Path(__file__).resolve().parents[3]
DEFAULT_MATRIX_PATH: Final = (
    RESEMBLIO_ROOT / "02-prd" / "apple-pilot" /
    "2026-09-09-apple-component-classification-matrix.json"
)
DEFAULT_CORPUS_ROOT: Final = API_ROOT / "_vendored" / "drl_corpus"
DEFAULT_PUBLIC_ARTIFACT_PATH: Final = API_ROOT / "app" / "data" / "apple_system_manifest.json"
DEFAULT_PRIVATE_LEDGER_PATH: Final = API_ROOT / "private" / "apple_evidence_ledger.json"

Classification = Literal["foundation", "component", "composition"]


class ManifestCompileError(ValueError):
    """Raised when compiler inputs fail integrity or contract validation."""


class ManifestValidationError(ValueError):
    """Raised when a public manifest is incomplete, unsafe, or inconsistent."""


class EvidenceReference(TypedDict):
    """Opaque public evidence lineage for one canonical record."""

    evidence_id: str
    evidence_sha256: str


class ManifestRecord(TypedDict):
    """One public canonical Apple system record."""

    record_id: str
    slug: str
    classification: Classification
    route: str
    aliases: list[str]
    readiness: dict[str, object]
    evidence: EvidenceReference
    facets: dict[str, object]
    facet_evidence_sha256: str
    relationships: dict[str, list[str]]
    compiler_version: str


class AppleSystemManifest(TypedDict):
    """Public content-addressed contract consumed across repositories."""

    schema_version: str
    artifact_id: str
    artifact_version: str
    compiler_version: str
    normalization_policy_version: str
    inventory_version: str
    inventory_sha256: str
    evidence_set_sha256: str
    artifact_sha256: str
    record_count: int
    breakdown: dict[str, int]
    attribution: dict[str, str]
    scrub_policy: dict[str, object]
    records: list[ManifestRecord]
    projections: dict[str, object]


class EvidenceLedgerRecord(TypedDict):
    """Private mapping from opaque evidence identity to vendored sources."""

    evidence_id: str
    record_id: str
    source_path: str
    asset_html_path: str
    asset_html_sha256: str
    tokens_css_path: str
    tokens_css_sha256: str
    evidence_sha256: str


class EvidenceLedger(TypedDict):
    """Private compiler ledger that is never attached to an API response."""

    schema_version: str
    compiler_version: str
    normalization_policy_version: str
    inventory_sha256: str
    evidence_set_sha256: str
    historical_context_sha256: str
    records: list[EvidenceLedgerRecord]


@dataclass(frozen=True)
class InventoryRecord:
    """Normalized private inventory input before public scrubbing."""

    record_id: str
    slug: str
    classification: Classification
    source_path: str
    route: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceSignals:
    """Neutral signals extracted deterministically from HTML and CSS."""

    tags: tuple[str, ...]
    landmarks: tuple[str, ...]
    roles: tuple[str, ...]
    accessibility: tuple[str, ...]
    states: tuple[str, ...]
    token_domains: tuple[str, ...]
    responsive: bool
    reduced_motion: bool
    duration_ms: int | None
    section_count: int
    interactive_count: int
    custom_property_count: int


@dataclass(frozen=True)
class CompileResult:
    """Validated public artifact and private ledger produced together."""

    public_manifest: AppleSystemManifest
    private_ledger: EvidenceLedger


_FOUNDATION_IDENTITIES: Final[dict[str, tuple[str, str]]] = {
    "assets/alphabets/apple": ("foundation:alphabet", "alphabet"),
    "assets/atoms/color-groups/apple-color-group-001": ("foundation:color-scale", "color-scale"),
    "assets/atoms/radius-scales/apple-radius-scale-001": ("foundation:radius-scale", "radius-scale"),
    "assets/atoms/shadow-scales/apple-shadow-scale-001": ("foundation:shadow-scale", "shadow-scale"),
    "assets/atoms/spacing-scales/apple-spacing-scale-001": ("foundation:spacing-scale", "spacing-scale"),
    "assets/atoms/swatches/apple-swatches-001": ("foundation:color-swatches", "color-swatches"),
    "assets/atoms/type-pairings/apple-type-pairing-001": ("foundation:type-pairing", "type-pairing"),
    "assets/atoms/type-specimens/apple-type-specimen-001": ("foundation:type-specimen", "type-specimen"),
    "assets/atoms/motion-primitives/apple-motion-system-001": ("foundation:motion-primitives", "motion-primitives"),
    "assets/atoms/spacing-scales/apple-layout-rhythm-001": ("foundation:layout-rhythm", "layout-rhythm"),
    "assets/libraries/apple": ("foundation:system-overview", "system-overview"),
}

_TAG_VOCABULARY: Final = frozenset({
    "a", "article", "aside", "button", "dialog", "fieldset", "footer",
    "form", "header", "input", "label", "li", "main", "nav", "ol",
    "section", "select", "textarea", "ul",
})
_LANDMARK_TAGS: Final = frozenset({"aside", "footer", "form", "header", "main", "nav", "section"})
_INTERACTIVE_TAGS: Final = frozenset({"a", "button", "input", "select", "textarea"})
_SAFE_ROLES: Final = frozenset({
    "alert", "button", "dialog", "list", "listbox", "menu", "navigation",
    "progressbar", "search", "status", "tab", "tablist", "tooltip",
})
_SAFE_A11Y_ATTRIBUTES: Final[dict[str, str]] = {
    "aria-controls": "controlled region",
    "aria-describedby": "described control",
    "aria-expanded": "expandable state",
    "aria-label": "accessible name",
    "aria-labelledby": "labelled control",
    "aria-live": "live announcement",
    "aria-modal": "modal behavior",
    "aria-pressed": "pressed state",
    "aria-selected": "selected state",
    "alt": "text alternative",
    "for": "explicit label association",
    "required": "required input",
}
_TOKEN_DOMAIN_PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    "color": re.compile(r"(?:color|background|border).*:", re.I),
    "typography": re.compile(r"(?:font|line-height|letter-spacing).*:", re.I),
    "spacing": re.compile(r"(?:margin|padding|gap|space).*:", re.I),
    "radius": re.compile(r"border-radius.*:", re.I),
    "shadow": re.compile(r"(?:box-shadow|text-shadow).*:", re.I),
    "motion": re.compile(r"(?:transition|animation|duration|easing).*:", re.I),
    "layout": re.compile(r"(?:display|grid|flex|width|height).*:", re.I),
}
_STATE_PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    "hover": re.compile(r":hover\b", re.I),
    "focus": re.compile(r":focus(?:-visible|-within)?\b", re.I),
    "disabled": re.compile(r"(?::disabled|\[disabled\])", re.I),
    "active": re.compile(r":active\b", re.I),
    "checked": re.compile(r"(?::checked|aria-checked)", re.I),
    "selected": re.compile(r"(?:aria-selected|data-selected)", re.I),
    "expanded": re.compile(r"aria-expanded", re.I),
    "loading": re.compile(r"(?:aria-busy|progress|loader|loading)", re.I),
    "empty": re.compile(r"(?:empty-state|:empty)", re.I),
}
_DURATION_RE: Final = re.compile(r"(?<![\w.-])(\d+(?:\.\d+)?)\s*(ms|s)\b", re.I)
_PRIVATE_LEAK_RE: Final = re.compile(
    r"(?:assets[\\/]|_vendored|design reference library|resemblio://|drl[-_/]|"
    r"https?://|www\.|<[^>]+>|@font-face|font-family|data-rs-source|"
    r"\.css\b|\.html\b|[a-z]:\\)",
    re.I,
)
_PROPRIETARY_IDENTITY_RE: Final = re.compile(
    r"(?:iphone|ipad|macos|cupertino|san francisco pro|sf pro|new york font|"
    r"myriad|product sans)",
    re.I,
)
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_RECORD_ID_RE: Final = re.compile(r"^(foundation|component|composition):[a-z0-9][a-z0-9-]*$")
_ROUTE_RE: Final = re.compile(r"^/library/apple(?:/[a-z0-9-]+)*/$")


class _SignalHTMLParser(html.parser.HTMLParser):
    """Collect only controlled-vocabulary HTML semantics."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: Counter[str] = Counter()
        self.roles: set[str] = set()
        self.accessibility: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Capture safe tag, role, and accessibility presence signals."""
        lowered_tag = tag.lower()
        if lowered_tag in _TAG_VOCABULARY:
            self.tags[lowered_tag] += 1
        for name, value in attrs:
            lowered_name = name.lower()
            if lowered_name == "role" and value and value.lower() in _SAFE_ROLES:
                self.roles.add(value.lower())
            safe_attribute = _SAFE_A11Y_ATTRIBUTES.get(lowered_name)
            if safe_attribute:
                self.accessibility.add(safe_attribute)


def canonical_json_bytes(value: object) -> bytes:
    """Return stable UTF-8 JSON bytes with sorted keys and one trailing LF."""
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    """Return a lowercase SHA-256 digest for bytes."""
    return hashlib.sha256(value).hexdigest()


def normalized_text_bytes(path: Path) -> bytes:
    """Read one UTF-8 evidence file with CRLF normalized to LF."""
    return path.read_bytes().replace(b"\r\n", b"\n")


def normalized_file_sha256(path: Path) -> str:
    """Hash one text file according to the pinned LF normalization policy."""
    return sha256_bytes(normalized_text_bytes(path))


def artifact_sha256(manifest: Mapping[str, object]) -> str:
    """Hash a manifest body while excluding its self-referential digest."""
    body = dict(manifest)
    body.pop("artifact_sha256", None)
    return sha256_bytes(canonical_json_bytes(body))


def _load_json(path: Path, label: str) -> dict[str, Any]:
    """Load an object-shaped JSON file or raise a path-safe compile error."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestCompileError(f"{label} is missing or invalid") from exc
    if not isinstance(value, dict):
        raise ManifestCompileError(f"{label} must be a JSON object")
    return cast(dict[str, Any], value)


def _canonical_route(slug: str) -> str:
    """Return the canonical public Apple route for a neutral record slug."""
    return f"/library/apple/{slug}/"


def normalize_inventory(matrix: Mapping[str, object]) -> list[InventoryRecord]:
    """Normalize the three binding matrix containers into 41 stable records."""
    if matrix.get("schema_version") != INVENTORY_VERSION:
        raise ManifestCompileError("inventory schema_version is unsupported")
    records: list[InventoryRecord] = []
    raw_foundations = matrix.get("records")
    if not isinstance(raw_foundations, list):
        raise ManifestCompileError("inventory foundation records are missing")
    for raw in raw_foundations:
        if not isinstance(raw, Mapping):
            raise ManifestCompileError("foundation inventory entry must be an object")
        source_path = raw.get("source_path")
        route = raw.get("target_route")
        if not isinstance(source_path, str) or source_path not in _FOUNDATION_IDENTITIES:
            raise ManifestCompileError("foundation source path lacks a stable neutral identity")
        if not isinstance(route, str):
            raise ManifestCompileError("foundation route is missing")
        record_id, slug = _FOUNDATION_IDENTITIES[source_path]
        records.append(InventoryRecord(record_id, slug, "foundation", source_path, route, ()))

    for classification, container_name in (
        ("component", "component_family_records"),
        ("composition", "composition_records"),
    ):
        raw_container = matrix.get(container_name)
        if not isinstance(raw_container, Mapping):
            raise ManifestCompileError(f"inventory {container_name} is missing")
        for raw_slug, raw_path in raw_container.items():
            if not isinstance(raw_slug, str) or not isinstance(raw_path, str):
                raise ManifestCompileError(f"inventory {container_name} entry is malformed")
            slug = raw_slug.removeprefix("compositions/")
            record_id = f"{classification}:{slug}"
            route_slug = raw_slug if classification == "composition" and raw_slug.startswith("compositions/") else slug
            route = f"/library/apple/{route_slug}/"
            aliases: tuple[str, ...] = ()
            if raw_slug == "forms":
                aliases = (_canonical_route("form-fields"),)
            elif raw_slug == "compositions/about":
                aliases = (_canonical_route("settings"),)
            records.append(InventoryRecord(
                record_id, slug, cast(Classification, classification), raw_path, route, aliases
            ))

    records.sort(key=lambda item: item.record_id)
    declared_count = matrix.get("record_count")
    if declared_count != 41 or len(records) != 41:
        raise ManifestCompileError("binding inventory must contain exactly 41 records")
    ids = [record.record_id for record in records]
    if len(ids) != len(set(ids)):
        raise ManifestCompileError("binding inventory contains duplicate stable identities")
    counts = Counter(record.classification for record in records)
    if counts != Counter({"foundation": 11, "component": 17, "composition": 13}):
        raise ManifestCompileError("binding inventory classification breakdown is invalid")
    return records


def _inventory_digest(records: Sequence[InventoryRecord]) -> str:
    """Hash the normalized private inventory, including source mappings."""
    body = [
        {
            "aliases": list(record.aliases),
            "classification": record.classification,
            "record_id": record.record_id,
            "route": record.route,
            "source_path": record.source_path,
        }
        for record in sorted(records, key=lambda item: item.record_id)
    ]
    return sha256_bytes(canonical_json_bytes(body))


def _public_inventory_digest(records: Sequence[Mapping[str, object]]) -> str:
    """Hash only neutral public identity, classification, route, and aliases."""
    body = [
        {
            "aliases": record.get("aliases"),
            "classification": record.get("classification"),
            "record_id": record.get("record_id"),
            "route": record.get("route"),
            "slug": record.get("slug"),
        }
        for record in sorted(records, key=lambda item: str(item.get("record_id", "")))
    ]
    return sha256_bytes(canonical_json_bytes(body))


def _corpus_paths(corpus: Mapping[str, object]) -> set[str]:
    """Return every asset path registered in the vendored corpus metadata."""
    paths: set[str] = set()
    systems = corpus.get("systems")
    if not isinstance(systems, list):
        raise ManifestCompileError("vendored corpus systems are missing")
    for system in systems:
        if not isinstance(system, Mapping):
            continue
        assets = system.get("assets")
        if not isinstance(assets, list):
            continue
        for asset in assets:
            if isinstance(asset, Mapping) and isinstance(asset.get("path"), str):
                paths.add(cast(str, asset["path"]))
    return paths


def _manifest_hashes(vendored_manifest: Mapping[str, object]) -> dict[str, str]:
    """Return the validated path-to-hash map from the pinned vendor manifest."""
    raw_files = vendored_manifest.get("files")
    if not isinstance(raw_files, list):
        raise ManifestCompileError("vendored integrity manifest files are missing")
    hashes: dict[str, str] = {}
    for item in raw_files:
        if not isinstance(item, Mapping):
            raise ManifestCompileError("vendored integrity entry is malformed")
        path = item.get("path")
        digest = item.get("sha256")
        if not isinstance(path, str) or not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise ManifestCompileError("vendored integrity entry is malformed")
        if path in hashes:
            raise ManifestCompileError("vendored integrity manifest has duplicate paths")
        hashes[path] = digest
    return hashes


def extract_evidence_signals(html_text: str, css_text: str) -> EvidenceSignals:
    """Extract neutral, bounded HTML and CSS evidence signals."""
    parser = _SignalHTMLParser()
    parser.feed(html_text)
    combined = f"{html_text}\n{css_text}"
    states = tuple(sorted(name for name, pattern in _STATE_PATTERNS.items() if pattern.search(combined)))
    token_domains = tuple(sorted(name for name, pattern in _TOKEN_DOMAIN_PATTERNS.items() if pattern.search(css_text)))
    durations: list[int] = []
    for number, unit in _DURATION_RE.findall(css_text):
        milliseconds = round(float(number) * (1000 if unit.lower() == "s" else 1))
        if 0 < milliseconds <= 60000:
            durations.append(milliseconds)
    tags = tuple(sorted(parser.tags))
    return EvidenceSignals(
        tags=tags,
        landmarks=tuple(sorted(set(tags) & _LANDMARK_TAGS)),
        roles=tuple(sorted(parser.roles)),
        accessibility=tuple(sorted(parser.accessibility)),
        states=states,
        token_domains=token_domains,
        responsive="@media" in css_text.lower(),
        reduced_motion="prefers-reduced-motion" in css_text.lower(),
        duration_ms=min(durations) if durations else None,
        section_count=parser.tags["section"],
        interactive_count=sum(parser.tags[tag] for tag in _INTERACTIVE_TAGS),
        custom_property_count=len(set(re.findall(r"(?<!-)--[a-z0-9_-]+\s*:", css_text, re.I))),
    )


def _not_applicable(reason: str) -> dict[str, str]:
    """Return the required explicit not-applicable facet shape."""
    return {"status": "not_applicable", "reason": reason}


def _motion_facet(signals: EvidenceSignals) -> dict[str, object]:
    """Build a bounded motion facet from measured evidence signals."""
    if signals.duration_ms is None and "motion" not in signals.token_domains:
        return _not_applicable("No motion declaration is present in this evidence record.")
    return {
        "duration_ms": signals.duration_ms or 1,
        "reduced_motion": "supported" if signals.reduced_motion else "not_observed",
        "state_driven": bool(signals.states),
    }


def _foundation_facets(record: InventoryRecord, signals: EvidenceSignals) -> dict[str, object]:
    """Build foundation-specific rule and scale facets."""
    return {
        "facet_type": "foundation",
        "foundation_kind": record.slug,
        "token_domains": list(signals.token_domains) or ["layout"],
        "custom_property_count": max(signals.custom_property_count, 1),
        "responsive": "supported" if signals.responsive else "not_observed",
        "motion": _motion_facet(signals),
        "component_anatomy": _not_applicable("Foundation records define reusable system rules rather than component anatomy."),
    }


def _component_facets(record: InventoryRecord, signals: EvidenceSignals) -> dict[str, object]:
    """Build component-specific anatomy, state, and accessibility facets."""
    anatomy = list(signals.tags) or ["section"]
    states = ["default", *signals.states]
    accessibility = sorted(set(signals.accessibility) | set(signals.roles))
    if not accessibility:
        accessibility = ["native semantics"]
    return {
        "facet_type": "component",
        "family": record.slug,
        "anatomy": anatomy,
        "states": states,
        "accessibility": accessibility,
        "interactive_element_count": max(signals.interactive_count, 1),
        "token_domains": list(signals.token_domains) or ["layout"],
        "responsive": "supported" if signals.responsive else "not_observed",
        "motion": _motion_facet(signals),
    }


def _composition_facets(record: InventoryRecord, signals: EvidenceSignals) -> dict[str, object]:
    """Build composition-specific landmarks, hierarchy, and behavior facets."""
    landmarks = list(signals.landmarks) or ["section"]
    return {
        "facet_type": "composition",
        "composition_kind": record.slug,
        "landmarks": landmarks,
        "section_count": max(signals.section_count, 1),
        "hierarchy_levels": max(sum(tag in signals.tags for tag in ("header", "main", "section", "footer")), 1),
        "interactive_element_count": signals.interactive_count,
        "token_domains": list(signals.token_domains) or ["layout"],
        "responsive": "supported" if signals.responsive else "not_observed",
        "motion": _motion_facet(signals),
    }


def _relationships(record: InventoryRecord) -> dict[str, list[str]]:
    """Derive stable record relationships from classification and record identity."""
    base_foundations = ["foundation:color-scale", "foundation:spacing-scale", "foundation:type-specimen"]
    if record.classification == "foundation":
        return {"foundations": [], "components": [], "compositions": []}
    if record.classification == "component":
        composition = {
            "buttons": "composition:cta-block",
            "links": "composition:navigation",
            "icon-buttons": "composition:navigation",
            "inputs": "composition:about",
            "search": "composition:navigation",
            "selection-controls": "composition:about",
            "segmented-controls": "composition:research",
            "forms": "composition:about",
            "product-cards": "composition:feature-grid",
            "editorial-cards": "composition:editorial",
            "comparison-tiles": "composition:feature-grid",
            "promo-panels": "composition:cta-block",
            "empty-states": "composition:research",
            "status-indicators": "composition:research",
            "loaders": "composition:research",
            "tooltips": "composition:research",
            "modal-sheet": "composition:launch",
        }[record.slug]
        return {"foundations": base_foundations, "components": [], "compositions": [composition]}
    component_map = {
        "article-layout": ["component:editorial-cards", "component:links"],
        "cta-block": ["component:buttons", "component:promo-panels"],
        "feature-grid": ["component:product-cards", "component:comparison-tiles"],
        "footer": ["component:links", "component:buttons"],
        "hero": ["component:buttons", "component:links"],
        "navigation": ["component:links", "component:icon-buttons", "component:search"],
        "news-list": ["component:editorial-cards", "component:links"],
        "process-steps": ["component:status-indicators", "component:links"],
        "testimonials": ["component:editorial-cards", "component:links"],
        "about": ["component:forms", "component:links"],
        "editorial": ["component:editorial-cards", "component:links"],
        "launch": ["component:buttons", "component:promo-panels"],
        "research": ["component:search", "component:segmented-controls"],
    }
    return {"foundations": base_foundations, "components": component_map[record.slug], "compositions": []}


def _build_facets(record: InventoryRecord, signals: EvidenceSignals) -> dict[str, object]:
    """Dispatch to the classification-specific public facet compiler."""
    if record.classification == "foundation":
        return _foundation_facets(record, signals)
    if record.classification == "component":
        return _component_facets(record, signals)
    return _composition_facets(record, signals)


def _facet_evidence_digest(record_id: str, evidence_sha256: str, facets: Mapping[str, object]) -> str:
    """Bind classification facets to the record identity and verified evidence."""
    return sha256_bytes(canonical_json_bytes({
        "evidence_sha256": evidence_sha256,
        "facets": facets,
        "record_id": record_id,
    }))


def _readiness() -> dict[str, object]:
    """Return the Phase 2 readiness state without overclaiming public completion."""
    return {
        "status": "evidence_compiled",
        "public_view_ready": False,
        "next_phase": "teaching_and_specimens",
    }


def _build_projections(records: Sequence[ManifestRecord]) -> dict[str, object]:
    """Derive consumer navigation, sitemap, teaching, and export projections."""
    by_classification: dict[str, list[str]] = {"foundation": [], "component": [], "composition": []}
    routes: set[str] = {"/library/apple/"}
    aliases: dict[str, str] = {}
    for record in records:
        by_classification[record["classification"]].append(record["record_id"])
        routes.add(record["route"])
        for alias in record["aliases"]:
            aliases[alias] = record["route"]
    return {
        "navigation": [
            {"group": key, "record_ids": sorted(value)}
            for key, value in by_classification.items()
        ],
        "sitemap_routes": sorted(routes),
        "aliases": dict(sorted(aliases.items())),
        "teaching": {key: sorted(value) for key, value in by_classification.items()},
        "export": {
            "artifact_id": ARTIFACT_ID,
            "artifact_version": ARTIFACT_VERSION,
            "record_ids": sorted(record["record_id"] for record in records),
        },
    }


def compile_apple_system_manifest(
    *,
    matrix_path: Path = DEFAULT_MATRIX_PATH,
    corpus_root: Path = DEFAULT_CORPUS_ROOT,
) -> CompileResult:
    """Compile and validate public and private Apple evidence artifacts.

    All file reads are constrained to the supplied matrix and vendored corpus.
    The function performs no writes; callers choose where to persist the two
    products only after validation succeeds.
    """
    matrix = _load_json(matrix_path, "binding inventory")
    inventory = normalize_inventory(matrix)
    inventory_digest = _inventory_digest(inventory)
    if inventory_digest != EXPECTED_BINDING_INVENTORY_SHA256:
        raise ManifestCompileError("binding inventory changed without an approved compiler lock update")
    neutral_inventory = [
        {
            "aliases": list(record.aliases),
            "classification": record.classification,
            "record_id": record.record_id,
            "route": record.route,
            "slug": record.slug,
        }
        for record in inventory
    ]
    if _public_inventory_digest(neutral_inventory) != EXPECTED_PUBLIC_INVENTORY_SHA256:
        raise ManifestCompileError("public inventory changed without an approved compiler lock update")
    corpus = _load_json(corpus_root / "corpus.json", "vendored corpus")
    vendored_manifest = _load_json(corpus_root / "manifest.json", "vendored integrity manifest")
    hashes = _manifest_hashes(vendored_manifest)
    if normalized_file_sha256(corpus_root / "corpus.json") != hashes.get("corpus.json"):
        raise ManifestCompileError("vendored corpus hash does not match its integrity manifest")
    registered_paths = _corpus_paths(corpus)
    public_records: list[ManifestRecord] = []
    ledger_records: list[EvidenceLedgerRecord] = []

    for record in inventory:
        if record.source_path not in registered_paths:
            raise ManifestCompileError("binding inventory source is absent from vendored corpus metadata")
        html_rel = f"{record.source_path}/asset.html"
        css_rel = f"{record.source_path}/tokens.css"
        html_path = corpus_root / PurePosixPath(html_rel)
        css_path = corpus_root / PurePosixPath(css_rel)
        expected_html_hash = hashes.get(html_rel)
        expected_css_hash = hashes.get(css_rel)
        if not html_path.is_file() or not css_path.is_file():
            raise ManifestCompileError("record evidence file is missing")
        if expected_html_hash is None or expected_css_hash is None:
            raise ManifestCompileError("record evidence is absent from the vendored integrity manifest")
        actual_html_hash = normalized_file_sha256(html_path)
        actual_css_hash = normalized_file_sha256(css_path)
        if actual_html_hash != expected_html_hash or actual_css_hash != expected_css_hash:
            raise ManifestCompileError("record evidence hash does not match the vendored integrity manifest")
        evidence_id = "ev_" + sha256_bytes(record.source_path.encode("utf-8"))[:24]
        evidence_digest = sha256_bytes(canonical_json_bytes({
            "asset_html_sha256": actual_html_hash,
            "tokens_css_sha256": actual_css_hash,
        }))
        signals = extract_evidence_signals(
            normalized_text_bytes(html_path).decode("utf-8"),
            normalized_text_bytes(css_path).decode("utf-8"),
        )
        facets = _build_facets(record, signals)
        public_records.append(ManifestRecord(
            record_id=record.record_id,
            slug=record.slug,
            classification=record.classification,
            route=record.route,
            aliases=list(record.aliases),
            readiness=_readiness(),
            evidence=EvidenceReference(evidence_id=evidence_id, evidence_sha256=evidence_digest),
            facets=facets,
            facet_evidence_sha256=_facet_evidence_digest(record.record_id, evidence_digest, facets),
            relationships=_relationships(record),
            compiler_version=COMPILER_VERSION,
        ))
        ledger_records.append(EvidenceLedgerRecord(
            evidence_id=evidence_id,
            record_id=record.record_id,
            source_path=record.source_path,
            asset_html_path=html_rel,
            asset_html_sha256=actual_html_hash,
            tokens_css_path=css_rel,
            tokens_css_sha256=actual_css_hash,
            evidence_sha256=evidence_digest,
        ))

    evidence_set_digest = sha256_bytes(canonical_json_bytes([
        {
            "evidence_id": record["evidence"]["evidence_id"],
            "evidence_sha256": record["evidence"]["evidence_sha256"],
        }
        for record in public_records
    ]))
    historical_path = corpus_root / "systems" / "apple" / "system.json"
    historical_digest = normalized_file_sha256(historical_path)
    if historical_digest != hashes.get("systems/apple/system.json"):
        raise ManifestCompileError("historical context hash does not match the integrity manifest")
    ledger = EvidenceLedger(
        schema_version=PRIVATE_LEDGER_SCHEMA_VERSION,
        compiler_version=COMPILER_VERSION,
        normalization_policy_version=NORMALIZATION_POLICY_VERSION,
        inventory_sha256=inventory_digest,
        evidence_set_sha256=evidence_set_digest,
        historical_context_sha256=historical_digest,
        records=ledger_records,
    )
    breakdown = Counter(record["classification"] for record in public_records)
    manifest = AppleSystemManifest(
        schema_version=SCHEMA_VERSION,
        artifact_id=ARTIFACT_ID,
        artifact_version=ARTIFACT_VERSION,
        compiler_version=COMPILER_VERSION,
        normalization_policy_version=NORMALIZATION_POLICY_VERSION,
        inventory_version=INVENTORY_VERSION,
        inventory_sha256=inventory_digest,
        evidence_set_sha256=evidence_set_digest,
        artifact_sha256="",
        record_count=len(public_records),
        breakdown={
            "foundations": breakdown["foundation"],
            "components": breakdown["component"],
            "compositions": breakdown["composition"],
        },
        attribution={
            "source_name": "Apple",
            "statement": "Inspired by Apple. No affiliation or endorsement.",
        },
        scrub_policy={
            "identity_removed": True,
            "source_attribution_retained": True,
            "private_evidence_excluded": True,
            "forbidden_content": [
                "source paths", "source URLs", "raw markup", "raw styles",
                "internal markers", "copied identity", "proprietary font names",
            ],
        },
        records=public_records,
        projections=_build_projections(public_records),
    )
    manifest["artifact_sha256"] = artifact_sha256(manifest)
    validate_public_manifest(manifest)
    return CompileResult(public_manifest=manifest, private_ledger=ledger)


def _safe_public_strings(value: object, *, path: tuple[str, ...] = ()) -> None:
    """Reject private evidence and copied identity outside allowed attribution."""
    if isinstance(value, str):
        if _PRIVATE_LEAK_RE.search(value):
            raise ManifestValidationError("public manifest contains private or raw evidence content")
        if _PROPRIETARY_IDENTITY_RE.search(value):
            raise ManifestValidationError("public manifest contains proprietary identity content")
        if "apple" in value.lower():
            allowed = (
                path[:1] == ("attribution",)
                or "aliases" in path
                or path[-1:] in (
                    ("artifact_id",),
                    ("schema_version",),
                    ("inventory_version",),
                    ("route",),
                    ("aliases",),
                    ("sitemap_routes",),
                )
            )
            if not allowed:
                raise ManifestValidationError("source identity is allowed only in attribution and routes")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _safe_public_strings(item, path=(*path, str(key)))
    elif isinstance(value, list):
        for item in value:
            _safe_public_strings(item, path=path)


def _require_nonempty(value: object, label: str) -> None:
    """Reject empty strings, lists, and mappings in applicable public facets."""
    if isinstance(value, str) and not value.strip():
        raise ManifestValidationError(f"{label} cannot be empty")
    if isinstance(value, (list, dict)) and not value:
        raise ManifestValidationError(f"{label} cannot be empty")


def _validate_not_applicable(value: object, label: str) -> bool:
    """Validate one explicit not-applicable facet and return whether it matched."""
    if not isinstance(value, Mapping) or value.get("status") != "not_applicable":
        return False
    if set(value) != {"status", "reason"} or not isinstance(value.get("reason"), str) or not value["reason"].strip():
        raise ManifestValidationError(f"{label} has an invalid not_applicable reason")
    return True


def _validate_facets(record: Mapping[str, object]) -> None:
    """Validate classification-specific facet shape and evidence binding."""
    classification = record.get("classification")
    facets = record.get("facets")
    if not isinstance(facets, Mapping) or facets.get("facet_type") != classification:
        raise ManifestValidationError("record facets do not match classification")
    required: dict[str, set[str]] = {
        "foundation": {"facet_type", "foundation_kind", "token_domains", "custom_property_count", "responsive", "motion", "component_anatomy"},
        "component": {"facet_type", "family", "anatomy", "states", "accessibility", "interactive_element_count", "token_domains", "responsive", "motion"},
        "composition": {"facet_type", "composition_kind", "landmarks", "section_count", "hierarchy_levels", "interactive_element_count", "token_domains", "responsive", "motion"},
    }
    if classification not in required or set(facets) != required[cast(str, classification)]:
        raise ManifestValidationError("record facets have missing or unsupported keys")
    for key, value in facets.items():
        if key == "motion" and _validate_not_applicable(value, "motion"):
            continue
        if key == "component_anatomy" and _validate_not_applicable(value, "component_anatomy"):
            continue
        _require_nonempty(value, f"facets.{key}")
    if classification == "foundation" and not _validate_not_applicable(facets.get("component_anatomy"), "component_anatomy"):
        raise ManifestValidationError("foundation component_anatomy must be not_applicable")
    if classification == "component":
        states = facets.get("states")
        if not isinstance(states, list) or "default" not in states:
            raise ManifestValidationError("component state coverage must include default")
    motion = facets.get("motion")
    if not _validate_not_applicable(motion, "motion"):
        if not isinstance(motion, Mapping) or set(motion) != {"duration_ms", "reduced_motion", "state_driven"}:
            raise ManifestValidationError("motion facet is malformed")
        duration = motion.get("duration_ms")
        if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
            raise ManifestValidationError("motion duration_ms must be a positive integer")
        if motion.get("reduced_motion") not in {"supported", "not_observed"} or not isinstance(motion.get("state_driven"), bool):
            raise ManifestValidationError("motion behavior is malformed")
    evidence = record.get("evidence")
    if not isinstance(evidence, Mapping) or not isinstance(evidence.get("evidence_sha256"), str):
        raise ManifestValidationError("record evidence is malformed")
    expected = _facet_evidence_digest(
        cast(str, record.get("record_id")),
        cast(str, evidence["evidence_sha256"]),
        cast(Mapping[str, object], facets),
    )
    if record.get("facet_evidence_sha256") != expected:
        raise ManifestValidationError("record facets are not bound to their evidence")


def validate_public_manifest(raw_manifest: Mapping[str, object]) -> AppleSystemManifest:
    """Validate and copy the complete production public manifest contract."""
    required_keys = {
        "schema_version", "artifact_id", "artifact_version", "compiler_version",
        "normalization_policy_version", "inventory_version", "inventory_sha256",
        "evidence_set_sha256", "artifact_sha256", "record_count", "breakdown",
        "attribution", "scrub_policy", "records", "projections",
    }
    if set(raw_manifest) != required_keys:
        raise ManifestValidationError("public manifest has missing or unsupported keys")
    if raw_manifest.get("schema_version") != SCHEMA_VERSION or raw_manifest.get("artifact_id") != ARTIFACT_ID:
        raise ManifestValidationError("public manifest identity is unsupported")
    for digest_key in ("inventory_sha256", "evidence_set_sha256", "artifact_sha256"):
        digest = raw_manifest.get(digest_key)
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise ManifestValidationError(f"{digest_key} must be a SHA-256 digest")
    if raw_manifest.get("artifact_sha256") != artifact_sha256(raw_manifest):
        raise ManifestValidationError("artifact body digest does not match")
    records = raw_manifest.get("records")
    if not isinstance(records, list) or len(records) != 41 or raw_manifest.get("record_count") != 41:
        raise ManifestValidationError("public manifest must contain exactly 41 records")
    if raw_manifest.get("inventory_sha256") != EXPECTED_BINDING_INVENTORY_SHA256:
        raise ManifestValidationError("inventory digest differs from the approved binding inventory lock")
    if raw_manifest.get("artifact_version") != ARTIFACT_VERSION or raw_manifest.get("compiler_version") != COMPILER_VERSION:
        raise ManifestValidationError("artifact or compiler version is unsupported")
    if raw_manifest.get("normalization_policy_version") != NORMALIZATION_POLICY_VERSION or raw_manifest.get("inventory_version") != INVENTORY_VERSION:
        raise ManifestValidationError("normalization or inventory version is unsupported")
    actual_ids: list[str] = []
    evidence_ids: list[str] = []
    routes: set[str] = set()
    aliases: set[str] = set()
    component_facet_digests: set[str] = set()
    counts: Counter[str] = Counter()
    record_keys = {
        "record_id", "slug", "classification", "route", "aliases", "readiness",
        "evidence", "facets", "facet_evidence_sha256", "relationships", "compiler_version",
    }
    for raw_record in records:
        if not isinstance(raw_record, Mapping) or set(raw_record) != record_keys:
            raise ManifestValidationError("public record has missing or unsupported keys")
        record_id = raw_record.get("record_id")
        classification = raw_record.get("classification")
        route = raw_record.get("route")
        if not isinstance(record_id, str) or _RECORD_ID_RE.fullmatch(record_id) is None:
            raise ManifestValidationError("record_id is malformed")
        if classification not in ("foundation", "component", "composition"):
            raise ManifestValidationError("record classification is unsupported")
        if not isinstance(route, str) or _ROUTE_RE.fullmatch(route) is None:
            raise ManifestValidationError("record route is malformed")
        raw_aliases = raw_record.get("aliases")
        if not isinstance(raw_aliases, list) or any(not isinstance(alias, str) or _ROUTE_RE.fullmatch(alias) is None for alias in raw_aliases):
            raise ManifestValidationError("record aliases are malformed")
        for alias in raw_aliases:
            if alias in aliases or alias in routes:
                raise ManifestValidationError("record alias collision")
            aliases.add(alias)
        if route in aliases:
            raise ManifestValidationError("record route collides with an alias")
        routes.add(route)
        readiness = raw_record.get("readiness")
        if not isinstance(readiness, Mapping) or readiness.get("status") != "evidence_compiled" or readiness.get("public_view_ready") is not False:
            raise ManifestValidationError("record readiness overclaims Phase 2 completion")
        evidence = raw_record.get("evidence")
        if not isinstance(evidence, Mapping) or set(evidence) != {"evidence_id", "evidence_sha256"}:
            raise ManifestValidationError("record evidence is malformed")
        evidence_id = evidence.get("evidence_id")
        evidence_digest = evidence.get("evidence_sha256")
        if not isinstance(evidence_id, str) or re.fullmatch(r"ev_[0-9a-f]{24}", evidence_id) is None:
            raise ManifestValidationError("record evidence_id is malformed")
        if not isinstance(evidence_digest, str) or _SHA256_RE.fullmatch(evidence_digest) is None:
            raise ManifestValidationError("record evidence digest is malformed")
        _validate_facets(raw_record)
        relationships = raw_record.get("relationships")
        if not isinstance(relationships, Mapping) or set(relationships) != {"foundations", "components", "compositions"}:
            raise ManifestValidationError("record relationships are malformed")
        for related in relationships.values():
            if not isinstance(related, list) or any(not isinstance(item, str) for item in related):
                raise ManifestValidationError("record relationship values are malformed")
        if raw_record.get("compiler_version") != COMPILER_VERSION:
            raise ManifestValidationError("record compiler version is unsupported")
        actual_ids.append(record_id)
        evidence_ids.append(evidence_id)
        counts[cast(str, classification)] += 1
        if classification == "component":
            component_facet_digests.add(cast(str, raw_record.get("facet_evidence_sha256")))
    if len(actual_ids) != len(set(actual_ids)):
        raise ManifestValidationError("record identities must be unique")
    if _public_inventory_digest(cast(Sequence[Mapping[str, object]], records)) != EXPECTED_PUBLIC_INVENTORY_SHA256:
        raise ManifestValidationError("public record inventory differs from the approved inventory lock")
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ManifestValidationError("public evidence IDs must be unique")
    expected_evidence_set = sha256_bytes(canonical_json_bytes([
        {
            "evidence_id": cast(Mapping[str, str], record["evidence"])["evidence_id"],
            "evidence_sha256": cast(Mapping[str, str], record["evidence"])["evidence_sha256"],
        }
        for record in records
    ]))
    if raw_manifest.get("evidence_set_sha256") != expected_evidence_set:
        raise ManifestValidationError("evidence set digest does not match public lineage")
    if counts != Counter({"foundation": 11, "component": 17, "composition": 13}):
        raise ManifestValidationError("record classification breakdown is invalid")
    if raw_manifest.get("breakdown") != {"foundations": 11, "components": 17, "compositions": 13}:
        raise ManifestValidationError("public manifest breakdown is invalid")
    if len(component_facet_digests) != 17:
        raise ManifestValidationError("component facets must be family-specific")
    relationships_ids = {
        related_id
        for record in records
        for related_values in cast(Mapping[str, list[str]], record["relationships"]).values()
        for related_id in related_values
    }
    if not relationships_ids.issubset(set(actual_ids)):
        raise ManifestValidationError("record relationship references an unknown identity")
    projections = raw_manifest.get("projections")
    expected_projections = _build_projections(cast(Sequence[ManifestRecord], records))
    if projections != expected_projections:
        raise ManifestValidationError("consumer projections do not match canonical records")
    _safe_public_strings(raw_manifest)
    return cast(AppleSystemManifest, deepcopy(dict(raw_manifest)))


def validate_private_ledger(ledger: Mapping[str, object], manifest: Mapping[str, object]) -> EvidenceLedger:
    """Validate private evidence resolution without exposing it to consumers."""
    if ledger.get("schema_version") != PRIVATE_LEDGER_SCHEMA_VERSION:
        raise ManifestValidationError("private ledger schema_version is unsupported")
    if ledger.get("inventory_sha256") != manifest.get("inventory_sha256") or ledger.get("evidence_set_sha256") != manifest.get("evidence_set_sha256"):
        raise ManifestValidationError("private ledger digests do not match the public artifact")
    raw_records = ledger.get("records")
    public_records = manifest.get("records")
    if not isinstance(raw_records, list) or not isinstance(public_records, list) or len(raw_records) != 41:
        raise ManifestValidationError("private ledger must resolve exactly 41 records")
    private_by_id = {
        item.get("evidence_id"): item
        for item in raw_records
        if isinstance(item, Mapping)
    }
    if len(private_by_id) != 41:
        raise ManifestValidationError("private ledger evidence IDs must be unique")
    for public in public_records:
        if not isinstance(public, Mapping) or not isinstance(public.get("evidence"), Mapping):
            raise ManifestValidationError("public record evidence is malformed")
        evidence = cast(Mapping[str, object], public["evidence"])
        private = private_by_id.get(evidence.get("evidence_id"))
        if not isinstance(private, Mapping) or private.get("evidence_sha256") != evidence.get("evidence_sha256"):
            raise ManifestValidationError("public evidence does not resolve exactly once in private ledger")
    return cast(EvidenceLedger, deepcopy(dict(ledger)))


def load_public_manifest(path: Path = DEFAULT_PUBLIC_ARTIFACT_PATH) -> AppleSystemManifest:
    """Load and validate the checked-in canonical public artifact."""
    return validate_public_manifest(_load_json(path, "public Apple system manifest"))


def component_manifest_slice(category_slug: str | None) -> dict[str, object] | None:
    """Return a backward-compatible component manifest slice from the artifact."""
    if category_slug is None:
        return None
    canonical_slug = "forms" if category_slug == "form-fields" else category_slug
    record_id = f"component:{canonical_slug}"
    record = next((item for item in load_public_manifest()["records"] if item["record_id"] == record_id), None)
    if record is None:
        return None
    facets = record["facets"]
    motion = facets["motion"]
    if isinstance(motion, Mapping) and motion.get("status") == "not_applicable":
        compatibility_motion: object = {"status": "not_applicable", "reason": cast(str, motion["reason"])}
    else:
        compatibility_motion = {
            "trigger": "state transition",
            "duration_ms": cast(Mapping[str, object], motion)["duration_ms"],
            "easing": "ease-out",
            "reduced_motion": "reduce nonessential transition",
            "loop": "none",
        }
    states = cast(list[str], facets["states"])
    anatomy = cast(list[str], facets["anatomy"])
    accessibility = cast(list[str], facets["accessibility"])
    return {
        "schema_version": "resemblio_component_manifest_v1",
        "family_id": canonical_slug,
        "role": f"{canonical_slug.replace('-', ' ')} control family",
        "anatomy": [{"name": item, "required": True} for item in anatomy],
        "slots": [{"name": anatomy[0], "required": True}],
        "variants": [{"name": "evidence", "values": ["primary", "secondary"]}],
        "states": states,
        "semantics": [f"implements the {canonical_slug.replace('-', ' ')} interaction pattern"],
        "accessibility": {
            "semantic_role": accessibility[0],
            "requirements": accessibility,
        },
        "keyboard": ["Enter", "Space where applicable"],
        "responsive": [cast(str, facets["responsive"])],
        "motion": compatibility_motion,
        "relationships": {
            "foundations": [item.removeprefix("foundation:") for item in record["relationships"]["foundations"]],
            "compositions": [item.removeprefix("composition:") for item in record["relationships"]["compositions"]],
            "dependent_components": [],
        },
        "token_references": [f"tokens.{item}.system" for item in cast(list[str], facets["token_domains"])],
        "implementation_constraints": [
            "Re-author all target copy and visual identity.",
            "Use target-system tokens rather than copied source expressions.",
        ],
    }


def readiness_projection() -> dict[str, dict[str, object]]:
    """Return readiness keyed by canonical record ID from the public artifact."""
    manifest = load_public_manifest()
    return {record["record_id"]: deepcopy(record["readiness"]) for record in manifest["records"]}


def write_compiled_artifacts(
    result: CompileResult,
    *,
    public_path: Path = DEFAULT_PUBLIC_ARTIFACT_PATH,
    private_path: Path = DEFAULT_PRIVATE_LEDGER_PATH,
) -> None:
    """Write validated compiler products using deterministic canonical bytes."""
    validate_public_manifest(result.public_manifest)
    validate_private_ledger(result.private_ledger, result.public_manifest)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.write_bytes(canonical_json_bytes(result.public_manifest))
    private_path.write_bytes(canonical_json_bytes(result.private_ledger))

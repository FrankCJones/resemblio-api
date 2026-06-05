"""Bridge from the FastAPI service to the existing Codex extractor.

DRL import precedence is fixed at module load:

1. The API vendored corpus at `_vendored/drl/drl`.
2. The local workspace DRL folder as a development fallback path.
3. A clear runtime failure if the vendored corpus is missing or broken.

The bridge validates vendored `_scripts` before loading `extractor.*`. That
keeps the extractor adapter bound to the shipped copy even though the adapter
also adds its historical workspace path.
"""
from __future__ import annotations

import importlib
import hashlib
import json
import os
import sys
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Iterator
from zipfile import ZIP_DEFLATED, ZipFile

if TYPE_CHECKING:
    from extractor.drl_adapter import TokenSet  # type-check only; safe at runtime
    from app.failure_modes import FailureCode  # noqa: F401

ToDtcgJson = Callable[[Any], dict[str, Any]]
ExtractorLoad = tuple[type[Any], int, type[Any], ToDtcgJson]

API_ROOT = Path(__file__).resolve().parents[1]
VENDORED_DRL_ROOT = API_ROOT / "_vendored" / "drl" / "drl"
WORKSPACE_DRL_ROOT = API_ROOT.parents[2] / "Design Reference Library"
DRL_STARTUP_ERROR = "DRL vendored corpus missing or broken; see _vendored/drl/README.md"
DRL_REQUIRED_MODULES = (
    "_scripts.extraction",
    "_scripts.fetch_html",
    "_scripts.recon",
    "_scripts.recon_ping",
)


class ExtractionBridgeError(RuntimeError):
    """Extractor failure surfaced as an API boundary error.

    Carries the raw extractor message plus a classified `FailureCode` so
    `POST /v1/extractions` can return a typed `error_code` to clients. The
    classification is computed at the bridge boundary via prefix-matching of
    the extractor's stable free-text format (see `app.failure_modes`).
    """

    def __init__(self, message: str, code: "FailureCode | None" = None) -> None:
        """Create a bridge error with an optional pre-classified code.

        When `code` is None the message is classified via
        `classify_extractor_error`; callers that already know the failure mode
        (e.g. `extract_design_tokens` for the "no tokens" sentinel) may pass it
        explicitly to bypass re-parsing.
        """
        super().__init__(message)
        from app.failure_modes import FailureCode, classify_extractor_error
        self.code: FailureCode = code if code is not None else classify_extractor_error(message)


def _prepend_sys_path(path: Path) -> None:
    """Move a path to the front of `sys.path` without duplicating entries."""
    path_text = str(path)
    if path_text in sys.path:
        sys.path.remove(path_text)
    sys.path.insert(0, path_text)


def _install_drl_paths() -> None:
    """Put the vendored DRL root before the optional workspace fallback."""
    if WORKSPACE_DRL_ROOT.exists():
        _prepend_sys_path(WORKSPACE_DRL_ROOT)
    _prepend_sys_path(VENDORED_DRL_ROOT)


def _clear_drl_module_cache() -> None:
    """Drop any already-imported `_scripts` modules before vendored reload."""
    for module_name in list(sys.modules):
        if module_name == "_scripts" or module_name.startswith("_scripts."):
            del sys.modules[module_name]


def _module_file(module: ModuleType) -> Path:
    """Return the resolved file path for a loaded module."""
    file_name = getattr(module, "__file__", None)
    if not file_name:
        raise RuntimeError(DRL_STARTUP_ERROR)
    return Path(file_name).resolve()


def _module_is_vendored(module: ModuleType) -> bool:
    """Return whether a loaded DRL module came from the vendored corpus."""
    try:
        _module_file(module).relative_to(VENDORED_DRL_ROOT.resolve())
    except ValueError:
        return False
    return True


def _load_required_drl_modules() -> dict[str, ModuleType]:
    """Import all DRL modules needed by the extractor adapter."""
    return {module_name: importlib.import_module(module_name) for module_name in DRL_REQUIRED_MODULES}


def _verify_vendored_drl() -> ModuleType:
    """Validate that required DRL modules load from the API vendored tree."""
    if not (VENDORED_DRL_ROOT / "_scripts" / "extraction.py").exists():
        raise RuntimeError(DRL_STARTUP_ERROR)
    _install_drl_paths()
    modules = _load_required_drl_modules()
    if not all(_module_is_vendored(module) for module in modules.values()):
        _clear_drl_module_cache()
        _install_drl_paths()
        modules = _load_required_drl_modules()
    if not all(_module_is_vendored(module) for module in modules.values()):
        raise RuntimeError(DRL_STARTUP_ERROR)

    extraction_module = modules["_scripts.extraction"]
    if not isinstance(getattr(extraction_module, "SCHEMA_VERSION", None), int):
        raise RuntimeError(DRL_STARTUP_ERROR)
    if not callable(getattr(extraction_module, "validate_token_set", None)):
        raise RuntimeError(DRL_STARTUP_ERROR)
    return extraction_module


VENDORED_DRL_EXTRACTION = _verify_vendored_drl()
VENDORED_DRL_SCHEMA_VERSION = int(getattr(VENDORED_DRL_EXTRACTION, "SCHEMA_VERSION"))


def _load_real_extractor() -> ExtractorLoad:
    """Lazy-import the DRL-backed extractor.

    Returns a tuple of (CodexExtractor, SCHEMA_VERSION, TokenSet, to_dtcg_json).
    Raises ExtractionBridgeError if the vendored DRL corpus is not reachable
    on this host.
    """
    try:
        from extractor.codex_extractor import CodexExtractor
        from extractor.drl_adapter import SCHEMA_VERSION, TokenSet, to_dtcg_json
        return CodexExtractor, SCHEMA_VERSION, TokenSet, to_dtcg_json
    except ImportError as exc:
        from app.failure_modes import FailureCode
        raise ExtractionBridgeError(
            f"Extractor unavailable on this host: {exc}. "
            "DRL vendored corpus missing or broken; see _vendored/drl/README.md.",
            code=FailureCode.INTERNAL_ERROR,
        ) from exc


def _load_extractor() -> ExtractorLoad:
    """Load the production extractor, keeping a test patch seam local."""
    return _load_real_extractor()


@dataclass(frozen=True)
class ExtractionBundle:
    """Successful extraction payload ready for persistence and R2 upload.

    ``palette_completeness_warning`` is the A1.1 (2026-06-04) additive
    field surfaced from the screenshot-palette pass. It is the list of
    lowercase hex strings the rendered page shows but the declared-token
    pipeline missed, or None when the screenshot pass was unavailable,
    errored, or returned a fully covered palette. The route handler
    threads this onto ``ExtractionResponse.palette_completeness_warning``
    on the freshly-created response only; cached reads via ``_response_for``
    return None because the warning is not persisted in the extraction
    row (the rendered-palette report is recomputed per extraction; the
    DB row carries only the final token set).
    """

    tokens_json: dict[str, Any]
    dtcg_json: dict[str, Any]
    zip_bytes: bytes
    extracted_at: datetime
    schema_version: int
    palette_completeness_warning: list[str] | None = None
    # S20 confidence rubric (R3-downstream cycle #2). Computed at bundle
    # build time from the same tokens + palette warning the response carries,
    # so the rubric values stay in sync with whatever the customer receives.
    # See ``extractor.confidence_rubric`` for the schema.
    confidence_rubric: dict[str, Any] | None = None


@dataclass(frozen=True)
class BundleManifest:
    """Manifest included in every extraction ZIP bundle."""

    schema_version: int
    url: str
    extracted_at: str
    tokens_sha256: str

    def as_json(self) -> dict[str, Any]:
        """Return a JSON-serializable manifest dict."""
        return {
            "schema_version": self.schema_version,
            "url": self.url,
            "extracted_at": self.extracted_at,
            "tokens_sha256": self.tokens_sha256,
        }


def extract_design_tokens(url: str) -> ExtractionBundle:
    """Run the existing extractor and package the result for the API.

    Reads ``last_palette_completeness_warning`` off the CodexExtractor
    instance after ``extract()`` and forwards it onto the bundle so the
    route handler can surface it on the API response. The warning is
    a side-effect of the screenshot-palette pass and only fires when
    that pass produced colors the declared pipeline missed; on
    extractor instances where the pass was skipped or returned no gap,
    the field is None.
    """
    CodexExtractor, _SCHEMA_VERSION, _TokenSet, _to_dtcg_json = _load_extractor()
    extractor_instance = CodexExtractor()
    with _without_extractor_db_url():
        token_set, error = extractor_instance.extract(url)
    if error is not None or token_set is None:
        from app.failure_modes import FailureCode
        if error is not None:
            # Bridge classifies the extractor's stable free-text format.
            raise ExtractionBridgeError(error)
        raise ExtractionBridgeError("extractor returned no tokens", code=FailureCode.NO_TOKENS_FOUND)
    warning: list[str] | None = getattr(
        extractor_instance, "last_palette_completeness_warning", None
    )
    return bundle_from_token_set(url, token_set, palette_completeness_warning=warning)


def bundle_from_token_set(
    url: str,
    token_set: "TokenSet",
    extracted_at: datetime | None = None,
    palette_completeness_warning: list[str] | None = None,
) -> ExtractionBundle:
    """Build DTCG JSON plus a ZIP bundle from a validated TokenSet.

    ``palette_completeness_warning`` is the optional A1.1 signal forwarded
    from the screenshot-palette pass via the extractor instance. It is
    additive and does NOT influence the DTCG payload or the ZIP bundle
    (which remain the canonical, stable customer-facing artifacts); it
    rides on the live extraction response only.
    """
    _CodexExtractor, SCHEMA_VERSION, _TokenSet, to_dtcg_json = _load_extractor()
    completed_at = extracted_at or datetime.now(timezone.utc)
    tokens_json = dict(token_set)
    dtcg_json = {"schema_version": SCHEMA_VERSION, **to_dtcg_json(token_set)}
    tokens_bytes = json.dumps(dtcg_json, sort_keys=True, separators=(",", ":")).encode("utf-8")
    manifest = BundleManifest(
        schema_version=SCHEMA_VERSION,
        url=url,
        extracted_at=completed_at.isoformat(),
        tokens_sha256=hashlib.sha256(tokens_bytes).hexdigest(),
    )
    zip_buffer = BytesIO()
    with ZipFile(zip_buffer, "w", ZIP_DEFLATED) as zip_file:
        zip_file.writestr("tokens.json", json.dumps(dtcg_json, indent=2, sort_keys=True))
        zip_file.writestr("manifest.json", json.dumps(manifest.as_json(), indent=2, sort_keys=True))
    # Compute the S20 confidence rubric from the same inputs the response
    # surface uses. Lazy import keeps the bridge module import-cheap for
    # tests that patch out the extractor entirely.
    from extractor.confidence_rubric import compute_confidence_rubric
    rubric = compute_confidence_rubric(tokens_json, palette_completeness_warning)
    return ExtractionBundle(
        tokens_json=tokens_json,
        dtcg_json=dtcg_json,
        zip_bytes=zip_buffer.getvalue(),
        extracted_at=completed_at,
        schema_version=SCHEMA_VERSION,
        palette_completeness_warning=palette_completeness_warning,
        confidence_rubric=dict(rubric),
    )


@contextmanager
def _without_extractor_db_url() -> Iterator[None]:
    """Disable the extractor's legacy optional persistence during API calls."""
    previous = os.environ.pop("RESEMBLIO_DB_URL", None)
    try:
        yield
    finally:
        if previous is not None:
            os.environ["RESEMBLIO_DB_URL"] = previous

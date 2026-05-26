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
    """Successful extraction payload ready for persistence and R2 upload."""

    tokens_json: dict[str, Any]
    dtcg_json: dict[str, Any]
    zip_bytes: bytes
    extracted_at: datetime
    schema_version: int


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
    """Run the existing extractor and package the result for the API."""
    CodexExtractor, _SCHEMA_VERSION, _TokenSet, _to_dtcg_json = _load_extractor()
    with _without_extractor_db_url():
        token_set, error = CodexExtractor().extract(url)
    if error is not None or token_set is None:
        from app.failure_modes import FailureCode
        if error is not None:
            # Bridge classifies the extractor's stable free-text format.
            raise ExtractionBridgeError(error)
        raise ExtractionBridgeError("extractor returned no tokens", code=FailureCode.NO_TOKENS_FOUND)
    return bundle_from_token_set(url, token_set)


def bundle_from_token_set(url: str, token_set: "TokenSet", extracted_at: datetime | None = None) -> ExtractionBundle:
    """Build DTCG JSON plus a ZIP bundle from a validated TokenSet."""
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
    return ExtractionBundle(
        tokens_json=tokens_json,
        dtcg_json=dtcg_json,
        zip_bytes=zip_buffer.getvalue(),
        extracted_at=completed_at,
        schema_version=SCHEMA_VERSION,
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

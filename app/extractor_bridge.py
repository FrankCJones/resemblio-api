"""Bridge from the FastAPI service to the existing Codex extractor."""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from typing import TYPE_CHECKING, Any, Iterator
from zipfile import ZIP_DEFLATED, ZipFile

if TYPE_CHECKING:
    from extractor.drl_adapter import TokenSet  # type-check only; safe at runtime


class ExtractionBridgeError(RuntimeError):
    """Extractor failure surfaced as an API boundary error."""


def _load_extractor():
    """Lazy-import the DRL-backed extractor.

    Returns a tuple of (CodexExtractor, SCHEMA_VERSION, TokenSet, to_dtcg_json).
    Raises ExtractionBridgeError if the upstream DRL _scripts package is not
    reachable on this host (production deploy when DRL is not vendored, etc).
    """
    try:
        from extractor.codex_extractor import CodexExtractor
        from extractor.drl_adapter import SCHEMA_VERSION, TokenSet, to_dtcg_json
        return CodexExtractor, SCHEMA_VERSION, TokenSet, to_dtcg_json
    except ImportError as exc:
        raise ExtractionBridgeError(
            f"Extractor unavailable on this host: {exc}. "
            "DRL _scripts/ is not reachable; deploy needs vendoring or a "
            "DRL-on-path link. See projects/Resemblio/code/api/CODEX_REPORT_S1.md "
            "and the v1.1 follow-up task list."
        ) from exc


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
        raise ExtractionBridgeError(error or "extractor returned no tokens")
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


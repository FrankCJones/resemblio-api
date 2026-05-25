"""Tests for the API vendored DRL import surface."""
from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

from app import extractor_bridge


def test_vendored_drl_module_resolves_from_api_tree() -> None:
    """The bridge should bind DRL helpers to the vendored API copy."""
    extraction = importlib.import_module("_scripts.extraction")
    assert isinstance(extractor_bridge.VENDORED_DRL_SCHEMA_VERSION, int)
    assert callable(getattr(extraction, "validate_token_set", None))
    assert _module_path(extraction).relative_to(extractor_bridge.VENDORED_DRL_ROOT.resolve())


def _module_path(module: ModuleType) -> Path:
    """Return a resolved module path for assertions."""
    file_name = getattr(module, "__file__", None)
    assert isinstance(file_name, str)
    return Path(file_name).resolve()

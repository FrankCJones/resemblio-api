"""Regression tests for production import topology of the extractor package.

The production deploy on `resemblio-prod-01` runs uvicorn with cwd
`/opt/resemblio-api/app/`, which puts that directory on `sys.path` but NOT its
`extractor/` subdirectory. A bare `import drl_adapter` (or
`from drl_adapter import ...`) inside the extractor package therefore fails at
runtime even when pytest hides the bug by adding `extractor` to its own
pythonpath. This test enforces the package-qualified import path so the
failure is caught in CI rather than in production.

Bug history: 502 from POST /v1/extractions on 2026-05-26 with
`No module named 'drl_adapter'`. Fix: qualify the import in
`extractor/codex_extractor.py` as `from extractor.drl_adapter import ...`.
"""
from __future__ import annotations

import importlib


def test_extractor_bridge_resolves_drl_adapter() -> None:
    """Importing extractor_bridge must resolve drl_adapter without sys.path hacks."""
    bridge = importlib.import_module("app.extractor_bridge")
    # The bridge lazy-loads the extractor; force the import path the route hits.
    CodexExtractor, schema_version, TokenSet, to_dtcg_json = bridge._load_real_extractor()
    assert CodexExtractor is not None
    assert isinstance(schema_version, int)
    assert TokenSet is not None
    assert callable(to_dtcg_json)


def test_codex_extractor_uses_qualified_drl_import() -> None:
    """codex_extractor must import drl_adapter via the `extractor` package."""
    module = importlib.import_module("extractor.codex_extractor")
    # The bound names come from the qualified module, not a bare top-level one.
    drl_adapter_module = importlib.import_module("extractor.drl_adapter")
    assert module.SCHEMA_VERSION is drl_adapter_module.SCHEMA_VERSION
    assert module.validate_token_set is drl_adapter_module.validate_token_set

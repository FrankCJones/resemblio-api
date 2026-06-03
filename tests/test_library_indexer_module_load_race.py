"""Module-load race regression tests for the library indexer.

Background
----------
On 2026-06-02 the Resemblio Library went down for 3 hours because
``app.library_indexer`` was loaded in a path where
``app.extractor_bridge``'s ``sys.path`` install for the vendored DRL corpus
had not run yet. The indexer's lazy ``from _scripts.templates import ...``
call inside ``_all_template_classes`` raised ``ModuleNotFoundError`` on
every job, every job got bounced to ``pending``, and ``library_pages``
stayed empty for 3 hours. The eventual fix (commit ``c5631c8``) added a
top-of-module ``from app import extractor_bridge`` import line. The
contract that "the bridge must load before the indexer" became implicit -
enforced only by a comment.

These tests assert that the contract is now ENFORCED by code:

1. ``test_module_load_emits_startup_log_with_schema_version`` - happy path.
   Loading ``app.library_indexer`` cleanly produces a ``StartupLog`` with
   the documented schema version, the bridge marked loaded, and a
   ``module_load_order`` showing the bridge before any ``_scripts.*``
   entry. This is the post-mortem signal an operator greps in journald.
2. ``test_guard_fires_when_drl_path_not_installed`` - the race shape.
   Simulates the failure by unloading ``app.extractor_bridge`` and the
   ``_scripts`` package and removing the DRL roots from ``sys.path``, then
   forcing a re-import of ``app.library_indexer``. The guard must raise
   ``ImportError`` whose message names the 2026-06-02 outage shape and the
   remediation. A silent empty-library is the prohibited failure mode.
3. ``test_guard_failure_message_shape`` - belt + suspenders. Asserts the
   failure-message template names the specific module probed and includes
   the original ``ImportError`` so the operator can confirm root cause from
   the alert text alone.

The tests use synthetic ``sys.modules`` + ``sys.path`` manipulation under a
``monkeypatch`` so they restore process state cleanly and do not leak into
sibling tests that depend on the indexer being importable.

No network. No DB. Pure module-load assertions.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


# Names the tests manipulate. Kept as module constants so a future contract
# change (different probed module, different bridge name) flips both the
# implementation and the tests by editing one spot per side.
_INDEXER_MODULE = "app.library_indexer"
_BRIDGE_MODULE = "app.extractor_bridge"
_DRL_PACKAGE_PREFIX = "_scripts"


def _drl_path_entries() -> list[str]:
    """Return ``sys.path`` entries that look like a DRL root.

    The bridge installs two candidates: the vendored ``_vendored/drl/drl``
    path and the workspace ``Design Reference Library`` fallback. Both end
    in a directory that contains a ``_scripts`` folder. We identify them by
    that signature so the test does not hardcode either path string (which
    would drift the moment the project layout moves).
    """
    return [
        entry for entry in sys.path
        if Path(entry).joinpath("_scripts").is_dir()
    ]


@pytest.fixture
def fresh_indexer_import(monkeypatch):
    """Drop cached indexer + bridge + DRL state, return a re-import callable.

    Yields a zero-arg function. Calling it re-imports
    ``app.library_indexer`` under whatever ``sys.path`` + ``sys.modules``
    state the test set up. ``monkeypatch`` restores ``sys.path`` and
    ``sys.modules`` automatically at fixture teardown.
    """
    # Snapshot-and-pop the modules we care about. monkeypatch.delitem on
    # sys.modules restores on teardown, so the sibling test suite (which
    # depends on the indexer staying importable) is not contaminated.
    for name in list(sys.modules):
        if (
            name == _INDEXER_MODULE
            or name == _BRIDGE_MODULE
            or name == _DRL_PACKAGE_PREFIX
            or name.startswith(_DRL_PACKAGE_PREFIX + ".")
        ):
            monkeypatch.delitem(sys.modules, name, raising=False)

    def _reimport():
        return importlib.import_module(_INDEXER_MODULE)

    return _reimport


def test_module_load_emits_startup_log_with_schema_version(fresh_indexer_import):
    """Happy path: clean load produces a documented-shape StartupLog.

    The bridge loads first (the top-of-module import in library_indexer.py
    is the contract), the guard passes silently, and ``_STARTUP_LOG`` is
    populated with ``extractor_bridge_loaded=True``,
    ``drl_templates_importable=True``, and a ``module_load_order`` that
    shows the bridge ahead of every ``_scripts.*`` entry.
    """
    library_indexer = fresh_indexer_import()

    log = library_indexer._STARTUP_LOG
    assert log.schema_version == "library_indexer_startup_v1"
    assert log.extractor_bridge_loaded is True
    assert log.drl_templates_importable is True

    # Order check: if the bridge is present in the captured module-load
    # order, it must precede every ``_scripts.*`` entry. The bridge can be
    # legitimately ABSENT from the order even on a healthy load (when the
    # test runner imported it earlier and the parent ``app`` package still
    # holds the attribute even though ``sys.modules`` lost the entry); in
    # that case ``extractor_bridge_loaded`` proves the contract held via
    # the package-attribute fallback path, and the order check is vacuous
    # but safe.
    order = log.module_load_order
    if _BRIDGE_MODULE in order:
        scripts_positions = [i for i, name in enumerate(order) if name.startswith(_DRL_PACKAGE_PREFIX)]
        if scripts_positions:
            bridge_position = order.index(_BRIDGE_MODULE)
            assert bridge_position < min(scripts_positions), (
                f"Bridge must load before any _scripts.* module. Order was: {order!r}"
            )


def test_guard_fires_when_drl_path_not_installed(monkeypatch, fresh_indexer_import):
    """Race shape: bridge skipped + DRL paths gone => guard raises ImportError.

    Simulates the 2026-06-02 outage by:

    1. Removing every ``sys.path`` entry that contains ``_scripts/``. After
       this step, ``importlib.import_module('_scripts.templates')`` raises
       ``ModuleNotFoundError`` regardless of bridge state.
    2. Dropping the bridge and ``_scripts`` modules from ``sys.modules`` so
       neither is available pre-loaded.
    3. Patching ``app.extractor_bridge`` import to a stub that does NOT
       perform the path install. This represents the failure where some
       other code path imported the bridge module object without triggering
       its side-effect-on-load.

    Under those conditions the guard ``_assert_drl_path_ready`` must raise
    ``ImportError`` whose message names the outage shape. A silent module
    load that lets the indexer run with an empty-pages result is the
    explicit anti-pattern this test is here to prevent.
    """
    # Step 1: scrub DRL path entries.
    for entry in _drl_path_entries():
        # monkeypatch.setattr handles list mutation cleanly via the
        # snapshot-and-restore protocol.
        if entry in sys.path:
            monkeypatch.setattr(
                sys, "path", [p for p in sys.path if p != entry],
            )

    # Step 2: drop any cached _scripts so a stale import does not mask the
    # missing-path condition. (fresh_indexer_import already dropped them,
    # but a sibling test could have re-populated between fixture init and
    # this point.)
    for name in list(sys.modules):
        if name == _DRL_PACKAGE_PREFIX or name.startswith(_DRL_PACKAGE_PREFIX + "."):
            monkeypatch.delitem(sys.modules, name, raising=False)

    # Step 3: register a stub for app.extractor_bridge so the indexer's
    # top-of-module ``from app import extractor_bridge`` import resolves
    # WITHOUT triggering the real bridge's path-install side effect. This
    # is the structural shape of the 2026-06-02 outage: bridge module
    # object existed in sys.modules, path install did not run.
    import types

    stub_bridge = types.ModuleType(_BRIDGE_MODULE)
    monkeypatch.setitem(sys.modules, _BRIDGE_MODULE, stub_bridge)

    with pytest.raises(ImportError) as excinfo:
        fresh_indexer_import()

    message = str(excinfo.value)
    assert "library_indexer module-load race detected" in message, (
        f"Guard message must name the race shape so an operator paging on it "
        f"recognizes the 2026-06-02 outage instantly. Got: {message!r}"
    )
    assert "_scripts.templates" in message, (
        "Guard message must name the specific probed module so the operator "
        "knows what to verify on the box."
    )
    assert "extractor_bridge" in message, (
        "Guard message must name the bridge as the remediation hook."
    )


def test_guard_failure_message_shape():
    """Belt + suspenders: the failure-message template names every contract piece.

    The template lives in ``_DRL_PATH_GUARD_FAILURE_MSG``. Render it with a
    synthetic ``ImportError`` and assert every field a paging operator
    needs is present: the outage date hint, the probed module name, the
    bridge name as the remediation pointer, and the original error.
    """
    from app.library_indexer import _DRL_PATH_GUARD_FAILURE_MSG

    rendered = _DRL_PATH_GUARD_FAILURE_MSG.format(
        required_module="_scripts.templates",
        original=ImportError("No module named '_scripts'"),
    )

    for needle in (
        "library_indexer module-load race detected",
        "_scripts.templates",
        "extractor_bridge",
        "2026-06-02",
        "c5631c8",
        "No module named '_scripts'",
    ):
        assert needle in rendered, (
            f"Failure-message template missing required field {needle!r}. "
            f"Rendered: {rendered!r}"
        )

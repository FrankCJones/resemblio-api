"""Stage 12 (TDD): assert every declared CLI entrypoint imports cleanly.

Background
==========
On 2026-06-02 the Library v1.1 deploy went green and 500'd every metadata
route for three hours because of a module-load shape failure that built
clean, tested clean, and only surfaced at request time. The same failure
class can bite the API CLI entrypoints: ``app.cli.library_indexer`` is
invoked from a systemd timer every 60 seconds on ``resemblio-prod-01``,
and a transitive import side effect that breaks ``python -m app.cli.X``
would silently degrade indexing without any HTTP probe ever noticing.

The shell-level gate (``ci/entrypoints.sh``) catches this in CI before
the deploy step. This pytest layer adds two complementary assertions
that run inside the standard pytest suite:

1. Every module under ``app.cli`` whose name does not start with ``_``
   runs ``python -m app.cli.<name> --help`` in a clean subprocess and
   exits 0 or 2 (argparse uses 2 for some help paths; both are clean).
2. The set of modules under ``app.cli`` matches the ``ENTRYPOINTS``
   array in ``ci/entrypoints.sh`` exactly. If a new CLI lands under
   ``app.cli/`` without being added to the shell array, this test
   fails - and vice versa.

Per CTO Stage 12 (`cto-reviews/2026-06-03-resemblio-back-on-track-tdd-
plan.md` Stage 12). Pairs with the existing ``ci/entrypoints.sh`` and
the ``Entrypoint smoke`` step in ``.github/workflows/deploy.yml``.

Why subprocess, not in-process import
=====================================
A module-load race only manifests when the module is the import root
(``python -m app.cli.X`` enters via the module's own ``__main__`` block).
An in-process ``importlib.import_module`` would mask the failure because
the test process has already imported a different graph of ``app.*``
modules through ``conftest.py``. Clean subprocess is the only honest
shape.

Why exit code 0 OR 2
====================
``argparse`` exits 0 on ``--help``. A module that uses ``argparse`` with
``add_help=True`` returns 0. A module that uses click or typer with
specific argument signatures can return 2 (argparse's "argument error"
code) for a bare ``--help``; both are clean exits, not crashes. Any
other exit code (1 from an unhandled exception, 127 from missing module,
etc.) is a failure.
"""
from __future__ import annotations

import os
import pkgutil
import re
import subprocess
import sys
from pathlib import Path

import pytest

import app.cli as _app_cli_pkg

# ---------------------------------------------------------------------------
# Path constants. Resolved relative to this file so the test works from any
# cwd (pytest from repo root, IDE from tests/, CI runner, etc.).
# ---------------------------------------------------------------------------

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
ENTRYPOINTS_SH: Path = REPO_ROOT / "ci" / "entrypoints.sh"

# Exit codes considered clean. See module docstring.
CLEAN_HELP_EXIT_CODES: frozenset[int] = frozenset({0, 2})

# Timeout per --help subprocess. Generous; a healthy --help finishes in
# under a second. If a CLI's import graph is so heavy that --help takes
# longer than this, that itself is a regression worth surfacing.
HELP_TIMEOUT_SECONDS: int = 30


def _discover_app_cli_modules() -> list[str]:
    """Return the dotted module paths of every public CLI under ``app.cli``.

    "Public" means the leaf name does not begin with ``_`` (so
    ``__init__`` and any future private helpers are excluded). Order is
    sorted so the parametrized test ids are stable across runs.

    Discovery uses ``pkgutil.iter_modules`` against the imported package's
    ``__path__``, which mirrors what ``python -m app.cli.<name>`` would
    resolve at runtime. This is the same surface ``ci/entrypoints.sh``
    must enumerate by hand; the parity test below catches drift.
    """
    discovered: list[str] = []
    for module_info in pkgutil.iter_modules(_app_cli_pkg.__path__):
        leaf = module_info.name
        if leaf.startswith("_"):
            continue
        discovered.append(f"app.cli.{leaf}")
    return sorted(discovered)


def _parse_entrypoints_sh(text: str) -> list[str]:
    """Extract the ENTRYPOINTS bash array values from ``ci/entrypoints.sh``.

    The shell file declares::

        ENTRYPOINTS=(
          "app.cli.library_indexer"
          "app.cli.sweep_idempotency"
        )

    We use a small regex over the literal block. Resilient to comments
    inside the block, trailing whitespace, and tabs vs spaces; brittle on
    purpose to anything more exotic (no command substitution, no
    variable expansion). If a future shape change breaks parsing, the
    parity test fails loud rather than silently desync.
    """
    match = re.search(
        r"ENTRYPOINTS\s*=\s*\(([^)]*)\)",
        text,
        re.DOTALL,
    )
    if not match:
        raise AssertionError(
            "Could not locate ENTRYPOINTS=(...) block in ci/entrypoints.sh. "
            "The shape may have changed; update _parse_entrypoints_sh to match."
        )
    block = match.group(1)
    entries: list[str] = []
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Each entry is a quoted dotted-module path, e.g. "app.cli.foo".
        quoted = re.match(r'"([^"]+)"', line)
        if quoted:
            entries.append(quoted.group(1))
    return sorted(entries)


# ---------------------------------------------------------------------------
# Discovery happens at module import. If it fails, every test below errors
# loudly rather than masking the discovery failure.
# ---------------------------------------------------------------------------

DISCOVERED_CLI_MODULES: list[str] = _discover_app_cli_modules()


@pytest.fixture(scope="module")
def entrypoints_sh_text() -> str:
    """Read ``ci/entrypoints.sh`` once per test module; fail loud if missing."""
    if not ENTRYPOINTS_SH.is_file():
        pytest.fail(
            f"ci/entrypoints.sh not found at expected path: {ENTRYPOINTS_SH}"
        )
    return ENTRYPOINTS_SH.read_text(encoding="utf-8")


def test_at_least_one_cli_module_discovered() -> None:
    """If ``app.cli`` is empty, parametrization below silently passes with zero
    cases. Assert the discovery list is non-empty so an accidental package
    rename or path bug surfaces here rather than as a quiet no-op.
    """
    assert DISCOVERED_CLI_MODULES, (
        "No CLI modules discovered under app.cli/. Expected at least one "
        "(e.g. app.cli.library_indexer). Did the package move?"
    )


@pytest.mark.parametrize("module_path", DISCOVERED_CLI_MODULES)
def test_cli_module_runs_help_cleanly_in_subprocess(module_path: str) -> None:
    """``python -m <module_path> --help`` must exit 0 or 2 in a fresh process.

    Any other exit code (1 from an unhandled exception during import, 127
    from a missing module, 139 from a segfault, etc.) is a failure. This
    catches transitive-import bugs that would otherwise only surface when
    the systemd timer fires on prod.
    """
    # PYTHONPATH = repo root so `app.*` is importable in the child. We do not
    # inherit the parent's PYTHONPATH because conftest.py mutates sys.path
    # implicitly via pytest's rootdir handling; the child needs an explicit
    # PYTHONPATH that mirrors the CI shape (`PYTHON -m app.cli.X` from the
    # repo root).
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    try:
        result = subprocess.run(
            [sys.executable, "-m", module_path, "--help"],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=HELP_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"{module_path} --help did not complete within "
            f"{HELP_TIMEOUT_SECONDS}s; import graph may be hung. "
            f"stdout: {exc.stdout!r} stderr: {exc.stderr!r}"
        )

    if result.returncode not in CLEAN_HELP_EXIT_CODES:
        pytest.fail(
            f"{module_path} --help exited with code {result.returncode}; "
            f"expected one of {sorted(CLEAN_HELP_EXIT_CODES)}.\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )


def test_entrypoints_sh_matches_discovered_cli_modules(
    entrypoints_sh_text: str,
) -> None:
    """The ENTRYPOINTS array in ``ci/entrypoints.sh`` must match the set of
    modules discovered under ``app.cli``.

    Drift in either direction is a bug:

    * Module under ``app.cli`` not in the shell array: the CI gate would
      not catch a module-load failure for that CLI.
    * Entry in the shell array not under ``app.cli``: the shell smoke
      would fail loud at deploy time on a non-existent module path.

    Fix by editing ``ci/entrypoints.sh`` to mirror the discovered set.
    """
    declared = _parse_entrypoints_sh(entrypoints_sh_text)
    discovered = DISCOVERED_CLI_MODULES

    missing_from_sh = set(discovered) - set(declared)
    extra_in_sh = set(declared) - set(discovered)

    assert not missing_from_sh and not extra_in_sh, (
        "ci/entrypoints.sh ENTRYPOINTS drift vs discovered app.cli modules.\n"
        f"  modules under app.cli/ but not in ci/entrypoints.sh: "
        f"{sorted(missing_from_sh) or '(none)'}\n"
        f"  entries in ci/entrypoints.sh but not under app.cli/: "
        f"{sorted(extra_in_sh) or '(none)'}\n"
        "Fix: edit ci/entrypoints.sh so the ENTRYPOINTS array matches the "
        "discovered module set, then re-run this test."
    )


# ---------------------------------------------------------------------------
# Pure-data unit tests on the shell parser. These have no I/O and are the
# tests that protect the regex from silent drift.
# ---------------------------------------------------------------------------


def test_parse_entrypoints_sh_extracts_quoted_entries() -> None:
    """The parser pulls each quoted entry from the ENTRYPOINTS block."""
    fixture = (
        "set -euo pipefail\n"
        'ENTRYPOINTS=(\n'
        '  "app.cli.alpha"\n'
        '  "app.cli.beta"\n'
        ")\n"
    )
    assert _parse_entrypoints_sh(fixture) == ["app.cli.alpha", "app.cli.beta"]


def test_parse_entrypoints_sh_ignores_comments_and_blank_lines() -> None:
    """Comments inside the array block are skipped; blanks are skipped."""
    fixture = (
        'ENTRYPOINTS=(\n'
        "  # leading comment\n"
        "\n"
        '  "app.cli.alpha"\n'
        "  # trailing comment\n"
        '  "app.cli.beta"\n'
        ")\n"
    )
    assert _parse_entrypoints_sh(fixture) == ["app.cli.alpha", "app.cli.beta"]


def test_parse_entrypoints_sh_raises_on_missing_block() -> None:
    """If the ENTRYPOINTS=() block is missing, the parser surfaces it loudly."""
    fixture = "#!/usr/bin/env bash\nset -euo pipefail\necho hello\n"
    with pytest.raises(AssertionError, match="ENTRYPOINTS"):
        _parse_entrypoints_sh(fixture)


def test_parse_entrypoints_sh_returns_sorted_output() -> None:
    """Output ordering is sorted so callers can compare sets stably."""
    fixture = (
        'ENTRYPOINTS=(\n'
        '  "app.cli.zeta"\n'
        '  "app.cli.alpha"\n'
        '  "app.cli.mu"\n'
        ")\n"
    )
    assert _parse_entrypoints_sh(fixture) == [
        "app.cli.alpha",
        "app.cli.mu",
        "app.cli.zeta",
    ]

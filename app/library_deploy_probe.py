"""Library indexer deploy probe - live systemctl state gatherer.

This module bridges the gap between the pure ``evaluate_deploy_state``
evaluator (which takes a ``DeployCheckState`` struct and has no I/O) and
the live system. It provides:

1. ``parse_systemctl_output`` - a pure function that maps raw ``systemctl``
   stdout/exit-code pairs to booleans. Pure, no I/O, fully testable.

2. ``gather_deploy_state`` - the thin live caller. Accepts an injected
   command-runner so it is testable with a fake runner and only shells
   out when called from an operator session.

Together they form the pipeline:
  gather_deploy_state(runner) -> DeployCheckState
  evaluate_deploy_state(state) -> DeployCheckResult

Why separate from ``library_deploy_selfcheck``
----------------------------------------------
``library_deploy_selfcheck`` is intentionally pure - it evaluates a struct,
emits a result, never touches the OS. Keeping I/O here preserves that
invariant: the selfcheck module can be imported and tested in any context
(CI, local, operator session) without mocking OS calls.

The oneshot ``service_active`` subtlety (see below) lives here because it
is about interpreting live system state, not about the evaluation logic.

Oneshot ``is-active`` subtlety
-------------------------------
The library indexer runs as a ``Type=oneshot`` systemd service: it drains a
batch, logs a tick line, and exits 0. Between ticks ``systemctl is-active
resemblio-library-indexer.service`` reports ``inactive``, NOT ``active``. This
is correct behavior for a oneshot unit and must NOT be treated as a failure.

``service_active`` in ``DeployCheckState`` is therefore derived from the last
run's exit status (``systemctl show -p ExecMainStatus``) rather than from
``is-active``. When ``ExecMainStatus=0`` the most recent run succeeded.
When the timer has never fired, ``ExecMainStatus=-1`` (or similar system
default); we treat ``!=0`` as ``service_active=False``.

``service_active=False`` is NOT a hard gate in ``evaluate_deploy_state``
(the service only runs when the timer fires; a fresh deploy that has not
yet ticked will have no recorded run). Operators inspect it separately.

Authorization notes
-------------------
This module only reads system state. The prod ops that fix a failing check
(``sudo systemctl enable --now``, ``git pull``) are Phase 5 YELLOW/GREEN gates
in the plan and are NOT invoked here.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.library_deploy_selfcheck import (
    LIBRARY_INDEXER_SERVICE_UNIT,
    LIBRARY_INDEXER_TIMER_UNIT,
    DeployCheckState,
    DeployCheckResult,
    evaluate_deploy_state,
)


# ---------------------------------------------------------------------------
# Constants: systemctl output sentinels
# ---------------------------------------------------------------------------

# ``systemctl is-enabled`` stdout values that mean the unit will start on boot.
# ``enabled-runtime`` means the unit was enabled via ``--runtime`` (not persistent
# across reboots); we include it here so the check passes in environments that
# use runtime enablement. Extend if a new active-equivalent state is observed.
TIMER_ENABLED_STATES: frozenset[str] = frozenset({"enabled", "enabled-runtime"})

# ``systemctl is-enabled`` values that mean the unit will NOT start on boot.
TIMER_DISABLED_STATES: frozenset[str] = frozenset(
    {"disabled", "masked", "not-found", "static", "indirect", ""}
)

# ``systemctl is-active`` stdout for the timer unit.
TIMER_ACTIVE_VALUE = "active"

# ExecMainStatus exit code that means the most recent oneshot run succeeded.
SERVICE_SUCCESS_EXIT_CODE = "0"


# ---------------------------------------------------------------------------
# CommandRunner type: thin callable wrapping subprocess.run
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CommandResult:
    """Result of a single command run.

    ``stdout`` is stripped of surrounding whitespace; ``exit_code`` is the
    process exit code (0 = success). Both are needed to interpret systemctl
    output because ``is-enabled`` on a missing unit returns a non-zero exit
    code AND a non-empty stdout string (``not-found``).
    """

    stdout: str
    exit_code: int


# A CommandRunner is any callable that takes a list of string args and returns
# a CommandResult. The live implementation shells out; the test implementation
# uses an injected mapping.
CommandRunner = Callable[[list[str]], CommandResult]


def live_runner(args: list[str]) -> CommandResult:
    """Shell out to the real system. Use only from an operator session.

    Captures stdout; does not raise on non-zero exit codes (callers
    interpret the exit code explicitly).
    """
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
    )
    return CommandResult(stdout=proc.stdout.strip(), exit_code=proc.returncode)


# ---------------------------------------------------------------------------
# Pure parsing functions (no I/O; fully testable)
# ---------------------------------------------------------------------------

def parse_timer_enabled(stdout: str) -> bool:
    """Return True when ``systemctl is-enabled <timer>`` stdout signals active enablement.

    Matches against ``TIMER_ENABLED_STATES`` (case-insensitive) so that
    a trailing newline or mixed-case variant does not produce a false negative.

    Args:
        stdout: stripped stdout from ``systemctl is-enabled <unit>``.

    Returns:
        True if the unit is persistently enabled; False otherwise.
    """
    return stdout.strip().lower() in TIMER_ENABLED_STATES


def parse_timer_active(stdout: str) -> bool:
    """Return True when ``systemctl is-active <timer>`` stdout is 'active'.

    For the TIMER unit (not the oneshot service), ``is-active=active`` is
    the correct healthy state - the timer is waiting to fire on schedule.

    Args:
        stdout: stripped stdout from ``systemctl is-active <timer-unit>``.

    Returns:
        True if the timer is currently active (waiting to fire).
    """
    return stdout.strip().lower() == TIMER_ACTIVE_VALUE


def parse_service_active_from_exec_status(stdout: str) -> bool:
    """Derive service_active from ``systemctl show -p ExecMainStatus`` output.

    For a ``Type=oneshot`` service, ``is-active`` returns ``inactive`` between
    ticks (correct systemd behavior). We instead read ``ExecMainStatus`` which
    is the numeric exit code of the most recent ``ExecStart`` invocation.

    ``ExecMainStatus=0`` -> last run succeeded -> service_active=True.
    Any other value (``-1`` for never-run, or a non-zero exit code) -> False.

    The raw stdout from ``systemctl show -p ExecMainStatus
    resemblio-library-indexer.service`` is: ``ExecMainStatus=<N>``

    Args:
        stdout: stripped stdout from the ``systemctl show`` command.

    Returns:
        True if the last run exited 0; False if never run or last run failed.
    """
    # Expected format: "ExecMainStatus=0"
    _, _, value = stdout.partition("=")
    return value.strip() == SERVICE_SUCCESS_EXIT_CODE


def parse_unit_file_present(unit_file_path: Optional[str]) -> bool:
    """Return True when the given path points to an existing file.

    Args:
        unit_file_path: absolute path to the unit file on the system, or None.

    Returns:
        True if the file exists at that path.
    """
    if unit_file_path is None:
        return False
    return Path(unit_file_path).is_file()


# ---------------------------------------------------------------------------
# State gathering (thin; injected runner)
# ---------------------------------------------------------------------------

def gather_deploy_state(
    runner: CommandRunner,
    *,
    unit_file_path: str = f"/etc/systemd/system/{LIBRARY_INDEXER_TIMER_UNIT}",
    drl_templates_importable: bool,
    library_pages_count: int = 0,
) -> DeployCheckState:
    """Probe the live system and return a ``DeployCheckState``.

    Separates state-gathering (this function) from state-evaluation
    (``evaluate_deploy_state``) so both are independently testable.
    The runner callable is injected so tests can supply a fake that returns
    controlled output without touching the OS.

    Args:
        runner: a ``CommandRunner`` that executes a command and returns
            ``CommandResult``. Use ``live_runner`` from an operator session;
            use a fake mapping in tests.
        unit_file_path: absolute path where the timer unit file should be
            installed on the target system. Defaults to the canonical
            ``/etc/systemd/system/`` path on a prod box.
        drl_templates_importable: the caller supplies this because testing
            DRL importability requires a Python sys.path probe that belongs
            in the caller's context (the caller knows whether it is running
            from a prod venv or a test harness). Pass the result of:
            ``importlib.util.find_spec("_scripts.templates") is not None``
            (or the equivalent for the DRL module path in use).
        library_pages_count: pass the current count from the DB if available;
            forwarded to ``DeployCheckState`` for informational reporting only.

    Returns:
        ``DeployCheckState`` ready to feed into ``evaluate_deploy_state``.
    """
    enabled_result = runner(["systemctl", "is-enabled", LIBRARY_INDEXER_TIMER_UNIT])
    active_result = runner(["systemctl", "is-active", LIBRARY_INDEXER_TIMER_UNIT])
    exec_status_result = runner(
        [
            "systemctl",
            "show",
            "-p",
            "ExecMainStatus",
            LIBRARY_INDEXER_SERVICE_UNIT,
        ]
    )

    return DeployCheckState(
        unit_file_present=parse_unit_file_present(unit_file_path),
        timer_enabled=parse_timer_enabled(enabled_result.stdout),
        timer_active=parse_timer_active(active_result.stdout),
        service_active=parse_service_active_from_exec_status(exec_status_result.stdout),
        drl_templates_importable=drl_templates_importable,
        library_pages_count=library_pages_count,
    )


# ---------------------------------------------------------------------------
# Combined pipeline (convenience)
# ---------------------------------------------------------------------------

def probe_and_evaluate(
    runner: CommandRunner,
    *,
    unit_file_path: str = f"/etc/systemd/system/{LIBRARY_INDEXER_TIMER_UNIT}",
    drl_templates_importable: bool,
    library_pages_count: int = 0,
) -> DeployCheckResult:
    """Gather state then evaluate it; return a ``DeployCheckResult``.

    This is the entry point for an operator session. Call ``evaluate_deploy_state``
    directly if you already have a ``DeployCheckState`` (e.g. from tests).

    Args:
        runner: command runner (use ``live_runner`` from a prod session).
        unit_file_path: canonical path to the installed timer unit file.
        drl_templates_importable: see ``gather_deploy_state``.
        library_pages_count: current ``library_pages`` row count (informational).

    Returns:
        ``DeployCheckResult`` with ``ok``, ``failing_checks``, and
        ``passing_checks`` populated.
    """
    state = gather_deploy_state(
        runner,
        unit_file_path=unit_file_path,
        drl_templates_importable=drl_templates_importable,
        library_pages_count=library_pages_count,
    )
    return evaluate_deploy_state(state)

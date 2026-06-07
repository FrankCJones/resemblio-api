"""Library indexer deploy self-check (Phase 5).

Pure evaluation module: given a structured state dict describing the prod
environment, returns a typed result indicating whether the indexer is
correctly deployed. No SSH, no I/O - those are the caller's responsibility.

The caller (a deploy script or the ``verify_drl_bootstrap.py`` harness) probes
the live state via ``systemctl is-enabled``/``systemctl is-active`` and passes
the results in as a ``DeployCheckState``. This module just interprets them.

Why a pure evaluation function rather than direct ``systemctl`` calls
----------------------------------------------------------------------
The deploy check runs from different contexts: CI (no prod SSH), a local
operator shell (has SSH), and the Phase 5 gated prod-ops step (authenticated
session). Separating the state-gathering from the evaluation means the
evaluation is unit-testable without mocking OS calls, and the state-gathering
can be implemented differently per context.

The prod ops that fix a failing check (``systemctl enable --now``) are Phase 5
YELLOW gates; this module only reports the current state, never mutates it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

LIBRARY_DEPLOY_SELFCHECK_SCHEMA_VERSION = "library_deploy_selfcheck_v1"
"""Bumped when DeployCheckResult shape changes."""

LIBRARY_INDEXER_TIMER_UNIT = "resemblio-library-indexer.timer"
"""Canonical name of the timer unit that fires the indexer every 60 seconds."""

LIBRARY_INDEXER_SERVICE_UNIT = "resemblio-library-indexer.service"
"""Canonical name of the indexer service unit."""


@dataclass(frozen=True)
class DeployCheckState:
    """Observed state of the library indexer deployment.

    All fields are booleans or counters; the caller populates them from
    ``systemctl`` output and importlib probes.
    """

    unit_file_present: bool
    """True when the timer unit file exists in /etc/systemd/system/ (or
    deploy/systemd/ locally)."""

    timer_enabled: bool
    """True when ``systemctl is-enabled resemblio-library-indexer.timer``
    exits 0 (unit will start on boot)."""

    timer_active: bool
    """True when ``systemctl is-active resemblio-library-indexer.timer``
    exits 0 (timer is currently running)."""

    service_active: bool
    """True when the most recent service invocation exited 0.
    False when the timer has never fired or the last run failed."""

    drl_templates_importable: bool
    """True when ``_scripts.templates`` is importable (DRL sys.path install
    ran before this check). Mirrors the library_indexer startup guard."""

    library_pages_count: int = 0
    """Current count of rows in library_pages. Used for informational
    reporting; not a pass/fail criterion in Phase 5 (seeding may not have
    run yet)."""


@dataclass(frozen=True)
class DeployCheckResult:
    """Result of evaluating a DeployCheckState.

    ``ok=True`` means the indexer is deployed correctly and should be
    picking up pending jobs. ``ok=False`` means at least one check failed;
    ``failing_checks`` names which ones so the operator knows what to fix.
    """

    schema_version: str
    ok: bool
    failing_checks: tuple[str, ...]
    """Sorted names of the checks that failed. Empty when ok=True."""

    passing_checks: tuple[str, ...]
    """Sorted names of the checks that passed."""

    library_pages_count: int
    """Passed through from DeployCheckState for informational logging."""


def evaluate_deploy_state(state: DeployCheckState) -> DeployCheckResult:
    """Evaluate a DeployCheckState and return a typed result.

    Checks:
      unit_file:    state.unit_file_present must be True.
      timer_enabled: state.timer_enabled must be True.
      timer_active:  state.timer_active must be True.
      drl_importable: state.drl_templates_importable must be True.

    ``service_active`` is recorded but not a hard gate: the service fires
    only when the timer triggers, and in a fresh deploy the timer may not
    have fired yet. The operator inspects service_active separately.

    Args:
        state: the observed deployment state.

    Returns:
        ``DeployCheckResult`` with ``ok``, ``failing_checks``, and
        ``passing_checks``.
    """
    failing: list[str] = []
    passing: list[str] = []

    def _check(name: str, *, condition: bool) -> None:
        if condition:
            passing.append(name)
        else:
            failing.append(name)

    _check("unit_file", condition=state.unit_file_present)
    _check("timer_enabled", condition=state.timer_enabled)
    _check("timer_active", condition=state.timer_active)
    _check("drl_importable", condition=state.drl_templates_importable)

    return DeployCheckResult(
        schema_version=LIBRARY_DEPLOY_SELFCHECK_SCHEMA_VERSION,
        ok=len(failing) == 0,
        failing_checks=tuple(sorted(failing)),
        passing_checks=tuple(sorted(passing)),
        library_pages_count=state.library_pages_count,
    )

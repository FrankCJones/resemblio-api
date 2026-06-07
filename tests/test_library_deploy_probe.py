"""Tests for app.library_deploy_probe - live systemctl state gatherer.

These tests are pure-data and offline. They drive the pure parsing functions
directly and inject a fake command runner into ``gather_deploy_state`` so the
test suite never shells out to systemctl.

The critical behavioral contract documented in these tests:
  - ``parse_timer_enabled``: distinguishes "enabled"/"enabled-runtime" from
    "disabled"/"masked"/"not-found"/"static"/"" (the full set systemctl emits).
  - ``parse_timer_active``: only "active" is active; "inactive"/"failed" are not.
  - ``parse_service_active_from_exec_status``: a ``Type=oneshot`` service
    reports ``inactive`` from ``is-active`` between ticks - correct behavior.
    ``ExecMainStatus=0`` is the only signal that means the last run succeeded.
    Any other value (``-1`` for never-run; non-zero for failure) maps to False.
  - ``gather_deploy_state``: composes the above parsers with a runner to produce
    a ``DeployCheckState`` that feeds ``evaluate_deploy_state``.
  - ``probe_and_evaluate``: convenience wrapper that chains both steps.

Authorization note: these tests run entirely offline. The gated prod ops
are Frank/Jim gates and are not part of this test harness.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from app.library_deploy_probe import (
    TIMER_ACTIVE_VALUE,
    TIMER_DISABLED_STATES,
    TIMER_ENABLED_STATES,
    CommandResult,
    CommandRunner,
    gather_deploy_state,
    parse_service_active_from_exec_status,
    parse_timer_active,
    parse_timer_enabled,
    parse_unit_file_present,
    probe_and_evaluate,
)
from app.library_deploy_selfcheck import (
    LIBRARY_INDEXER_SERVICE_UNIT,
    LIBRARY_INDEXER_TIMER_UNIT,
    evaluate_deploy_state,
)


# ---------------------------------------------------------------------------
# Fake runner helpers
# ---------------------------------------------------------------------------

def _make_runner(responses: dict[str, CommandResult]) -> CommandRunner:
    """Build a fake runner that returns preset ``CommandResult`` values.

    The key in ``responses`` is the space-joined command args, e.g.:
    ``"systemctl is-enabled resemblio-library-indexer.timer"``.
    Raises ``KeyError`` on unrecognised commands so tests surface missing stubs
    quickly rather than silently returning an empty result.
    """
    def _runner(args: list[str]) -> CommandResult:
        key = " ".join(args)
        if key not in responses:
            raise KeyError(
                f"Fake runner has no stub for command: {key!r}. "
                f"Available: {list(responses)}"
            )
        return responses[key]

    return _runner


def _healthy_runner(*, unit_file_path: str = "/tmp/never-used") -> CommandRunner:
    """Return a fake runner where the indexer is fully healthy.

    ``ExecMainStatus=0`` means the last oneshot run succeeded.
    """
    return _make_runner(
        {
            f"systemctl is-enabled {LIBRARY_INDEXER_TIMER_UNIT}": CommandResult(
                stdout="enabled", exit_code=0
            ),
            f"systemctl is-active {LIBRARY_INDEXER_TIMER_UNIT}": CommandResult(
                stdout="active", exit_code=0
            ),
            f"systemctl show -p ExecMainStatus {LIBRARY_INDEXER_SERVICE_UNIT}": CommandResult(
                stdout="ExecMainStatus=0", exit_code=0
            ),
        }
    )


def _not_installed_runner() -> CommandRunner:
    """Return a fake runner where the timer units are not installed."""
    return _make_runner(
        {
            f"systemctl is-enabled {LIBRARY_INDEXER_TIMER_UNIT}": CommandResult(
                stdout="not-found", exit_code=1
            ),
            f"systemctl is-active {LIBRARY_INDEXER_TIMER_UNIT}": CommandResult(
                stdout="inactive", exit_code=3
            ),
            f"systemctl show -p ExecMainStatus {LIBRARY_INDEXER_SERVICE_UNIT}": CommandResult(
                stdout="ExecMainStatus=-1", exit_code=0
            ),
        }
    )


# ---------------------------------------------------------------------------
# parse_timer_enabled
# ---------------------------------------------------------------------------

class TestParseTimerEnabled:
    """parse_timer_enabled maps systemctl is-enabled stdout to a boolean."""

    @pytest.mark.parametrize("stdout", sorted(TIMER_ENABLED_STATES))
    def test_enabled_states_return_true(self, stdout: str) -> None:
        assert parse_timer_enabled(stdout) is True

    @pytest.mark.parametrize("stdout", sorted(TIMER_DISABLED_STATES))
    def test_disabled_states_return_false(self, stdout: str) -> None:
        assert parse_timer_enabled(stdout) is False

    def test_not_found_returns_false(self) -> None:
        """'not-found' is the canonical systemctl output when the unit is absent."""
        assert parse_timer_enabled("not-found") is False

    def test_masked_returns_false(self) -> None:
        assert parse_timer_enabled("masked") is False

    def test_case_insensitive(self) -> None:
        """Systemd output is lowercase but defence against future drift."""
        assert parse_timer_enabled("Enabled") is True

    def test_whitespace_stripped(self) -> None:
        """Trailing newline from subprocess stdout does not cause false negative."""
        assert parse_timer_enabled("enabled\n") is True
        assert parse_timer_enabled("  enabled  ") is True

    def test_empty_string_returns_false(self) -> None:
        assert parse_timer_enabled("") is False


# ---------------------------------------------------------------------------
# parse_timer_active
# ---------------------------------------------------------------------------

class TestParseTimerActive:
    """parse_timer_active maps systemctl is-active (timer) stdout to a boolean."""

    def test_active_returns_true(self) -> None:
        assert parse_timer_active("active") is True

    @pytest.mark.parametrize(
        "stdout",
        ["inactive", "failed", "activating", "deactivating", "not-found", ""],
    )
    def test_non_active_states_return_false(self, stdout: str) -> None:
        assert parse_timer_active(stdout) is False

    def test_case_insensitive(self) -> None:
        assert parse_timer_active("Active") is True

    def test_whitespace_stripped(self) -> None:
        assert parse_timer_active("active\n") is True


# ---------------------------------------------------------------------------
# parse_service_active_from_exec_status
# ---------------------------------------------------------------------------

class TestParseServiceActiveFromExecStatus:
    """parse_service_active_from_exec_status interprets ExecMainStatus output.

    A Type=oneshot service reports 'inactive' from is-active between ticks;
    we derive service_active from ExecMainStatus=<N> instead.
    """

    def test_exit_zero_is_active(self) -> None:
        """ExecMainStatus=0 means the last run succeeded."""
        assert parse_service_active_from_exec_status("ExecMainStatus=0") is True

    def test_minus_one_is_not_active(self) -> None:
        """ExecMainStatus=-1 means the service has never run (fresh deploy)."""
        assert parse_service_active_from_exec_status("ExecMainStatus=-1") is False

    def test_nonzero_exit_is_not_active(self) -> None:
        """A non-zero exit code means the last run failed."""
        assert parse_service_active_from_exec_status("ExecMainStatus=1") is False
        assert parse_service_active_from_exec_status("ExecMainStatus=127") is False

    def test_empty_string_is_not_active(self) -> None:
        """Graceful handling of unexpected empty output."""
        assert parse_service_active_from_exec_status("") is False

    def test_malformed_output_is_not_active(self) -> None:
        """If systemctl output does not contain '=', treat as not-active."""
        assert parse_service_active_from_exec_status("ExecMainStatus") is False

    def test_whitespace_stripped(self) -> None:
        assert parse_service_active_from_exec_status("ExecMainStatus=0\n") is True


# ---------------------------------------------------------------------------
# parse_unit_file_present
# ---------------------------------------------------------------------------

class TestParseUnitFilePresent:
    """parse_unit_file_present checks whether the path points to a real file."""

    def test_existing_file_returns_true(self, tmp_path: Path) -> None:
        f = tmp_path / "resemblio-library-indexer.timer"
        f.write_text("[Unit]\n", encoding="utf-8")
        assert parse_unit_file_present(str(f)) is True

    def test_absent_path_returns_false(self, tmp_path: Path) -> None:
        assert parse_unit_file_present(str(tmp_path / "nonexistent.timer")) is False

    def test_none_returns_false(self) -> None:
        assert parse_unit_file_present(None) is False

    def test_directory_returns_false(self, tmp_path: Path) -> None:
        """A directory at the path is not the same as the unit file."""
        d = tmp_path / "resemblio-library-indexer.timer"
        d.mkdir()
        assert parse_unit_file_present(str(d)) is False


# ---------------------------------------------------------------------------
# gather_deploy_state (injected runner)
# ---------------------------------------------------------------------------

class TestGatherDeployState:
    """gather_deploy_state composes parsers with the runner to build DeployCheckState."""

    def test_healthy_state_all_true(self, tmp_path: Path) -> None:
        """When all probes return healthy values, all fields are True."""
        unit_file = tmp_path / LIBRARY_INDEXER_TIMER_UNIT
        unit_file.write_text("[Timer]\n", encoding="utf-8")
        runner = _healthy_runner(unit_file_path=str(unit_file))

        state = gather_deploy_state(
            runner,
            unit_file_path=str(unit_file),
            drl_templates_importable=True,
            library_pages_count=100,
        )

        assert state.unit_file_present is True
        assert state.timer_enabled is True
        assert state.timer_active is True
        assert state.service_active is True
        assert state.drl_templates_importable is True
        assert state.library_pages_count == 100

    def test_not_installed_state(self, tmp_path: Path) -> None:
        """When timer units are absent, relevant booleans are False."""
        unit_file_path = str(tmp_path / "absent.timer")
        runner = _not_installed_runner()

        state = gather_deploy_state(
            runner,
            unit_file_path=unit_file_path,
            drl_templates_importable=False,
        )

        assert state.unit_file_present is False
        assert state.timer_enabled is False
        assert state.timer_active is False
        # service_active is False because ExecMainStatus=-1 (never run)
        assert state.service_active is False
        assert state.drl_templates_importable is False

    def test_fresh_deploy_service_not_yet_run(self, tmp_path: Path) -> None:
        """Fresh deploy: timer enabled+active but oneshot has not fired yet.

        This is the expected state immediately after 'systemctl enable --now'
        before the first 60-second tick. service_active=False here is NOT a
        failure; evaluate_deploy_state does not gate on it.
        """
        unit_file = tmp_path / LIBRARY_INDEXER_TIMER_UNIT
        unit_file.write_text("[Timer]\n", encoding="utf-8")
        runner = _make_runner(
            {
                f"systemctl is-enabled {LIBRARY_INDEXER_TIMER_UNIT}": CommandResult(
                    stdout="enabled", exit_code=0
                ),
                f"systemctl is-active {LIBRARY_INDEXER_TIMER_UNIT}": CommandResult(
                    stdout="active", exit_code=0
                ),
                f"systemctl show -p ExecMainStatus {LIBRARY_INDEXER_SERVICE_UNIT}": CommandResult(
                    stdout="ExecMainStatus=-1", exit_code=0  # never run
                ),
            }
        )

        state = gather_deploy_state(
            runner,
            unit_file_path=str(unit_file),
            drl_templates_importable=True,
        )

        # service_active is informational only; the eval should still pass
        assert state.service_active is False
        assert state.timer_enabled is True
        assert state.timer_active is True

    def test_library_pages_count_passthrough(self, tmp_path: Path) -> None:
        """library_pages_count is forwarded unchanged to the state."""
        runner = _not_installed_runner()
        state = gather_deploy_state(
            runner,
            unit_file_path=str(tmp_path / "absent.timer"),
            drl_templates_importable=False,
            library_pages_count=6156,
        )
        assert state.library_pages_count == 6156


# ---------------------------------------------------------------------------
# probe_and_evaluate (end-to-end pipeline)
# ---------------------------------------------------------------------------

class TestProbeAndEvaluate:
    """probe_and_evaluate chains gather then evaluate into a single call."""

    def test_healthy_system_returns_ok(self, tmp_path: Path) -> None:
        unit_file = tmp_path / LIBRARY_INDEXER_TIMER_UNIT
        unit_file.write_text("[Timer]\n", encoding="utf-8")
        runner = _healthy_runner(unit_file_path=str(unit_file))

        result = probe_and_evaluate(
            runner,
            unit_file_path=str(unit_file),
            drl_templates_importable=True,
        )

        assert result.ok is True
        assert result.failing_checks == ()

    def test_not_installed_system_returns_failing(self, tmp_path: Path) -> None:
        runner = _not_installed_runner()

        result = probe_and_evaluate(
            runner,
            unit_file_path=str(tmp_path / "absent.timer"),
            drl_templates_importable=False,
        )

        assert result.ok is False
        assert len(result.failing_checks) >= 3  # unit_file, timer_enabled, timer_active, drl_importable

    def test_fresh_deploy_passes_eval_despite_no_service_run(
        self, tmp_path: Path
    ) -> None:
        """A system where the timer just started but service hasn't fired yet.

        evaluate_deploy_state does NOT gate on service_active, so this passes.
        The operator reads service_active from the DeployCheckResult separately.
        """
        unit_file = tmp_path / LIBRARY_INDEXER_TIMER_UNIT
        unit_file.write_text("[Timer]\n", encoding="utf-8")
        runner = _make_runner(
            {
                f"systemctl is-enabled {LIBRARY_INDEXER_TIMER_UNIT}": CommandResult(
                    stdout="enabled", exit_code=0
                ),
                f"systemctl is-active {LIBRARY_INDEXER_TIMER_UNIT}": CommandResult(
                    stdout="active", exit_code=0
                ),
                f"systemctl show -p ExecMainStatus {LIBRARY_INDEXER_SERVICE_UNIT}": CommandResult(
                    stdout="ExecMainStatus=-1", exit_code=0
                ),
            }
        )

        result = probe_and_evaluate(
            runner,
            unit_file_path=str(unit_file),
            drl_templates_importable=True,
        )

        # Passes: service_active is informational, not gated
        assert result.ok is True

    def test_result_has_schema_version(self, tmp_path: Path) -> None:
        runner = _not_installed_runner()
        result = probe_and_evaluate(
            runner,
            unit_file_path=str(tmp_path / "absent.timer"),
            drl_templates_importable=False,
        )
        assert result.schema_version is not None
        assert "v1" in result.schema_version

    def test_library_pages_count_in_result(self, tmp_path: Path) -> None:
        unit_file = tmp_path / LIBRARY_INDEXER_TIMER_UNIT
        unit_file.write_text("[Timer]\n", encoding="utf-8")
        runner = _healthy_runner(unit_file_path=str(unit_file))
        result = probe_and_evaluate(
            runner,
            unit_file_path=str(unit_file),
            drl_templates_importable=True,
            library_pages_count=6156,
        )
        assert result.library_pages_count == 6156

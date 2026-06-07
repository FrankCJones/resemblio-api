"""Tests for app.library_deploy_selfcheck - indexer unit-install guard (Phase 5).

TDD: tests written BEFORE the implementation. These pin:
  - The self-check module evaluates a state dict and returns a typed result.
  - Recognizes the deployed (systemd unit installed + enabled + active) state.
  - Recognizes each failure mode (missing unit file, not enabled, not active).
  - The systemd unit FILE (in deploy/systemd/) is present and contains
    the 60-second cadence required by the indexer.

These tests are pure-data and local: they do NOT SSH to prod. Phase 5's
gated prod ops (systemctl enable --now) are Frank/Jim YELLOW gates, not
part of this test harness.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.library_deploy_selfcheck import (
    LIBRARY_INDEXER_TIMER_UNIT,
    DeployCheckResult,
    DeployCheckState,
    evaluate_deploy_state,
)

# Path to the vendored systemd unit files (co-located in this repo).
SYSTEMD_UNIT_DIR = (
    Path(__file__).resolve().parent.parent / "deploy" / "systemd"
)
TIMER_UNIT_FILE = SYSTEMD_UNIT_DIR / LIBRARY_INDEXER_TIMER_UNIT
SERVICE_UNIT_FILE = SYSTEMD_UNIT_DIR / "resemblio-library-indexer.service"


# ---------------------------------------------------------------------------
# Unit file presence (local filesystem check)
# ---------------------------------------------------------------------------

class TestUnitFilesPresent:
    """Vendored systemd unit files must exist in deploy/systemd/."""

    def test_timer_unit_file_exists(self) -> None:
        assert TIMER_UNIT_FILE.exists(), (
            f"Missing timer unit file: {TIMER_UNIT_FILE}. "
            "This file must be committed alongside the indexer."
        )

    def test_service_unit_file_exists(self) -> None:
        assert SERVICE_UNIT_FILE.exists(), (
            f"Missing service unit file: {SERVICE_UNIT_FILE}."
        )

    def test_timer_unit_specifies_60s_cadence(self) -> None:
        text = TIMER_UNIT_FILE.read_text(encoding="utf-8")
        assert "OnUnitActiveSec=60s" in text, (
            "Library indexer timer must fire every 60 seconds."
        )

    def test_timer_unit_has_persistent_true(self) -> None:
        text = TIMER_UNIT_FILE.read_text(encoding="utf-8")
        assert "Persistent=true" in text, (
            "Timer must be Persistent=true to catch missed ticks after reboot."
        )


# ---------------------------------------------------------------------------
# evaluate_deploy_state (pure; no SSH)
# ---------------------------------------------------------------------------

class TestEvaluateDeployState:
    """evaluate_deploy_state correctly classifies each state."""

    def _state(
        self,
        *,
        unit_file_present: bool = True,
        timer_enabled: bool = True,
        timer_active: bool = True,
        service_active: bool = True,
        drl_templates_importable: bool = True,
        library_pages_count: int = 0,
    ) -> DeployCheckState:
        return DeployCheckState(
            unit_file_present=unit_file_present,
            timer_enabled=timer_enabled,
            timer_active=timer_active,
            service_active=service_active,
            drl_templates_importable=drl_templates_importable,
            library_pages_count=library_pages_count,
        )

    def test_healthy_state_passes(self) -> None:
        result = evaluate_deploy_state(self._state())
        assert result.ok

    def test_missing_unit_file_fails(self) -> None:
        result = evaluate_deploy_state(self._state(unit_file_present=False))
        assert not result.ok
        assert "unit_file" in result.failing_checks

    def test_timer_not_enabled_fails(self) -> None:
        result = evaluate_deploy_state(self._state(timer_enabled=False))
        assert not result.ok
        assert "timer_enabled" in result.failing_checks

    def test_timer_not_active_fails(self) -> None:
        result = evaluate_deploy_state(self._state(timer_active=False))
        assert not result.ok
        assert "timer_active" in result.failing_checks

    def test_drl_not_importable_fails(self) -> None:
        result = evaluate_deploy_state(self._state(drl_templates_importable=False))
        assert not result.ok
        assert "drl_importable" in result.failing_checks

    def test_result_has_schema_version(self) -> None:
        result = evaluate_deploy_state(self._state())
        assert result.schema_version is not None
        assert "v1" in result.schema_version

    def test_passing_checks_listed(self) -> None:
        result = evaluate_deploy_state(self._state())
        assert len(result.passing_checks) > 0

    def test_partial_failure_lists_both(self) -> None:
        result = evaluate_deploy_state(
            self._state(timer_enabled=False, timer_active=False)
        )
        assert not result.ok
        assert len(result.failing_checks) >= 2

    def test_library_pages_count_in_result(self) -> None:
        state = self._state(library_pages_count=42)
        result = evaluate_deploy_state(state)
        assert result.library_pages_count == 42

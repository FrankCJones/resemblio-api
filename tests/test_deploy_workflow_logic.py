"""Stage 3 (TDD): assert deploy.yml carries the silent-partial-deploy gate.

Background
==========
On 2026-06-02 the API ``deploy.yml`` workflow returned green twice while
the running systemd unit's MainPID did NOT change: code on disk advanced,
alembic ran, but ``sudo systemctl restart resemblio-api`` no-op'd (or its
exit code was swallowed by the SSH heredoc) and the service kept serving
the prior process image. Parent-session SSH had to finish the restart by
hand both times. CTO Stage 3 (`cto-reviews/2026-06-03-resemblio-back-on-
track-tdd-plan.md` Stage 3) closes the bug by making the deploy gate the
PID-change AND the readyz verb-probe AND auto-fail if either misses.

What this test asserts
======================
This is a workflow-logic test, not a live SSH test. We read the deploy.yml
text and assert four required strings are present:

1. ``BEFORE_PID=`` capture of the pre-restart MainPID via
   ``systemctl show -p MainPID``.
2. ``AFTER_PID=`` capture of the post-restart MainPID.
3. A ``BEFORE_PID`` vs ``AFTER_PID`` equality check that exits non-zero
   when they match (the silent-partial-deploy signature).
4. A post-deploy probe of ``/v1/readyz`` (the verb-probe; readyz exercises
   DB + storage, so a 200 proves the NEW code is actually running, not
   just that the prior process is still serving healthz).

Why string-presence rather than executing the SSH block
-------------------------------------------------------
The deploy.yml SSH block runs against the live prod box; we cannot
exercise that from unit tests. A string-presence assertion is the
cheapest gate that catches the regression of "someone deleted the PID
check during a refactor" - the failure mode CTO explicitly named in the
stage spec (point 4: "Add a CI lint that fails if deploy.yml does not
contain both the PID-change check and the smoke step").

Pair this test with the live deploy: when the next deploy lands, the
parent session verifies the PID-change line is actually printed in the
GH Actions log, and the readyz check actually fires. That verification
is owned by the parent session, not this test.
"""
from __future__ import annotations

from pathlib import Path

import pytest

# Resolve the deploy.yml relative to this file so the test works from any cwd
# (pytest invocation from repo root, IDE invocation from tests/, CI etc.).
DEPLOY_YML = (
    Path(__file__).resolve().parent.parent
    / ".github"
    / "workflows"
    / "deploy.yml"
)


# Required string markers. Centralized so the lint script (ci/lint_deploy_yml.sh)
# and this test stay in lock-step; if a future edit drops a marker, both fail.
# The four markers correspond 1:1 to the four CTO Stage 3 requirements.
REQUIRED_MARKERS: tuple[str, ...] = (
    "BEFORE_PID=",       # 1. pre-restart PID capture
    "AFTER_PID=",        # 2. post-restart PID capture
    "PID did not change",  # 3. equality-failure diagnostic + non-zero exit
    "/v1/readyz",        # 4. verb-probe smoke (exercises new code path)
    # Stage 12 marker (CTO Stage 12 - entrypoint smoke gate). Catches
    # module-load races in `app.cli.*` BEFORE the deploy SSH block runs.
    # Paired with `tests/test_entrypoint_smoke.py` and `ci/entrypoints.sh`.
    "bash ci/entrypoints.sh",  # 5. entrypoint smoke step invocation
    # CTO 2026-06-04 markers (silent-partial-deploy git-SHA gate). Closes
    # Incident 2 (2026-06-02 `b3f7ca2`): SSH step returned success while
    # prod git HEAD never advanced past the prior commit. Design doc:
    # `cto-reviews/2026-06-04-cicd-partial-deploy-investigation.md`.
    "Git-SHA parity",       # 6. in-heredoc parity assertion + diagnostic
    "SendEnv=GITHUB_SHA",   # 7. forwards GITHUB_SHA to the remote shell
)


@pytest.fixture(scope="module")
def deploy_yml_text() -> str:
    """Read deploy.yml once per test module; fail loud if it is missing."""
    if not DEPLOY_YML.is_file():
        pytest.fail(f"deploy.yml not found at expected path: {DEPLOY_YML}")
    return DEPLOY_YML.read_text(encoding="utf-8")


@pytest.mark.parametrize("marker", REQUIRED_MARKERS)
def test_deploy_yml_contains_required_marker(deploy_yml_text: str, marker: str) -> None:
    """Every Stage 3 marker MUST be present in deploy.yml.

    Failure here means a deploy.yml edit removed the silent-partial-deploy
    gate. The fix is to put the gate back, not to relax the test.
    """
    assert marker in deploy_yml_text, (
        f"deploy.yml is missing required Stage 3 marker {marker!r}. "
        f"This marker is part of the silent-partial-deploy gate. "
        f"See cto-reviews/2026-06-03-resemblio-back-on-track-tdd-plan.md Stage 3."
    )


def test_pid_change_check_precedes_readyz_probe(deploy_yml_text: str) -> None:
    """The PID-change assertion must run BEFORE the readyz probe.

    Rationale: if the unit did not restart, readyz could still return 200
    by serving from the prior process image. The gate order is "did the
    process actually swap?" first, "is the swapped process healthy?"
    second. Reversing the order weakens the gate.
    """
    pid_idx = deploy_yml_text.find("PID did not change")
    readyz_idx = deploy_yml_text.find("/v1/readyz")
    assert pid_idx != -1, "PID-change diagnostic not found"
    assert readyz_idx != -1, "/v1/readyz probe not found"
    assert pid_idx < readyz_idx, (
        "PID-change assertion must appear before the /v1/readyz probe in "
        "deploy.yml so a no-op restart fails fast before the verb-probe runs."
    )


def test_deploy_yml_exits_nonzero_on_pid_mismatch(deploy_yml_text: str) -> None:
    """The PID-mismatch branch must call ``exit 1`` (non-zero) explicitly.

    Without this, the workflow could log the diagnostic and continue, which
    is exactly the silent-partial-deploy failure mode we are closing.
    """
    # Locate the PID-mismatch diagnostic and confirm an `exit 1` (or higher)
    # follows it within a small window. 600 chars is generous enough for the
    # human-readable diagnostic block, tight enough that an unrelated `exit 1`
    # later in the file cannot satisfy the assertion accidentally.
    idx = deploy_yml_text.find("PID did not change")
    assert idx != -1, "PID-change diagnostic marker missing"
    window = deploy_yml_text[idx : idx + 600]
    assert "exit 1" in window, (
        "Expected `exit 1` within 600 chars after the PID-change diagnostic; "
        "the workflow must fail red when restart did not advance MainPID."
    )


def test_git_sha_parity_check_precedes_pip_install(deploy_yml_text: str) -> None:
    """The git-SHA parity check must run BEFORE pip install.

    Rationale: if `git reset --hard origin/main` did not actually advance prod
    to the workflow's trigger SHA (the Incident 2 failure shape), running
    `pip install -e .` against a stale tree would install the prior version's
    code into the venv and the partial deploy could still progress. Gate
    order is "did the code actually land at the expected SHA?" first,
    everything else after. Mirror of `test_pid_change_check_precedes_readyz_probe`.
    """
    parity_idx = deploy_yml_text.find("Git-SHA parity")
    pip_idx = deploy_yml_text.find("/opt/resemblio-api/venv/bin/pip install")
    assert parity_idx != -1, "Git-SHA parity marker not found"
    assert pip_idx != -1, "pip install line not found"
    assert parity_idx < pip_idx, (
        "Git-SHA parity assertion must appear before the pip install step so "
        "a stale tree fails fast before deps are installed against it."
    )


def test_git_sha_parity_exits_nonzero_on_mismatch(deploy_yml_text: str) -> None:
    """The git-SHA mismatch branch must call ``exit 1`` (non-zero) explicitly.

    Without an `exit 1`, the workflow could log the FATAL diagnostic and
    continue, which is the silent-partial-deploy failure shape (Incident 2
    on 2026-06-02). Same gate pattern as the PID-mismatch assertion.
    """
    idx = deploy_yml_text.find("Git-SHA parity")
    assert idx != -1, "Git-SHA parity marker missing"
    # 1500-char window catches the diagnostic comment block + assertion + exit
    # while still bounding the search so an unrelated `exit 1` later in the
    # file cannot satisfy the assertion.
    window = deploy_yml_text[idx : idx + 1500]
    assert "exit 1" in window, (
        "Expected `exit 1` within 1500 chars after the Git-SHA parity marker; "
        "the workflow must fail red when prod git HEAD does not match the "
        "workflow trigger SHA."
    )


def test_git_sha_passed_via_env_and_sendenv(deploy_yml_text: str) -> None:
    """GITHUB_SHA must be wired through the env block AND SendEnv AND inline.

    Three-point match per CTO 2026-06-04 design doc test plan: a typo in any
    of the three would silently break the assertion by leaving EXPECTED_SHA
    empty. Asserting all three forces edits to keep them in lock-step.

    1. Step `env:` block declares `GITHUB_SHA: ${{ github.sha }}` (GitHub
       Actions expression that resolves the trigger SHA).
    2. `SendEnv=GITHUB_SHA` on the ssh invocation (sshd-side acceptance).
    3. Inline `GITHUB_SHA="$GITHUB_SHA"` prefix on `bash -s` (defense in
       depth that does not depend on the remote sshd accepting SendEnv;
       mirrors the same wiring already used for LOCAL_ALEMBIC_HEAD).
    """
    assert "GITHUB_SHA: ${{ github.sha }}" in deploy_yml_text, (
        "deploy.yml step env block must declare `GITHUB_SHA: ${{ github.sha }}` "
        "so the GitHub Actions trigger SHA is available to forward to the "
        "remote shell."
    )
    assert "SendEnv=GITHUB_SHA" in deploy_yml_text, (
        "ssh invocation must include `-o SendEnv=GITHUB_SHA` so sshd on prod "
        "accepts the GITHUB_SHA variable."
    )
    assert 'GITHUB_SHA="$GITHUB_SHA"' in deploy_yml_text, (
        "ssh command must inline `GITHUB_SHA=\"$GITHUB_SHA\"` as a prefix to "
        "`bash -s` so the value reaches the remote shell even if sshd is not "
        "configured to AcceptEnv GITHUB_SHA. Matches the LOCAL_ALEMBIC_HEAD "
        "pattern already used in the same line."
    )

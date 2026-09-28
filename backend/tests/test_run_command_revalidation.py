"""Dispatch-time run_command path revalidation for the Docker backend (#68).

Scenario: an allowlisted argv references a symlink that points at an allowed
target when policy decides ALLOW, but is swapped to a blocked target before
the Docker executor stages the workspace. The executor must revalidate the
path-bearing argv (and cwd) against the run scope in the staged workspace
immediately before dispatch and refuse without invoking the container.

All fixtures are synthetic. Docker-daemon tests use a mocked subprocess, so
they run with no daemon present.
"""

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import uuid

import pytest

from scopewatch.executor_docker import DockerExecutor
from scopewatch.models import PolicyOutcome, ReasonCode
from scopewatch.policy import evaluate_policy, revalidate_run_command_in_workspace
from scopewatch.schemas import ActionRequest, Run, TaskScope


NOW = datetime.now(timezone.utc).isoformat()
BLOCKED_SENTINEL = "SYNTHETIC-BLOCKED-CONTENT-68"


@pytest.fixture
def swap_scope() -> TaskScope:
    return TaskScope(
        schema_version="1",
        task_description="Allowlisted cat over tests, secrets blocked.",
        allowed_paths=["tests", "link"],
        blocked_paths=["secrets"],
        allowed_tools=["workspace"],
        allowed_operations=["run_command"],
        allowed_network_destinations=[],
        requires_approval=[],
        allowed_commands=[["cat"]],
        created_at=NOW,
    )


@pytest.fixture
def swap_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    (ws / "tests").mkdir(parents=True)
    (ws / "tests" / "allowed.txt").write_text("synthetic allowed", encoding="utf-8")
    (ws / "secrets").mkdir(parents=True)
    (ws / "secrets" / "notes.txt").write_text(BLOCKED_SENTINEL, encoding="utf-8")
    # Decision-time target: the allowed file.
    (ws / "link").symlink_to("tests/allowed.txt")
    return ws


@pytest.fixture
def swap_run(swap_scope: TaskScope) -> Run:
    return Run(
        id=str(uuid.uuid4()),
        name="Swap run",
        task_scope=swap_scope,
        created_at=NOW,
        updated_at=NOW,
    )


def _cat_action(run: Run, argv: list[str] | None = None) -> ActionRequest:
    return ActionRequest(
        id=str(uuid.uuid4()),
        run_id=run.id,
        tool="workspace",
        operation="run_command",
        resource=".",
        arguments={"argv": argv if argv is not None else ["cat", "link"]},
        requested_at=datetime.now(timezone.utc).isoformat(),
    )


def _swap_to_blocked(ws: Path) -> None:
    """Point the argv symlink at the blocked target (post-decision swap)."""
    (ws / "link").unlink()
    (ws / "link").symlink_to("secrets/notes.txt")


def _mock_docker(monkeypatch: pytest.MonkeyPatch, calls: list, payload: dict | None) -> None:
    """Pretend a daemon exists; record subprocess calls, never run anything."""
    import scopewatch.executor_docker as docker_mod

    monkeypatch.setattr(docker_mod, "is_docker_available", lambda *a, **k: True)

    def _fake(cmd: object, **kwargs: object) -> subprocess.CompletedProcess:
        assert isinstance(cmd, list)
        calls.append(list(cmd))
        if len(cmd) > 1 and cmd[1] == "run":
            assert payload is not None, "docker run invoked without a canned payload"
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload).encode(), b"")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(docker_mod.subprocess, "run", _fake)


def _executed_payload(argv: list[str]) -> dict:
    return {
        "status": "EXECUTED",
        "result": {
            "operation": "run_command",
            "argv": argv,
            "exit_code": 0,
            "stdout": "synthetic allowed\n",
            "stderr": "",
            "truncated_stdout": False,
            "truncated_stderr": False,
            "timed_out": False,
        },
        "error_code": None,
    }


# ---------------------------------------------------------------------
# Policy helper: resolved-target revalidation in a staged workspace.
# ---------------------------------------------------------------------


def test_helper_passes_before_swap(
    swap_workspace: Path, swap_run: Run, docker_backend: None
) -> None:
    action = _cat_action(swap_run)
    decision = evaluate_policy(action, swap_run, swap_workspace)
    assert decision.outcome == PolicyOutcome.ALLOW
    assert (
        revalidate_run_command_in_workspace(action, swap_run.task_scope, swap_workspace)
        is None
    )


def test_helper_detects_swap_to_blocked_target(
    swap_workspace: Path, swap_run: Run, docker_backend: None
) -> None:
    action = _cat_action(swap_run)
    assert evaluate_policy(action, swap_run, swap_workspace).outcome == PolicyOutcome.ALLOW
    _swap_to_blocked(swap_workspace)
    denial = revalidate_run_command_in_workspace(
        action, swap_run.task_scope, swap_workspace
    )
    assert denial is not None
    reason_code, _explanation, _rule = denial
    assert reason_code == ReasonCode.BLOCKED_PATH


def test_helper_rejects_mutated_unallowlisted_prefix(
    swap_workspace: Path, swap_run: Run, docker_backend: None
) -> None:
    action = _cat_action(swap_run)
    assert evaluate_policy(action, swap_run, swap_workspace).outcome == PolicyOutcome.ALLOW
    action.arguments = {"argv": ["rm", "link"]}
    denial = revalidate_run_command_in_workspace(
        action, swap_run.task_scope, swap_workspace
    )
    assert denial is not None
    assert denial[0] == ReasonCode.COMMAND_NOT_ALLOWED


def test_helper_rejects_blocked_cwd(
    swap_workspace: Path, swap_run: Run, docker_backend: None
) -> None:
    action = _cat_action(swap_run)
    action.resource = "secrets"
    denial = revalidate_run_command_in_workspace(
        action, swap_run.task_scope, swap_workspace
    )
    assert denial is not None
    assert denial[0] == ReasonCode.BLOCKED_PATH


# ---------------------------------------------------------------------
# Docker dispatch: refusal happens before the container is invoked.
# ---------------------------------------------------------------------


def test_docker_dispatch_refuses_swapped_symlink_before_invoke(
    swap_workspace: Path, swap_run: Run, docker_backend: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ALLOW at decision time, swapped before staging -> FAILED, no docker run."""
    action = _cat_action(swap_run)
    decision = evaluate_policy(action, swap_run, swap_workspace)
    assert decision.outcome == PolicyOutcome.ALLOW
    _swap_to_blocked(swap_workspace)

    calls: list = []
    import scopewatch.executor_docker as docker_mod

    monkeypatch.setattr(docker_mod, "is_docker_available", lambda *a, **k: True)

    def _refuse_run(cmd: object, **kwargs: object) -> subprocess.CompletedProcess:
        assert isinstance(cmd, list)
        calls.append(list(cmd))
        if len(cmd) > 1 and cmd[1] == "run":
            raise AssertionError("docker run must not be invoked for blocked dispatch")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(docker_mod.subprocess, "run", _refuse_run)

    from scopewatch.models import ExecutionStatus

    receipt = DockerExecutor().execute(
        action,
        swap_workspace,
        policy_decision=decision,
        task_scope=swap_run.task_scope,
    )
    assert receipt.status == ExecutionStatus.FAILED
    assert receipt.error_code == ReasonCode.BLOCKED_PATH.value
    assert not any(len(c) > 1 and c[1] == "run" for c in calls)
    # Blocked bytes never reach the receipt.
    assert BLOCKED_SENTINEL not in str(receipt.sanitized_result)


def test_docker_dispatch_proceeds_when_symlink_unchanged(
    swap_workspace: Path, swap_run: Run, docker_backend: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: no swap -> dispatch proceeds (mocked container success)."""
    from scopewatch.models import ExecutionStatus

    action = _cat_action(swap_run)
    decision = evaluate_policy(action, swap_run, swap_workspace)
    assert decision.outcome == PolicyOutcome.ALLOW

    calls: list = []
    _mock_docker(monkeypatch, calls, payload=_executed_payload(["cat", "link"]))
    receipt = DockerExecutor().execute(
        action,
        swap_workspace,
        policy_decision=decision,
        task_scope=swap_run.task_scope,
    )
    assert receipt.status == ExecutionStatus.EXECUTED
    assert any(len(c) > 1 and c[1] == "run" for c in calls)

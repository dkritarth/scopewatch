"""Executor tests for the allowlisted run_command operation (issue #36).

Seams under test: the in-container helper's run_command branch (executed
directly with the REAL helper source and a synthetic workspace, no Docker
daemon required), the local-executor refusal, and Docker fail-closed
behaviour. Full container round-trips are marked requires_docker and skip
cleanly when Docker is absent.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
import uuid

import pytest

from scopewatch.executor import ExecutionSecurityError, execute_action
from scopewatch.executor_docker import (
    RUN_COMMAND_DEFAULT_TIMEOUT_S,
    RUN_COMMAND_MAX_TIMEOUT_S,
    RUN_COMMAND_OUTPUT_LIMIT,
    _helper_code,
    clamp_run_command_timeout,
    extract_run_command_argv,
    is_docker_available,
)
from scopewatch.models import ExecutionStatus, PolicyOutcome, ReasonCode
from scopewatch.schemas import ActionRequest, PolicyDecision


def _allow_decision(action_id: str = "action-1") -> PolicyDecision:
    return PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id=action_id,
        outcome=PolicyOutcome.ALLOW,
        reason_code=ReasonCode.ALLOWED_TOOL_AND_RESOURCE,
        explanation="Permitted",
        matched_rule="RULE_ALLOWED",
        decided_at=datetime.now(timezone.utc).isoformat(),
        deterministic=True,
    )


def _run_command_action(
    action_id: str = "action-cmd",
    resource: str = ".",
    arguments: dict | None = None,
) -> ActionRequest:
    if arguments is None:
        arguments = {"command": "pytest tests/"}
    return ActionRequest(
        id=action_id,
        run_id="run-1",
        tool="workspace",
        operation="run_command",
        resource=resource,
        arguments=arguments,
        requested_at=datetime.now(timezone.utc).isoformat(),
    )


@pytest.fixture
def cmd_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "tests").mkdir()
    (ws / "tests" / "test_auth.py").write_text("synthetic", encoding="utf-8")
    return ws


def _run_helper_local(
    operation: str,
    resource: str,
    args_dict: dict,
    workspace: Path,
    timeout: float = 120.0,
) -> dict:
    """Execute the REAL container helper source locally (no Docker daemon).

    The helper resolves its workspace from SCOPEWATCH_WORKSPACE (defaulting
    to /workspace inside containers), so this exercises the exact timeout,
    truncation, and exit-code logic that runs in production.
    """
    env = dict(os.environ, SCOPEWATCH_WORKSPACE=str(workspace))
    proc = subprocess.run(
        [sys.executable, "-c", _helper_code(), operation, resource, json.dumps(args_dict)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=env,
        cwd=str(workspace),
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    return json.loads(proc.stdout.decode("utf-8", errors="replace"))


def _argv(*parts: str) -> dict:
    return {"argv": [sys.executable, "-c", *parts]}


# ---------------- Helper behaviour: no daemon required ----------------


def test_run_command_success_records_exit_code(cmd_workspace: Path) -> None:
    payload = _run_helper_local(
        "run_command", ".", _argv("print('synthetic-hello')"), cmd_workspace
    )
    assert payload["status"] == "EXECUTED"
    assert payload["error_code"] is None
    result = payload["result"]
    assert result["operation"] == "run_command"
    assert result["exit_code"] == 0
    assert result["stdout"] == "synthetic-hello\n"
    assert result["timed_out"] is False
    assert result["truncated_stdout"] is False


def test_run_command_nonzero_exit_is_failure_not_denial(cmd_workspace: Path) -> None:
    payload = _run_helper_local(
        "run_command", ".", _argv("import sys; sys.exit(3)"), cmd_workspace
    )
    assert payload["status"] == "FAILED"
    assert payload["error_code"] == "NONZERO_EXIT"
    assert payload["result"]["exit_code"] == 3
    # Executed-with-failure: a policy denial would be NOT_EXECUTED with a
    # policy ReasonCode, never FAILED/NONZERO_EXIT.
    assert payload["error_code"] not in {c.value for c in ReasonCode}


def test_run_command_timeout_kills_and_fails(cmd_workspace: Path) -> None:
    payload = _run_helper_local(
        "run_command",
        ".",
        {**_argv("import time; time.sleep(30)"), "timeout_s": 1},
        cmd_workspace,
    )
    assert payload["status"] == "FAILED"
    assert payload["error_code"] == "COMMAND_TIMEOUT"
    assert payload["result"]["timed_out"] is True


def test_run_command_stdout_truncated_with_marker(cmd_workspace: Path) -> None:
    payload = _run_helper_local(
        "run_command",
        ".",
        _argv("import sys; sys.stdout.write('A' * 200000)"),
        cmd_workspace,
    )
    assert payload["status"] == "EXECUTED"
    result = payload["result"]
    assert result["truncated_stdout"] is True
    assert "...[truncated" in result["stdout"]
    assert len(result["stdout"].encode("utf-8")) <= RUN_COMMAND_OUTPUT_LIMIT + 256


def test_run_command_no_shell_interpretation(cmd_workspace: Path) -> None:
    payload = _run_helper_local(
        "run_command",
        ".",
        {"argv": [sys.executable, "-c", "import sys; print(sys.argv[1])", "a;b"]},
        cmd_workspace,
    )
    assert payload["status"] == "EXECUTED"
    assert payload["result"]["stdout"] == "a;b\n"


def test_run_command_absolute_cwd_rejected(cmd_workspace: Path) -> None:
    payload = _run_helper_local(
        "run_command", "/tmp", _argv("print('x')"), cmd_workspace
    )
    assert payload["status"] == "FAILED"


def test_run_command_traversal_cwd_rejected(cmd_workspace: Path) -> None:
    payload = _run_helper_local(
        "run_command", "../..", _argv("print('x')"), cmd_workspace
    )
    assert payload["status"] == "FAILED"


def test_helper_source_has_no_shell_guards() -> None:
    code = _helper_code()
    assert "shell=False" in code
    assert "TimeoutExpired" in code
    assert "run_command" in code
    assert "...[truncated" in code
    assert "/workspace" in code


# ---------------- Host-side argument handling: no daemon required ----------------


def test_extract_argv_from_string() -> None:
    action = _run_command_action(arguments={"command": "python -m pytest tests/"})
    assert extract_run_command_argv(action) == ["python", "-m", "pytest", "tests/"]


def test_extract_argv_from_list() -> None:
    action = _run_command_action(arguments={"argv": ["pytest", "tests/"]})
    assert extract_run_command_argv(action) == ["pytest", "tests/"]


def test_extract_argv_malformed_returns_none() -> None:
    assert extract_run_command_argv(_run_command_action(arguments={})) is None
    assert (
        extract_run_command_argv(_run_command_action(arguments={"command": ""}))
        is None
    )
    assert (
        extract_run_command_argv(_run_command_action(arguments={"argv": []})) is None
    )


def test_timeout_clamp() -> None:
    assert clamp_run_command_timeout(10) == 10.0
    assert clamp_run_command_timeout("nope") == RUN_COMMAND_DEFAULT_TIMEOUT_S
    assert clamp_run_command_timeout(-5) == 1.0
    assert clamp_run_command_timeout(100000) == RUN_COMMAND_MAX_TIMEOUT_S


def test_local_executor_rejects_run_command(
    cmd_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "local")
    action = _run_command_action()
    with pytest.raises(ExecutionSecurityError, match="requires the Docker executor"):
        execute_action(action, cmd_workspace, policy_decision=_allow_decision())


def test_docker_unavailable_run_command_fails_closed(
    cmd_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    import scopewatch.executor_docker as docker_mod

    monkeypatch.setattr(docker_mod, "is_docker_available", lambda *a, **k: False)
    action = _run_command_action(arguments={"argv": ["pytest", "tests/"]})
    receipt = execute_action(
        action, cmd_workspace, policy_decision=_allow_decision(action.id)
    )
    assert receipt.status == ExecutionStatus.FAILED
    assert receipt.error_code == "EXECUTION_FAILED"
    assert receipt.executor == "docker-executor"


# ---------------- Container round-trips: skipped when Docker absent ----------------


@pytest.fixture
def requires_docker() -> None:
    if not is_docker_available():
        pytest.skip("Docker daemon unavailable; skipping Docker integration test.")


def _docker_action(argv: list[str], timeout_s: float = 60) -> ActionRequest:
    return _run_command_action(
        arguments={"argv": argv, "timeout_s": timeout_s},
    )


def test_docker_run_command_end_to_end(
    requires_docker: None, cmd_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    action = _docker_action(["python3", "-c", "print('synthetic-hello')"])
    receipt = execute_action(
        action, cmd_workspace, policy_decision=_allow_decision(action.id)
    )
    assert receipt.status == ExecutionStatus.EXECUTED
    assert receipt.sanitized_result is not None
    assert receipt.sanitized_result["exit_code"] == 0
    assert "synthetic-hello" in receipt.sanitized_result["stdout"]


def test_docker_run_command_timeout(
    requires_docker: None, cmd_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    action = _docker_action(
        ["python3", "-c", "import time; time.sleep(30)"], timeout_s=2
    )
    receipt = execute_action(
        action, cmd_workspace, policy_decision=_allow_decision(action.id)
    )
    assert receipt.status == ExecutionStatus.FAILED
    assert receipt.error_code == "COMMAND_TIMEOUT"


def test_docker_run_command_truncation(
    requires_docker: None, cmd_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    action = _docker_action(
        ["python3", "-c", "import sys; sys.stdout.write('B' * 200000)"]
    )
    receipt = execute_action(
        action, cmd_workspace, policy_decision=_allow_decision(action.id)
    )
    assert receipt.status == ExecutionStatus.EXECUTED
    assert receipt.sanitized_result is not None
    assert receipt.sanitized_result["truncated_stdout"] is True
    assert "...[truncated" in receipt.sanitized_result["stdout"]


def test_docker_run_command_nonzero_exit(
    requires_docker: None, cmd_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    action = _docker_action(["python3", "-c", "import sys; sys.exit(3)"])
    receipt = execute_action(
        action, cmd_workspace, policy_decision=_allow_decision(action.id)
    )
    assert receipt.status == ExecutionStatus.FAILED
    assert receipt.error_code == "NONZERO_EXIT"
    assert receipt.sanitized_result is not None
    assert receipt.sanitized_result["exit_code"] == 3

"""Policy tests for the allowlisted run_command operation (issue #36).

Seams under test: `evaluate_policy` for run_command decisions and the
`TaskScope.allowed_commands` / `commands_requiring_approval` contract.
Execution semantics live in test_executor_run_command.py.
"""

from datetime import datetime, timezone
from pathlib import Path
import uuid

import pytest

from scopewatch.models import PolicyOutcome, ReasonCode, RunStatus
from scopewatch.policy import evaluate_policy
from scopewatch.schemas import ActionRequest, Run, TaskScope


@pytest.fixture
def docker_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")


@pytest.fixture
def cmd_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "tests").mkdir()
    (ws / "tests" / "test_auth.py").write_text("synthetic test", encoding="utf-8")
    (ws / "secrets").mkdir()
    (ws / "secrets" / "notes.txt").write_text("synthetic secret", encoding="utf-8")
    (ws / "outputs").mkdir()
    return ws


@pytest.fixture
def cmd_run() -> Run:
    scope = TaskScope(
        schema_version="1",
        task_description="Run allowlisted synthetic test commands.",
        allowed_paths=["tests", "outputs"],
        blocked_paths=["secrets"],
        allowed_tools=["workspace"],
        allowed_operations=["list_directory", "read_text", "run_command"],
        allowed_network_destinations=[],
        requires_approval=[],
        allowed_commands=[["python", "-m", "pytest"], ["pytest"], ["ls"]],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    return Run(
        id=str(uuid.uuid4()),
        name="Command Run",
        task_scope=scope,
        status=RunStatus.ACTIVE,
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
        synthetic=True,
        interception_coverage="Synthetic test gateway",
        reasoning_availability="Unavailable",
    )


def _run_command_action(
    run: Run,
    resource: str = ".",
    command: str | None = None,
    argv: list[str] | None = None,
    extra_args: dict | None = None,
) -> ActionRequest:
    arguments: dict = dict(extra_args or {})
    if command is not None:
        arguments["command"] = command
    if argv is not None:
        arguments["argv"] = argv
    return ActionRequest(
        id=str(uuid.uuid4()),
        run_id=run.id,
        tool="workspace",
        operation="run_command",
        resource=resource,
        arguments=arguments,
        requested_at=datetime.now(timezone.utc).isoformat(),
    )


def test_run_command_allowed_pytest(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None
) -> None:
    action = _run_command_action(cmd_run, command="python -m pytest tests/")
    decision = evaluate_policy(action, cmd_run, cmd_workspace)
    assert decision.outcome == PolicyOutcome.ALLOW
    assert decision.reason_code == ReasonCode.ALLOWED_TOOL_AND_RESOURCE
    assert decision.deterministic is True


def test_run_command_allowed_argv_list_form(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None
) -> None:
    action = _run_command_action(cmd_run, argv=["pytest", "tests/"])
    decision = evaluate_policy(action, cmd_run, cmd_workspace)
    assert decision.outcome == PolicyOutcome.ALLOW


@pytest.mark.parametrize(
    "metachar",
    [";", "&", "|", ">", "<", "`", "$(", "\n", "\x00"],
)
def test_run_command_each_metacharacter_denied(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None, metachar: str
) -> None:
    action = _run_command_action(cmd_run, command=f"pytest tests/ {metachar} ls")
    decision = evaluate_policy(action, cmd_run, cmd_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.SHELL_METACHARACTER


def test_run_command_prefix_mismatch_denied(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None
) -> None:
    action = _run_command_action(cmd_run, command='python -c "print(1)"')
    decision = evaluate_policy(action, cmd_run, cmd_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.COMMAND_NOT_ALLOWED


def test_run_command_traversal_arg_denied(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None
) -> None:
    action = _run_command_action(cmd_run, command="pytest ../../etc")
    decision = evaluate_policy(action, cmd_run, cmd_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.PATH_TRAVERSAL


def test_run_command_absolute_arg_denied(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None
) -> None:
    action = _run_command_action(cmd_run, command="ls /home")
    decision = evaluate_policy(action, cmd_run, cmd_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.PATH_OUTSIDE_WORKSPACE


def test_run_command_blocked_arg_denied(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None
) -> None:
    action = _run_command_action(cmd_run, command="ls secrets/notes.txt")
    decision = evaluate_policy(action, cmd_run, cmd_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.BLOCKED_PATH


@pytest.mark.parametrize("raw", ["", "   ", "\t  "])
def test_run_command_empty_malformed(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None, raw: str
) -> None:
    action = _run_command_action(cmd_run, command=raw)
    decision = evaluate_policy(action, cmd_run, cmd_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.MALFORMED_REQUEST


def test_run_command_missing_arguments_malformed(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None
) -> None:
    action = _run_command_action(cmd_run)
    decision = evaluate_policy(action, cmd_run, cmd_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.MALFORMED_REQUEST


def test_run_command_unbalanced_quote_malformed(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None
) -> None:
    action = _run_command_action(cmd_run, command='pytest "tests/')
    decision = evaluate_policy(action, cmd_run, cmd_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.MALFORMED_REQUEST


def test_run_command_local_executor_unsupported(
    cmd_workspace: Path, cmd_run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "local")
    action = _run_command_action(cmd_run, command="pytest tests/")
    decision = evaluate_policy(action, cmd_run, cmd_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.UNSUPPORTED_OPERATION


def test_run_command_operation_not_in_scope(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None
) -> None:
    cmd_run.task_scope.allowed_operations = ["list_directory", "read_text"]
    action = _run_command_action(cmd_run, command="pytest tests/")
    decision = evaluate_policy(action, cmd_run, cmd_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.OPERATION_NOT_ALLOWED


def test_run_command_requires_approval_holds(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None
) -> None:
    cmd_run.task_scope.requires_approval = ["run_command"]
    action = _run_command_action(cmd_run, command="pytest tests/")
    decision = evaluate_policy(action, cmd_run, cmd_workspace)
    assert decision.outcome == PolicyOutcome.HOLD
    assert decision.reason_code == ReasonCode.APPROVAL_REQUIRED


def test_run_command_per_command_approval_holds_only_match(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None
) -> None:
    cmd_run.task_scope.commands_requiring_approval = [["python", "-m", "pytest"]]
    held = evaluate_policy(
        _run_command_action(cmd_run, command="python -m pytest tests/"),
        cmd_run,
        cmd_workspace,
    )
    assert held.outcome == PolicyOutcome.HOLD
    assert held.reason_code == ReasonCode.APPROVAL_REQUIRED
    allowed = evaluate_policy(
        _run_command_action(cmd_run, command="ls ."),
        cmd_run,
        cmd_workspace,
    )
    assert allowed.outcome == PolicyOutcome.ALLOW


def test_run_command_blocked_cwd_denied(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None
) -> None:
    action = _run_command_action(cmd_run, resource="secrets", command="ls .")
    decision = evaluate_policy(action, cmd_run, cmd_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.BLOCKED_PATH


def test_run_command_absolute_cwd_denied(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None
) -> None:
    action = _run_command_action(cmd_run, resource="/tmp", command="ls .")
    decision = evaluate_policy(action, cmd_run, cmd_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.PATH_OUTSIDE_WORKSPACE


def test_run_command_no_match_when_allowlist_empty(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None
) -> None:
    cmd_run.task_scope.allowed_commands = []
    action = _run_command_action(cmd_run, command="ls .")
    decision = evaluate_policy(action, cmd_run, cmd_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.COMMAND_NOT_ALLOWED

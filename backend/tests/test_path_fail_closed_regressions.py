"""Regression coverage for oversized paths and symlinked blocked files."""

from datetime import datetime, timezone
from pathlib import Path
import uuid

import pytest

from scopewatch.executor import execute_action
from scopewatch.models import ExecutionStatus, PolicyOutcome, ReasonCode, RunStatus
from scopewatch.policy import evaluate_policy
from scopewatch.schemas import ActionRequest, Run, TaskScope


NOW = datetime.now(timezone.utc).isoformat()
ENV_CONTENTS = "SYNTHETIC_ENV_SECRET=not-a-real-token"


@pytest.fixture
def regression_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / "outputs").mkdir(parents=True)
    (workspace / ".env").write_text(ENV_CONTENTS, encoding="utf-8")
    return workspace


@pytest.fixture
def regression_run() -> Run:
    scope = TaskScope(
        schema_version="1",
        task_description="Read test files and write outputs without reading secrets.",
        allowed_paths=["tests", "outputs"],
        blocked_paths=[".env"],
        allowed_tools=["workspace"],
        allowed_operations=["read_text", "run_command"],
        allowed_network_destinations=[],
        requires_approval=[],
        allowed_commands=[["pytest"]],
        created_at=NOW,
    )
    return Run(
        id=str(uuid.uuid4()),
        name="Path Regression Run",
        task_scope=scope,
        status=RunStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        synthetic=True,
        interception_coverage="Synthetic test gateway",
        reasoning_availability="Unavailable",
    )


def _action(run: Run, operation: str, resource: str, arguments: dict | None = None):
    return ActionRequest(
        id=str(uuid.uuid4()),
        run_id=run.id,
        tool="workspace",
        operation=operation,
        resource=resource,
        arguments=arguments or {},
        requested_at=NOW,
    )


def _assert_denied(action: ActionRequest, run: Run, workspace: Path) -> None:
    env_file = workspace / ".env"
    before = env_file.read_text(encoding="utf-8")
    decision = evaluate_policy(action, run, workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.MALFORMED_REQUEST
    receipt = execute_action(action, workspace, policy_decision=decision)
    assert receipt.status == ExecutionStatus.NOT_EXECUTED
    assert env_file.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("length", [200_000, 1_000_000], ids=["200kb", "1mb"])
def test_overlong_run_command_argument_denied(
    regression_workspace: Path, regression_run: Run, docker_backend: None, length: int
) -> None:
    action = _action(
        regression_run,
        "run_command",
        ".",
        {"argv": ["pytest", "A" * length]},
    )
    _assert_denied(action, regression_run, regression_workspace)


@pytest.mark.parametrize("length", [200_000, 1_000_000], ids=["200kb", "1mb"])
def test_overlong_resource_path_denied(
    regression_workspace: Path, regression_run: Run, length: int
) -> None:
    action = _action(regression_run, "read_text", "A" * length)
    _assert_denied(action, regression_run, regression_workspace)


def test_symlinked_directory_cannot_read_blocked_workspace_env(
    regression_workspace: Path, regression_run: Run, local_backend: None
) -> None:
    (regression_workspace / "outputs" / "linkdir").symlink_to(
        regression_workspace, target_is_directory=True
    )
    action = _action(regression_run, "read_text", "outputs/linkdir/.env")
    decision = evaluate_policy(action, regression_run, regression_workspace)

    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.BLOCKED_PATH
    assert decision.matched_rule == "RULE_BLOCKED_PATH_MATCHED"
    receipt = execute_action(action, regression_workspace, policy_decision=decision)
    assert receipt.status == ExecutionStatus.NOT_EXECUTED
    assert "SYNTHETIC_ENV_SECRET" not in str(receipt.sanitized_result)
    assert (regression_workspace / ".env").read_text(encoding="utf-8") == ENV_CONTENTS

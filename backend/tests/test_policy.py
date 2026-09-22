"""Unit tests for deterministic policy engine."""

from datetime import datetime, timezone
from pathlib import Path
import pytest
import uuid

from scopewatch.models import (
    PolicyOutcome,
    ReasonCode,
    ReasoningProvenance,
    RunStatus,
)
from scopewatch.policy import evaluate_policy
from scopewatch.schemas import ActionRequest, Run, TaskScope


@pytest.fixture
def sample_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "invoices" / "approved").mkdir(parents=True)
    (ws / "invoices" / "private").mkdir(parents=True)
    (ws / "invoices" / "approved" / "vendor-a.txt").write_text("vendor a data", encoding="utf-8")
    (ws / "invoices" / "private" / "payroll.txt").write_text("payroll data", encoding="utf-8")
    (ws / "outputs").mkdir()
    return ws


@pytest.fixture
def active_run() -> Run:
    scope = TaskScope(
        schema_version="1",
        task_description="Read approved invoices and write a summary.",
        allowed_paths=["invoices/approved", "outputs"],
        blocked_paths=["invoices/private", "credentials"],
        allowed_tools=["workspace"],
        allowed_operations=["list_directory", "read_text", "write_text"],
        allowed_network_destinations=[],
        requires_approval=["delete_path"],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    return Run(
        id=str(uuid.uuid4()),
        name="Invoice Run",
        task_scope=scope,
        status=RunStatus.ACTIVE,
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
        synthetic=True,
        interception_coverage="Synthetic test gateway",
        reasoning_availability="Unavailable",
    )


def test_allowed_descendant_path(sample_workspace: Path, active_run: Run) -> None:
    action = ActionRequest(
        id=str(uuid.uuid4()),
        run_id=active_run.id,
        tool="workspace",
        operation="read_text",
        resource="invoices/approved/vendor-a.txt",
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    decision = evaluate_policy(action, active_run, sample_workspace)
    assert decision.outcome == PolicyOutcome.ALLOW
    assert decision.reason_code == ReasonCode.ALLOWED_TOOL_AND_RESOURCE
    assert decision.deterministic is True


def test_blocked_descendant_path(sample_workspace: Path, active_run: Run) -> None:
    action = ActionRequest(
        id=str(uuid.uuid4()),
        run_id=active_run.id,
        tool="workspace",
        operation="read_text",
        resource="invoices/private/payroll.txt",
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    decision = evaluate_policy(action, active_run, sample_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.BLOCKED_PATH


def test_blocked_precedence_over_allowed(sample_workspace: Path, active_run: Run) -> None:
    # Add a blocked subpath under an allowed folder
    active_run.task_scope.allowed_paths.append("reports")
    active_run.task_scope.blocked_paths.append("reports/confidential")

    action = ActionRequest(
        id=str(uuid.uuid4()),
        run_id=active_run.id,
        tool="workspace",
        operation="read_text",
        resource="reports/confidential/secret.txt",
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    decision = evaluate_policy(action, active_run, sample_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.BLOCKED_PATH


def test_path_prefix_confusion_rejected(sample_workspace: Path, active_run: Run) -> None:
    # Allowed is "invoices/approved". Request is "invoices/approved-copy/other.txt".
    # Should not be treated as a descendant.
    action = ActionRequest(
        id=str(uuid.uuid4()),
        run_id=active_run.id,
        tool="workspace",
        operation="read_text",
        resource="invoices/approved-copy/other.txt",
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    decision = evaluate_policy(action, active_run, sample_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.PATH_NOT_ALLOWED


def test_parent_traversal_rejected(sample_workspace: Path, active_run: Run) -> None:
    action = ActionRequest(
        id=str(uuid.uuid4()),
        run_id=active_run.id,
        tool="workspace",
        operation="read_text",
        resource="../../.ssh/id_rsa",
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    decision = evaluate_policy(action, active_run, sample_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.PATH_TRAVERSAL


def test_absolute_path_rejected(sample_workspace: Path, active_run: Run) -> None:
    action = ActionRequest(
        id=str(uuid.uuid4()),
        run_id=active_run.id,
        tool="workspace",
        operation="read_text",
        resource="/etc/passwd",
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    decision = evaluate_policy(action, active_run, sample_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.PATH_OUTSIDE_WORKSPACE


def test_null_byte_rejected(sample_workspace: Path, active_run: Run) -> None:
    action = ActionRequest(
        id=str(uuid.uuid4()),
        run_id=active_run.id,
        tool="workspace",
        operation="read_text",
        resource="invoices/approved/vendor-a.txt\x00.exe",
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    decision = evaluate_policy(action, active_run, sample_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.MALFORMED_REQUEST


def test_symlink_escape_rejected(sample_workspace: Path, active_run: Run, tmp_path: Path) -> None:
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("sensitive", encoding="utf-8")
    symlink_path = sample_workspace / "invoices" / "approved" / "symlink_escape.txt"
    try:
        symlink_path.symlink_to(outside_file)
    except OSError:
        pytest.skip("Symlinks not supported in test environment.")

    action = ActionRequest(
        id=str(uuid.uuid4()),
        run_id=active_run.id,
        tool="workspace",
        operation="read_text",
        resource="invoices/approved/symlink_escape.txt",
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    decision = evaluate_policy(action, active_run, sample_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.SYMLINK_ESCAPE


def test_unknown_operation_rejected(sample_workspace: Path, active_run: Run) -> None:
    action = ActionRequest(
        id=str(uuid.uuid4()),
        run_id=active_run.id,
        tool="workspace",
        operation="drop_database",
        resource="invoices/approved/vendor-a.txt",
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    decision = evaluate_policy(action, active_run, sample_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.UNSUPPORTED_OPERATION


def test_disallowed_tool_rejected(sample_workspace: Path, active_run: Run) -> None:
    action = ActionRequest(
        id=str(uuid.uuid4()),
        run_id=active_run.id,
        tool="bash",
        operation="read_text",
        resource="invoices/approved/vendor-a.txt",
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    decision = evaluate_policy(action, active_run, sample_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.TOOL_NOT_ALLOWED


def test_network_request_denied(sample_workspace: Path, active_run: Run) -> None:
    action = ActionRequest(
        id=str(uuid.uuid4()),
        run_id=active_run.id,
        tool="workspace",
        operation="network_request",
        resource="https://external-api.com",
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    decision = evaluate_policy(action, active_run, sample_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.NETWORK_DISABLED


def test_approval_required_operation_enters_hold(sample_workspace: Path, active_run: Run) -> None:
    action = ActionRequest(
        id=str(uuid.uuid4()),
        run_id=active_run.id,
        tool="workspace",
        operation="delete_path",
        resource="outputs/old_summary.txt",
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    decision = evaluate_policy(action, active_run, sample_workspace)
    assert decision.outcome == PolicyOutcome.HOLD
    assert decision.reason_code == ReasonCode.APPROVAL_REQUIRED


def test_inactive_run_rejected(sample_workspace: Path, active_run: Run) -> None:
    active_run.status = RunStatus.COMPLETED
    action = ActionRequest(
        id=str(uuid.uuid4()),
        run_id=active_run.id,
        tool="workspace",
        operation="read_text",
        resource="invoices/approved/vendor-a.txt",
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    decision = evaluate_policy(action, active_run, sample_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.OPERATION_NOT_ALLOWED

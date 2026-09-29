"""Unit tests for the controlled synthetic executor (both backends)."""

from datetime import datetime, timezone
from pathlib import Path
import pytest
import uuid

from scopewatch.config import MAX_READ_BYTES, MAX_WRITE_BYTES
from scopewatch.executor import ExecutionSecurityError, execute_action
from scopewatch.models import (
    ApprovalStatus,
    ExecutionStatus,
    PolicyOutcome,
    ReasonCode,
)
from scopewatch.schemas import ActionRequest, ApprovalRequest, PolicyDecision


@pytest.fixture(params=["local", "docker"], autouse=True)
def executor_backend(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> str:
    """Parametrize every executor test over both backends.

    The Docker variants require a reachable daemon and are skipped cleanly
    when Docker is absent; on CI with Docker they run the same assertions
    through the container-isolated backend.
    """
    backend = str(request.param)
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", backend)
    if backend == "docker":
        from scopewatch.executor_docker import is_docker_available

        if not is_docker_available():
            pytest.skip("Docker daemon unavailable; skipping Docker backend variant.")
    return backend


@pytest.fixture
def sample_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "docs").mkdir()
    (ws / "docs" / "file_b.txt").write_text("content B", encoding="utf-8")
    (ws / "docs" / "file_a.txt").write_text("content A", encoding="utf-8")
    (ws / "docs" / "subfolder").mkdir()
    return ws


@pytest.fixture
def allow_decision() -> PolicyDecision:
    return PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id="action-1",
        outcome=PolicyOutcome.ALLOW,
        reason_code=ReasonCode.ALLOWED_TOOL_AND_RESOURCE,
        explanation="Permitted",
        matched_rule="RULE_ALLOWED",
        decided_at=datetime.now(timezone.utc).isoformat(),
        deterministic=True,
    )


def test_list_directory_deterministic(sample_workspace: Path, allow_decision: PolicyDecision) -> None:
    action = ActionRequest(
        id="action-1",
        run_id="run-1",
        tool="workspace",
        operation="list_directory",
        resource="docs",
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    receipt = execute_action(action, sample_workspace, policy_decision=allow_decision)
    assert receipt.status == ExecutionStatus.EXECUTED
    assert receipt.sanitized_result is not None
    assert receipt.sanitized_result["item_count"] == 3
    names = [item["name"] for item in receipt.sanitized_result["items"]]
    assert names == ["file_a.txt", "file_b.txt", "subfolder"]


def test_bounded_read_text(sample_workspace: Path, allow_decision: PolicyDecision) -> None:
    action = ActionRequest(
        id="action-1",
        run_id="run-1",
        tool="workspace",
        operation="read_text",
        resource="docs/file_a.txt",
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    receipt = execute_action(action, sample_workspace, policy_decision=allow_decision)
    assert receipt.status == ExecutionStatus.EXECUTED
    assert receipt.sanitized_result is not None
    assert receipt.sanitized_result["byte_count"] == 9
    assert receipt.sanitized_result["preview"] == "content A"


def test_oversized_read_rejected(sample_workspace: Path, allow_decision: PolicyDecision) -> None:
    large_file = sample_workspace / "docs" / "large.txt"
    large_file.write_bytes(b"x" * (MAX_READ_BYTES + 10))

    action = ActionRequest(
        id="action-1",
        run_id="run-1",
        tool="workspace",
        operation="read_text",
        resource="docs/large.txt",
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    receipt = execute_action(action, sample_workspace, policy_decision=allow_decision)
    assert receipt.status == ExecutionStatus.FAILED
    assert receipt.error_code == "OVERSIZED_FILE"


def test_safe_write_text(sample_workspace: Path, allow_decision: PolicyDecision) -> None:
    action = ActionRequest(
        id="action-1",
        run_id="run-1",
        tool="workspace",
        operation="write_text",
        resource="docs/nested/output.txt",
        arguments={"content": "new synthetic content"},
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    receipt = execute_action(action, sample_workspace, policy_decision=allow_decision)
    assert receipt.status == ExecutionStatus.EXECUTED
    assert (sample_workspace / "docs" / "nested" / "output.txt").read_text(encoding="utf-8") == "new synthetic content"


def test_oversized_write_rejected(sample_workspace: Path, allow_decision: PolicyDecision) -> None:
    action = ActionRequest(
        id="action-1",
        run_id="run-1",
        tool="workspace",
        operation="write_text",
        resource="docs/oversized.txt",
        arguments={"content": "a" * (MAX_WRITE_BYTES + 100)},
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    receipt = execute_action(action, sample_workspace, policy_decision=allow_decision)
    assert receipt.status == ExecutionStatus.FAILED
    assert receipt.error_code == "OVERSIZED_PAYLOAD"


def test_direct_execution_without_policy_fails(sample_workspace: Path) -> None:
    action = ActionRequest(
        id="action-1",
        run_id="run-1",
        tool="workspace",
        operation="read_text",
        resource="docs/file_a.txt",
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    with pytest.raises(ExecutionSecurityError, match="Direct execution without policy evidence"):
        execute_action(action, sample_workspace, policy_decision=None)


def test_deny_decision_cannot_execute(sample_workspace: Path) -> None:
    deny_decision = PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id="action-1",
        outcome=PolicyOutcome.DENY,
        reason_code=ReasonCode.BLOCKED_PATH,
        explanation="Blocked",
        matched_rule="RULE_BLOCKED",
        decided_at=datetime.now(timezone.utc).isoformat(),
        deterministic=True,
    )
    action = ActionRequest(
        id="action-1",
        run_id="run-1",
        tool="workspace",
        operation="read_text",
        resource="docs/file_a.txt",
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    receipt = execute_action(action, sample_workspace, policy_decision=deny_decision)
    assert receipt.status == ExecutionStatus.NOT_EXECUTED
    assert receipt.error_code == ReasonCode.BLOCKED_PATH.value


def test_network_request_raises_security_error(sample_workspace: Path, allow_decision: PolicyDecision) -> None:
    action = ActionRequest(
        id="action-1",
        run_id="run-1",
        tool="workspace",
        operation="network_request",
        resource="https://external.com",
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    with pytest.raises(ExecutionSecurityError, match="Network requests are forbidden"):
        execute_action(action, sample_workspace, policy_decision=allow_decision)


def test_delete_path_is_simulated(sample_workspace: Path) -> None:
    file_to_delete = sample_workspace / "docs" / "to_delete.txt"
    file_to_delete.write_text("keep me", encoding="utf-8")

    hold_decision = PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id="action-del",
        outcome=PolicyOutcome.HOLD,
        reason_code=ReasonCode.APPROVAL_REQUIRED,
        explanation="Hold for approval",
        matched_rule="RULE_HOLD",
        decided_at=datetime.now(timezone.utc).isoformat(),
        deterministic=True,
    )
    approval = ApprovalRequest(
        id=str(uuid.uuid4()),
        run_id="run-1",
        action_request_id="action-del",
        policy_decision_id=hold_decision.id,
        status=ApprovalStatus.APPROVED,
        requested_at=datetime.now(timezone.utc).isoformat(),
        expires_at=datetime.now(timezone.utc).isoformat(),
    )
    action = ActionRequest(
        id="action-del",
        run_id="run-1",
        tool="workspace",
        operation="delete_path",
        resource="docs/to_delete.txt",
        requested_at=datetime.now(timezone.utc).isoformat(),
    )

    receipt = execute_action(action, sample_workspace, policy_decision=hold_decision, approval_request=approval)
    assert receipt.status == ExecutionStatus.EXECUTED
    assert receipt.sanitized_result["simulated"] is True
    # Verify file was NOT unlinked in the synthetic baseline
    assert file_to_delete.exists()

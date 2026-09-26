"""Controlled synthetic executor operating within a restricted workspace.

Single entry point is :func:`execute_action`, which dispatches to the
backend selected by ``SCOPEWATCH_EXECUTOR=local|docker`` (default ``local``).
"""

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable
import uuid

from scopewatch.config import MAX_READ_BYTES, MAX_WRITE_BYTES
from scopewatch.models import (
    ApprovalStatus,
    ExecutionStatus,
    PolicyOutcome,
    SUPPORTED_OPERATIONS,
)
from scopewatch.schemas import ActionRequest, ApprovalRequest, ExecutionReceipt, PolicyDecision


class ExecutionSecurityError(Exception):
    """Raised when execution invariants or policy boundaries are violated."""


LOCAL_EXECUTOR_NAME = "synthetic-workspace-executor"


@runtime_checkable
class ExecutorBackend(Protocol):
    """Execution backend interface shared by local and Docker executors."""

    def execute(
        self,
        action: ActionRequest,
        workspace_root: Path,
        policy_decision: Optional[PolicyDecision] = None,
        approval_request: Optional[ApprovalRequest] = None,
    ) -> ExecutionReceipt:
        ...


def get_executor_backend() -> str:
    """Return the configured executor backend name (``local`` by default)."""
    return os.environ.get("SCOPEWATCH_EXECUTOR", "local").strip().lower() or "local"


def _verify_workspace_containment(workspace_root: Path, target_path: Path) -> Path:
    """Ensure target_path resolves strictly within workspace_root."""
    resolved_root = workspace_root.resolve()
    resolved_target = target_path.resolve()
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as exc:
        raise ExecutionSecurityError("Path escapes workspace boundary.") from exc
    return resolved_target


def _execute_local(
    action: ActionRequest,
    workspace_root: Path,
    policy_decision: Optional[PolicyDecision] = None,
    approval_request: Optional[ApprovalRequest] = None,
) -> ExecutionReceipt:
    """Execute an authorized action inside the synthetic workspace (local backend)."""
    receipt_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()

    # Invariant: Must have policy evidence
    if policy_decision is None:
        raise ExecutionSecurityError("Direct execution without policy evidence is prohibited.")

    # Invariant: If policy decision is DENY, cannot execute
    if policy_decision.outcome == PolicyOutcome.DENY:
        completed_at = datetime.now(timezone.utc).isoformat()
        return ExecutionReceipt(
            id=receipt_id,
            action_request_id=action.id,
            status=ExecutionStatus.NOT_EXECUTED,
            started_at=started_at,
            completed_at=completed_at,
            executor="synthetic-workspace-executor",
            sanitized_result={"reason": "Policy outcome was DENY; execution blocked."},
            error_code=policy_decision.reason_code.value,
            resource=action.resource,
            operation=action.operation,
        )

    # Invariant: If policy decision is HOLD, must have an APPROVED or CONSUMED approval request
    if policy_decision.outcome == PolicyOutcome.HOLD:
        if (
            approval_request is None
            or approval_request.status not in (ApprovalStatus.APPROVED, ApprovalStatus.CONSUMED)
            or approval_request.action_request_id != action.id
        ):
            raise ExecutionSecurityError(
                "Held action requires valid approved status to execute."
            )

    # Invariant: Network operations must never execute
    if action.operation == "network_request":
        raise ExecutionSecurityError("Network requests are forbidden in synthetic executor.")

    # Target path resolution
    resolved_workspace = workspace_root.resolve()
    target_path = resolved_workspace / action.resource

    try:
        _verify_workspace_containment(resolved_workspace, target_path)

        if action.operation == "list_directory":
            if not target_path.exists():
                raise FileNotFoundError(f"Directory not found: {action.resource}")
            if not target_path.is_dir():
                raise NotADirectoryError(f"Resource is not a directory: {action.resource}")

            items = []
            for entry in sorted(os.listdir(target_path)):
                entry_path = target_path / entry
                is_dir = entry_path.is_dir()
                items.append({
                    "name": entry,
                    "type": "directory" if is_dir else "file",
                })

            completed_at = datetime.now(timezone.utc).isoformat()
            return ExecutionReceipt(
                id=receipt_id,
                action_request_id=action.id,
                status=ExecutionStatus.EXECUTED,
                started_at=started_at,
                completed_at=completed_at,
                executor="synthetic-workspace-executor",
                sanitized_result={
                    "operation": "list_directory",
                    "resource": action.resource,
                    "item_count": len(items),
                    "items": items,
                },
                error_code=None,
                resource=action.resource,
                operation=action.operation,
            )

        elif action.operation == "read_text":
            if not target_path.exists():
                raise FileNotFoundError(f"File not found: {action.resource}")
            if target_path.is_dir():
                raise IsADirectoryError(f"Resource is a directory, not a text file: {action.resource}")

            file_size = target_path.stat().st_size
            if file_size > MAX_READ_BYTES:
                completed_at = datetime.now(timezone.utc).isoformat()
                return ExecutionReceipt(
                    id=receipt_id,
                    action_request_id=action.id,
                    status=ExecutionStatus.FAILED,
                    started_at=started_at,
                    completed_at=completed_at,
                    executor="synthetic-workspace-executor",
                    sanitized_result={"error": f"File size {file_size} exceeds {MAX_READ_BYTES} limit."},
                    error_code="OVERSIZED_FILE",
                    resource=action.resource,
                    operation=action.operation,
                )

            with open(target_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()

            preview = content[:500] if len(content) > 500 else content
            completed_at = datetime.now(timezone.utc).isoformat()
            return ExecutionReceipt(
                id=receipt_id,
                action_request_id=action.id,
                status=ExecutionStatus.EXECUTED,
                started_at=started_at,
                completed_at=completed_at,
                executor="synthetic-workspace-executor",
                sanitized_result={
                    "operation": "read_text",
                    "resource": action.resource,
                    "byte_count": file_size,
                    "preview": preview,
                    "truncated": len(content) > 500,
                },
                error_code=None,
                resource=action.resource,
                operation=action.operation,
            )

        elif action.operation == "write_text":
            content_to_write = action.arguments.get("content", "")
            if not isinstance(content_to_write, str):
                content_to_write = str(content_to_write)

            content_bytes = content_to_write.encode("utf-8")
            if len(content_bytes) > MAX_WRITE_BYTES:
                completed_at = datetime.now(timezone.utc).isoformat()
                return ExecutionReceipt(
                    id=receipt_id,
                    action_request_id=action.id,
                    status=ExecutionStatus.FAILED,
                    started_at=started_at,
                    completed_at=completed_at,
                    executor="synthetic-workspace-executor",
                    sanitized_result={"error": f"Payload {len(content_bytes)} bytes exceeds {MAX_WRITE_BYTES} limit."},
                    error_code="OVERSIZED_PAYLOAD",
                    resource=action.resource,
                    operation=action.operation,
                )

            # Ensure parent directory inside workspace
            parent_dir = target_path.parent
            _verify_workspace_containment(resolved_workspace, parent_dir)
            parent_dir.mkdir(parents=True, exist_ok=True)

            with open(target_path, "w", encoding="utf-8") as f:
                f.write(content_to_write)

            completed_at = datetime.now(timezone.utc).isoformat()
            return ExecutionReceipt(
                id=receipt_id,
                action_request_id=action.id,
                status=ExecutionStatus.EXECUTED,
                started_at=started_at,
                completed_at=completed_at,
                executor="synthetic-workspace-executor",
                sanitized_result={
                    "operation": "write_text",
                    "resource": action.resource,
                    "bytes_written": len(content_bytes),
                },
                error_code=None,
                resource=action.resource,
                operation=action.operation,
            )

        elif action.operation == "delete_path":
            # In the synthetic baseline, delete_path is simulated to avoid deleting files
            completed_at = datetime.now(timezone.utc).isoformat()
            return ExecutionReceipt(
                id=receipt_id,
                action_request_id=action.id,
                status=ExecutionStatus.EXECUTED,
                started_at=started_at,
                completed_at=completed_at,
                executor="synthetic-workspace-executor",
                sanitized_result={
                    "operation": "delete_path",
                    "resource": action.resource,
                    "simulated": True,
                    "note": "Deletion simulated safely in baseline demo; target not unlinked.",
                },
                error_code=None,
                resource=action.resource,
                operation=action.operation,
            )

        else:
            raise ExecutionSecurityError(f"Unsupported operation: {action.operation}")

    except Exception as exc:
        completed_at = datetime.now(timezone.utc).isoformat()
        sanitized_msg = type(exc).__name__
        if isinstance(exc, (FileNotFoundError, NotADirectoryError, IsADirectoryError)):
            sanitized_msg = str(exc)
        return ExecutionReceipt(
            id=receipt_id,
            action_request_id=action.id,
            status=ExecutionStatus.FAILED,
            started_at=started_at,
            completed_at=completed_at,
            executor="synthetic-workspace-executor",
            sanitized_result={"error": sanitized_msg},
            error_code="EXECUTION_FAILED",
            resource=action.resource,
            operation=action.operation,
        )


class LocalWorkspaceExecutor:
    """Local backend preserving the current synthetic workspace behaviour."""

    executor_name = LOCAL_EXECUTOR_NAME

    def execute(
        self,
        action: ActionRequest,
        workspace_root: Path,
        policy_decision: Optional[PolicyDecision] = None,
        approval_request: Optional[ApprovalRequest] = None,
    ) -> ExecutionReceipt:
        return _execute_local(
            action,
            workspace_root,
            policy_decision=policy_decision,
            approval_request=approval_request,
        )


def execute_action(
    action: ActionRequest,
    workspace_root: Path,
    policy_decision: Optional[PolicyDecision] = None,
    approval_request: Optional[ApprovalRequest] = None,
) -> ExecutionReceipt:
    """Single gateway entry point; dispatches to the configured backend.

    ``SCOPEWATCH_EXECUTOR=docker`` selects the Docker-isolated backend,
    anything else (including unset) selects the local backend. Docker
    failures fail closed inside the Docker backend and never fall back to
    local execution.
    """
    if get_executor_backend() == "docker":
        from scopewatch.executor_docker import DockerExecutor

        return DockerExecutor().execute(
            action,
            workspace_root,
            policy_decision=policy_decision,
            approval_request=approval_request,
        )
    return _execute_local(
        action,
        workspace_root,
        policy_decision=policy_decision,
        approval_request=approval_request,
    )

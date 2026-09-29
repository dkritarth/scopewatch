"""Controlled synthetic executor operating within a restricted workspace.

Single entry point is :func:`execute_action`, which dispatches to the
backend selected by ``SCOPEWATCH_EXECUTOR=local|docker`` (default ``local``).
"""

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Optional
import uuid

from scopewatch.config import MAX_READ_BYTES, MAX_WRITE_BYTES
from scopewatch.models import (
    ApprovalStatus,
    ExecutionStatus,
    PolicyOutcome,
    RunStatus,
    SUPPORTED_OPERATIONS,
)
from scopewatch.path_access import normalize_relative_path, scoped_path_reason
from scopewatch.schemas import ActionRequest, ApprovalRequest, ExecutionReceipt, PolicyDecision, TaskScope


class ExecutionSecurityError(Exception):
    """Raised when execution invariants or policy boundaries are violated."""


LOCAL_EXECUTOR_NAME = "synthetic-workspace-executor"


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


def _verify_stored_run_not_terminal(
    action: ActionRequest,
    db_path: Path | str,
) -> None:
    """Refuse execution when the stored run is terminal or unknown.

    Used when the caller supplies a store handle: direct executor calls
    bypass the service-layer run-status gate, so the executor re-checks the
    run lifecycle itself. Any store failure fails closed. Messages are
    static so no stored content leaks into errors.
    """
    from scopewatch.db import get_connection
    from scopewatch.repository import ScopewatchRepository

    try:
        conn = get_connection(db_path)
    except Exception as exc:
        raise ExecutionSecurityError(
            "Stored run authorization is unavailable; failing closed."
        ) from exc
    try:
        run = ScopewatchRepository.get_run(conn, action.run_id)
        if run is None:
            raise ExecutionSecurityError(
                "Stored run is unknown; execution refused."
            )
        if run.status in (RunStatus.COMPLETED, RunStatus.FAILED):
            raise ExecutionSecurityError(
                "Stored run is in a terminal state; execution refused."
            )
    except ExecutionSecurityError:
        raise
    except Exception as exc:
        raise ExecutionSecurityError(
            "Stored run authorization could not be verified; failing closed."
        ) from exc
    finally:
        conn.close()


def _verify_stored_approval_single_use(
    action: ActionRequest,
    approval_request: ApprovalRequest,
    db_path: Path | str,
) -> None:
    """Enforce verifiable single-use against the stored authorization.

    The presented approval must exactly match the stored approval for this
    action (same approval id), the stored approval must still be APPROVED
    (not PENDING, CONSUMED, DENIED, or EXPIRED), and no execution receipt
    may already exist for the action. Any violation or store failure fails
    closed with a static message.
    """
    from scopewatch.db import get_connection
    from scopewatch.repository import ScopewatchRepository

    _verify_stored_run_not_terminal(action, db_path)
    try:
        conn = get_connection(db_path)
    except Exception as exc:
        raise ExecutionSecurityError(
            "Stored approval authorization is unavailable; failing closed."
        ) from exc
    try:
        stored = ScopewatchRepository.get_approval_by_action(conn, action.id)
        if stored is None or stored.id != approval_request.id:
            raise ExecutionSecurityError(
                "No matching stored approval for this action; execution refused."
            )
        if stored.status != ApprovalStatus.APPROVED:
            raise ExecutionSecurityError(
                "Stored approval is not APPROVED; replay refused."
            )
        receipt = ScopewatchRepository.get_execution_receipt_by_action(conn, action.id)
        if receipt is not None:
            raise ExecutionSecurityError(
                "Action was already executed; replay refused."
            )
    except ExecutionSecurityError:
        raise
    except Exception as exc:
        raise ExecutionSecurityError(
            "Stored approval authorization could not be verified; failing closed."
        ) from exc
    finally:
        conn.close()
def _verify_scoped_target(
    workspace_root: Path, resource: str, task_scope: Optional[TaskScope],
    *, require_allowed: bool = True,
) -> Path:
    """Re-resolve immediately before execution; use the checked target for I/O."""
    root = workspace_root.resolve()
    requested = normalize_relative_path(resource)
    target = _verify_workspace_containment(root, root / requested)
    canonical = target.relative_to(root)
    if task_scope is None:
        # A direct caller without scope evidence cannot authorize an alias.
        if canonical != requested:
            raise ExecutionSecurityError("Symlink target requires task scope evidence.")
    else:
        allowed = task_scope.allowed_paths if require_allowed else ["."]
        if scoped_path_reason(requested, canonical, allowed, task_scope.blocked_paths):
            raise ExecutionSecurityError("Resolved path violates the task scope.")
    return target


def _execute_local(
    action: ActionRequest,
    workspace_root: Path,
    policy_decision: Optional[PolicyDecision] = None,
    approval_request: Optional[ApprovalRequest] = None,
    db_path: Optional[Path | str] = None,
    task_scope: Optional[TaskScope] = None,
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

    # Invariant: If policy decision is HOLD, must have an APPROVED approval
    # request bound to this exact action. CONSUMED approvals never execute:
    # single-use means a consumed approval cannot authorize a replay, and the
    # service always presents the APPROVED object (it consumes afterwards).
    if policy_decision.outcome == PolicyOutcome.HOLD:
        if (
            approval_request is None
            or approval_request.status != ApprovalStatus.APPROVED
            or approval_request.action_request_id != action.id
        ):
            raise ExecutionSecurityError(
                "Held action requires valid approved status to execute."
            )
        # Verifiable single-use: when given a store handle, the executor
        # consults the stored approval, the execution receipt, and the run
        # status instead of trusting the in-memory object alone.
        if db_path is not None:
            _verify_stored_approval_single_use(action, approval_request, db_path)
    elif db_path is not None:
        # Non-held actions carry no approval, but a store handle still binds
        # them to the run lifecycle: terminal runs execute nothing.
        _verify_stored_run_not_terminal(action, db_path)

    # Invariant: Network operations must never execute
    if action.operation == "network_request":
        raise ExecutionSecurityError("Network requests are forbidden in synthetic executor.")

    # Invariant: run_command is Docker-only (policy denies it for the local
    # backend, but the executor refuses it too as defence in depth).
    if action.operation == "run_command":
        raise ExecutionSecurityError(
            "run_command requires the Docker executor (SCOPEWATCH_EXECUTOR=docker)."
        )

    # Target path resolution
    resolved_workspace = workspace_root.resolve()

    try:
        target_path = _verify_scoped_target(
            resolved_workspace, action.resource, task_scope
        )

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


def execute_action(
    action: ActionRequest,
    workspace_root: Path,
    policy_decision: Optional[PolicyDecision] = None,
    approval_request: Optional[ApprovalRequest] = None,
    db_path: Optional[Path | str] = None,
    task_scope: Optional[TaskScope] = None,
) -> ExecutionReceipt:
    """Single gateway entry point; dispatches to the configured backend.

    ``SCOPEWATCH_EXECUTOR=docker`` selects the Docker-isolated backend,
    anything else (including unset) selects the local backend. Docker
    failures fail closed inside the Docker backend and never fall back to
    local execution.

    ``db_path`` optionally binds execution to the stored authorization
    (single-use approval + execution receipt + run lifecycle; issue #66).
    ``task_scope`` optionally enables dispatch-time run_command path
    revalidation in the Docker backend (issue #68). Both default to None,
    which preserves the existing service call shape.
    """
    if get_executor_backend() == "docker":
        from scopewatch.executor_docker import DockerExecutor

        return DockerExecutor().execute(
            action,
            workspace_root,
            policy_decision=policy_decision,
            approval_request=approval_request,
            db_path=db_path,
            task_scope=task_scope,
        )
    return _execute_local(
        action,
        workspace_root,
        policy_decision=policy_decision,
        approval_request=approval_request,
        db_path=db_path,
        task_scope=task_scope,
    )

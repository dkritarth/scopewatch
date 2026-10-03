"""Docker-isolated executor backend for Scopewatch.

Runs allowed file operations inside a per-run, hardened container. The
container mounts ONLY a per-run copy of the synthetic workspace at
``/workspace`` and never mounts host home, the Docker socket, or
credentials. All dispatches still require a stored policy decision (checked
on the host before Docker is touched); Docker-unavailable fails closed with
``EXECUTION_FAILED`` and never falls back to local execution.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, Optional

# Shared Docker job construction lives in scopewatch.docker_job (issue #106):
# image pin, hardening constants, helper code, `docker run` flag list, and the
# staged-workspace copy / copy-back walk. Imported here (not redefined) so the
# gateway and the runner cannot drift; the cross-test that pins them together
# is backend/tests/test_docker_job_share.py. Pre-dispatch gates stay here
# (issue #105 owns their sharing); transport/auth/socket stay in the runner.
#
# These names stay bound in this module's namespace on purpose: the gateway's
# monkeypatch-based tests patch them here, not in the shared module.
from scopewatch.docker_job import (
    CONTAINER_WORKSPACE,
    DOCKER_CPUS,
    DOCKER_IMAGE,
    DOCKER_MEMORY,
    DOCKER_PIDS_LIMIT,
    DOCKER_RUN_TIMEOUT_BUFFER_S,
    DOCKER_TIMEOUT_S,
    DOCKER_USER,
    RUN_COMMAND_DEFAULT_TIMEOUT_S,
    RUN_COMMAND_MAX_TIMEOUT_S,
    RUN_COMMAND_MIN_TIMEOUT_S,
    RUN_COMMAND_OUTPUT_LIMIT,
    best_effort_remove as _best_effort_remove,
    build_docker_command,
    clamp_run_command_timeout,
    helper_code as _helper_code,
    make_world_accessible as _make_world_accessible,
    prepare_docker_job,
    replace_link as _replace_link,
    stage_workspace_copy as _stage_workspace_copy,
    sync_copy_back as _sync_copy_back,
)
from scopewatch.models import ApprovalStatus, ExecutionStatus, PolicyOutcome
from scopewatch.schemas import (
    ActionRequest,
    ApprovalRequest,
    ExecutionReceipt,
    PolicyDecision,
    TaskScope,
)

# Optional image override (used by CI, which builds backend/executor/Dockerfile:
# pinned base + hashed lock so `python -m pytest` exists in the container).
# Explicit constructor/image arguments always win over this variable.
EXECUTOR_IMAGE_ENV_VAR = "SCOPEWATCH_EXECUTOR_IMAGE"


def resolve_executor_image(image: Optional[str] = None) -> str:
    """Return the container image to run: explicit arg, env override, or pin."""
    if image:
        return image
    return os.environ.get(EXECUTOR_IMAGE_ENV_VAR, DOCKER_IMAGE)

EXECUTOR_NAME = "docker-executor"


def extract_run_command_argv(action: ActionRequest) -> Optional[list[str]]:
    """Derive the argv list for a run_command action, or None if malformed.

    Accepts the string form (parsed here with shlex, mirroring the policy)
    or the pre-split list form. Never uses a shell.
    """
    arguments = action.arguments or {}
    raw_command = arguments.get("command")
    argv_arg = arguments.get("argv")
    if isinstance(raw_command, str):
        try:
            argv = shlex.split(raw_command, posix=True)
        except ValueError:
            return None
        return argv or None
    if (
        isinstance(argv_arg, list)
        and argv_arg
        and all(isinstance(item, str) for item in argv_arg)
    ):
        return list(argv_arg)
    return None







def is_docker_available(timeout_s: float = 5.0) -> bool:
    """Return True when a Docker daemon is reachable via the CLI."""
    try:
        proc = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_s,
        )
        return proc.returncode == 0
    except Exception:
        return False






def _load_scope_from_store(
    action: ActionRequest, db_path: Path | str | sqlite3.Connection
) -> Optional[TaskScope]:
    """Best-effort load of the run's task scope; None when unverifiable."""
    try:
        from scopewatch.executor import _resolve_connection
        from scopewatch.repository import ScopewatchRepository

        conn, should_close = _resolve_connection(db_path)
        try:
            run = ScopewatchRepository.get_run(conn, action.run_id)
        finally:
            if should_close:
                conn.close()
    except Exception:
        return None
    return run.task_scope if run is not None else None


class DockerExecutor:
    """Execute actions inside a per-run hardened container."""

    def __init__(
        self,
        image: Optional[str] = None,
        timeout_s: float = DOCKER_TIMEOUT_S,
    ) -> None:
        self.image = resolve_executor_image(image)
        self.timeout_s = timeout_s

    def execute(
        self,
        action: ActionRequest,
        workspace_root: Path,
        policy_decision: Optional[PolicyDecision] = None,
        approval_request: Optional[ApprovalRequest] = None,
        db_path: Optional[Path | str | sqlite3.Connection] = None,
        task_scope: Optional[TaskScope] = None,
    ) -> ExecutionReceipt:
        receipt_id = str(uuid.uuid4())
        started_at = datetime.now(timezone.utc).isoformat()

        def _failed(error_code: str, message: str) -> ExecutionReceipt:
            completed_at = datetime.now(timezone.utc).isoformat()
            return ExecutionReceipt(
                id=receipt_id,
                action_request_id=action.id,
                status=ExecutionStatus.FAILED,
                started_at=started_at,
                completed_at=completed_at,
                executor=EXECUTOR_NAME,
                sanitized_result={"error": message},
                error_code=error_code,
                resource=action.resource,
                operation=action.operation,
            )

        # Defence in depth: stored-decision checks run on the host before
        # Docker is touched. Mirrors the local backend invariants.
        if policy_decision is None:
            from scopewatch.executor import ExecutionSecurityError

            raise ExecutionSecurityError(
                "Direct execution without policy evidence is prohibited."
            )
        if policy_decision.outcome == PolicyOutcome.DENY:
            completed_at = datetime.now(timezone.utc).isoformat()
            return ExecutionReceipt(
                id=receipt_id,
                action_request_id=action.id,
                status=ExecutionStatus.NOT_EXECUTED,
                started_at=started_at,
                completed_at=completed_at,
                executor=EXECUTOR_NAME,
                sanitized_result={"reason": "Policy outcome was DENY; execution blocked."},
                error_code=policy_decision.reason_code.value,
                resource=action.resource,
                operation=action.operation,
            )
        if policy_decision.outcome == PolicyOutcome.HOLD:
            # Single-use (issue #66): only a live APPROVED approval bound to
            # this exact action executes. CONSUMED approvals never execute;
            # the service presents APPROVED and consumes afterwards.
            if (
                approval_request is None
                or approval_request.status != ApprovalStatus.APPROVED
                or approval_request.action_request_id != action.id
            ):
                from scopewatch.executor import ExecutionSecurityError

                raise ExecutionSecurityError(
                    "Held action requires valid approved status to execute."
                )
            # Verifiable single-use: with a store handle, consult the stored
            # approval, the execution receipt, and the run status.
            if db_path is not None:
                from scopewatch.executor import _verify_stored_approval_single_use

                _verify_stored_approval_single_use(action, approval_request, db_path)
        elif db_path is not None:
            # Non-held actions carry no approval, but a store handle still
            # binds them to the run lifecycle.
            from scopewatch.executor import _verify_stored_run_not_terminal

            _verify_stored_run_not_terminal(action, db_path)
        if action.operation == "network_request":
            from scopewatch.executor import ExecutionSecurityError

            raise ExecutionSecurityError("Network requests are forbidden in synthetic executor.")

        from scopewatch.executor import ExecutionSecurityError, _verify_scoped_target

        try:
            _verify_scoped_target(
                workspace_root, action.resource, task_scope,
                require_allowed=action.operation != "run_command",
            )
        except (ExecutionSecurityError, OSError, RuntimeError):
            return _failed("EXECUTION_FAILED", "Resolved path violates the task scope.")

        if not is_docker_available():
            return _failed("EXECUTION_FAILED", "Docker daemon unavailable; failing closed.")

        workspace_copy: Optional[Path] = None
        staging_root: Optional[Path] = None
        container_name = f"scopewatch-{uuid.uuid4().hex[:12]}"
        try:
            resolved_workspace = workspace_root.resolve()
            if not resolved_workspace.exists():
                return _failed("EXECUTION_FAILED", "Workspace root not found.")
            try:
                staging_root, workspace_copy = _stage_workspace_copy(
                    resolved_workspace
                )
            except OSError:
                return _failed("EXECUTION_FAILED", "Workspace staging failed.")
            try:
                _verify_scoped_target(
                    workspace_copy, action.resource, task_scope,
                    require_allowed=action.operation != "run_command",
                )
            except (ExecutionSecurityError, OSError, RuntimeError):
                return _failed("EXECUTION_FAILED", "Resolved path violates the task scope.")
            # run_command (issue #68): revalidate the path-bearing argv and
            # the cwd against the run scope in the STAGED workspace
            # immediately before dispatch. A symlink swapped between the
            # policy decision and staging resolves to its staged target
            # here; a blocked or escaping target refuses dispatch before
            # any container is invoked.
            if action.operation == "run_command":
                scope_for_revalidation = task_scope
                if scope_for_revalidation is None and db_path is not None:
                    scope_for_revalidation = _load_scope_from_store(action, db_path)
                    if scope_for_revalidation is None:
                        return _failed(
                            "EXECUTION_FAILED",
                            "run_command scope unavailable; failing closed.",
                        )
                if scope_for_revalidation is not None:
                    from scopewatch.policy import revalidate_run_command_in_workspace

                    denial = revalidate_run_command_in_workspace(
                        action, scope_for_revalidation, workspace_copy
                    )
                    if denial is not None:
                        reason_code, _explanation, _matched_rule = denial
                        return _failed(
                            reason_code.value,
                            "run_command dispatch refused: staged path revalidation failed.",
                        )
            # run_command (issue #36): normalize argv + timeout on the host so
            # the container always receives the argv list form with no shell.
            # The policy already allowlisted the command; a failure to
            # re-derive argv here fails closed.
            run_timeout_s: Optional[float] = None
            if action.operation == "run_command":
                run_argv = extract_run_command_argv(action)
                if run_argv is None:
                    return _failed("EXECUTION_FAILED", "Malformed run_command arguments.")
                run_timeout_s = clamp_run_command_timeout(
                    (action.arguments or {}).get("timeout_s", RUN_COMMAND_DEFAULT_TIMEOUT_S)
                )
                job_arguments: dict[str, Any] = {"argv": run_argv, "timeout_s": run_timeout_s}
            else:
                job_arguments = action.arguments or {}
            # One call builds the job and the exact JSON the container parses,
            # so the dispatched arguments cannot drift from the normalized ones
            # (the #104 failure mode). The host wait is derived from the same
            # timeout the container receives.
            job = prepare_docker_job(
                operation=action.operation,
                resource=action.resource,
                arguments=job_arguments,
                timeout_s=run_timeout_s,
            )
            cmd = build_docker_command(
                image=self.image,
                workspace_copy=workspace_copy,
                job=job,
                container_name=container_name,
                run_label=action.run_id,
            )
            try:
                proc = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=job.host_timeout_s(self.timeout_s),
                )
            except subprocess.TimeoutExpired:
                _best_effort_remove(container_name)
                return _failed("EXECUTION_FAILED", "Docker execution timed out.")
            except FileNotFoundError:
                return _failed("EXECUTION_FAILED", "Docker daemon unavailable; failing closed.")
            except Exception:
                _best_effort_remove(container_name)
                return _failed("EXECUTION_FAILED", "Docker execution failed.")
            if proc.returncode != 0:
                return _failed("EXECUTION_FAILED", "Docker execution failed.")
            try:
                payload: dict[str, Any] = json.loads(proc.stdout.decode("utf-8", errors="replace"))
            except Exception:
                return _failed("EXECUTION_FAILED", "Docker helper returned malformed output.")
            status = payload.get("status")
            result = payload.get("result") or {}
            error_code = payload.get("error_code")
            if status not in ("EXECUTED", "FAILED"):
                return _failed("EXECUTION_FAILED", "Docker helper returned malformed output.")
            completed_at = datetime.now(timezone.utc).isoformat()
            exec_status = (
                ExecutionStatus.EXECUTED if status == "EXECUTED" else ExecutionStatus.FAILED
            )
            # Sync successful writes back to the host workspace so the
            # gateway observes the same effect as the local backend. Reads,
            # listings, and simulated deletes need no sync, but a full copy
            # back is cheap for synthetic fixtures and keeps both backends
            # behaviourally identical.
            if exec_status == ExecutionStatus.EXECUTED and action.operation in (
                "write_text",
                "run_command",
            ):
                try:
                    _sync_copy_back(workspace_copy, resolved_workspace)
                except Exception:
                    return _failed("EXECUTION_FAILED", "Docker execution failed.")
            return ExecutionReceipt(
                id=receipt_id,
                action_request_id=action.id,
                status=exec_status,
                started_at=started_at,
                completed_at=completed_at,
                executor=EXECUTOR_NAME,
                sanitized_result=result,
                error_code=error_code,
                resource=action.resource,
                operation=action.operation,
            )
        finally:
            if staging_root is not None:
                shutil.rmtree(staging_root, ignore_errors=True)
            _best_effort_remove(container_name)




def execute_action_docker(
    action: ActionRequest,
    workspace_root: Path,
    policy_decision: Optional[PolicyDecision] = None,
    approval_request: Optional[ApprovalRequest] = None,
    image: Optional[str] = None,
    db_path: Optional[Path | str | sqlite3.Connection] = None,
    task_scope: Optional[TaskScope] = None,
) -> ExecutionReceipt:
    """Convenience wrapper used by the gateway entry point and tests."""
    return DockerExecutor(image=image).execute(
        action,
        workspace_root,
        policy_decision=policy_decision,
        approval_request=approval_request,
        db_path=db_path,
        task_scope=task_scope,
    )

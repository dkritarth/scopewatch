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
import math
import shlex
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from scopewatch.config import MAX_READ_BYTES, MAX_WRITE_BYTES
from scopewatch.models import ApprovalStatus, ExecutionStatus, PolicyOutcome
from scopewatch.schemas import (
    ActionRequest,
    ApprovalRequest,
    ExecutionReceipt,
    PolicyDecision,
)

# Pinned base image digest (Debian bookworm slim Python).
# Source: Docker Hub library/python layer page for 3.12-slim-bookworm,
# index digest as published June 2026.
DOCKER_IMAGE = (
    "python:3.12-slim-bookworm@"
    "sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254"
)

CONTAINER_WORKSPACE = "/workspace"
DOCKER_USER = "65534:65534"  # Debian 'nobody', non-root
DOCKER_MEMORY = "256m"
DOCKER_CPUS = "1.0"
DOCKER_PIDS_LIMIT = "64"
DOCKER_TIMEOUT_S = 60

EXECUTOR_NAME = "docker-executor"

# run_command limits (issue #36): default per-command timeout, host-side
# clamp bounds, and per-stream output cap with a truncation marker.
RUN_COMMAND_DEFAULT_TIMEOUT_S = 60.0
RUN_COMMAND_MIN_TIMEOUT_S = 1.0
RUN_COMMAND_MAX_TIMEOUT_S = 300.0
RUN_COMMAND_OUTPUT_LIMIT = 64 * 1024
DOCKER_RUN_TIMEOUT_BUFFER_S = 30.0


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


def clamp_run_command_timeout(value: Any) -> float:
    """Clamp a requested per-command timeout to the allowed range."""
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return RUN_COMMAND_DEFAULT_TIMEOUT_S
    if math.isnan(timeout) or math.isinf(timeout):
        return RUN_COMMAND_DEFAULT_TIMEOUT_S
    return min(max(timeout, RUN_COMMAND_MIN_TIMEOUT_S), RUN_COMMAND_MAX_TIMEOUT_S)


def _helper_code() -> str:
    """Return the in-container helper source.

    The helper re-validates that every path resolves inside /workspace
    (defence in depth: the host policy already denied escapes) and emits a
    single JSON document with structured outputs.
    """
    return (
        "import json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        f"MAX_READ = {int(MAX_READ_BYTES)}\n"
        f"MAX_WRITE = {int(MAX_WRITE_BYTES)}\n"
        f"RUN_CMD_LIMIT = {int(RUN_COMMAND_OUTPUT_LIMIT)}\n"
        f"RUN_CMD_TIMEOUT_DEFAULT = {float(RUN_COMMAND_DEFAULT_TIMEOUT_S)}\n"
        f"RUN_CMD_TIMEOUT_MIN = {float(RUN_COMMAND_MIN_TIMEOUT_S)}\n"
        f"RUN_CMD_TIMEOUT_MAX = {float(RUN_COMMAND_MAX_TIMEOUT_S)}\n"
        "WS = Path(os.environ.get('SCOPEWATCH_WORKSPACE', '/workspace'))\n"
        "def fail(msg, code):\n"
        "    print(json.dumps({'status': 'FAILED', 'result': {'error': msg}, 'error_code': code}))\n"
        "    return\n"
        "def main():\n"
        "    if len(sys.argv) < 4:\n"
        "        fail('malformed helper invocation', 'EXECUTION_FAILED')\n"
        "        return\n"
        "    op = sys.argv[1]\n"
        "    resource = sys.argv[2]\n"
        "    try:\n"
        "        args = json.loads(sys.argv[3]) if sys.argv[3] else {}\n"
        "    except Exception:\n"
        "        fail('malformed arguments', 'EXECUTION_FAILED')\n"
        "        return\n"
        "    if '\\x00' in resource:\n"
        "        fail('null byte in path', 'EXECUTION_FAILED')\n"
        "        return\n"
        "    if resource.startswith('/') or resource.startswith('\\\\') or (len(resource) > 1 and resource[1] == ':'):\n"
        "        fail('absolute paths are prohibited', 'EXECUTION_FAILED')\n"
        "        return\n"
        "    target = (WS / resource)\n"
        "    try:\n"
        "        resolved = target.resolve()\n"
        "        resolved.relative_to(WS.resolve())\n"
        "    except ValueError:\n"
        "        fail('path escapes workspace boundary', 'EXECUTION_FAILED')\n"
        "        return\n"
        "    except Exception:\n"
        "        fail('path resolution failed', 'EXECUTION_FAILED')\n"
        "        return\n"
        "    if op == 'network_request':\n"
        "        fail('network requests are forbidden', 'EXECUTION_FAILED')\n"
        "        return\n"
        "    try:\n"
        "        if op == 'list_directory':\n"
        "            if not resolved.exists():\n"
        "                fail(f'Directory not found: {resource}', 'EXECUTION_FAILED')\n"
        "                return\n"
        "            if not resolved.is_dir():\n"
        "                fail(f'Resource is not a directory: {resource}', 'EXECUTION_FAILED')\n"
        "                return\n"
        "            items = []\n"
        "            for entry in sorted(os.listdir(resolved)):\n"
        "                items.append({'name': entry, 'type': 'directory' if (resolved / entry).is_dir() else 'file'})\n"
        "            print(json.dumps({'status': 'EXECUTED', 'result': {'operation': 'list_directory', 'resource': resource, 'item_count': len(items), 'items': items}, 'error_code': None}))\n"
        "            return\n"
        "        elif op == 'read_text':\n"
        "            if not resolved.exists():\n"
        "                fail(f'File not found: {resource}', 'EXECUTION_FAILED')\n"
        "                return\n"
        "            if resolved.is_dir():\n"
        "                fail(f'Resource is a directory, not a text file: {resource}', 'EXECUTION_FAILED')\n"
        "                return\n"
        "            size = resolved.stat().st_size\n"
        "            if size > MAX_READ:\n"
        "                print(json.dumps({'status': 'FAILED', 'result': {'error': f'File size {size} exceeds {MAX_READ} limit.'}, 'error_code': 'OVERSIZED_FILE'}))\n"
        "                return\n"
        "            content = resolved.read_text(encoding='utf-8', errors='replace')\n"
        "            preview = content[:500] if len(content) > 500 else content\n"
        "            print(json.dumps({'status': 'EXECUTED', 'result': {'operation': 'read_text', 'resource': resource, 'byte_count': size, 'preview': preview, 'truncated': len(content) > 500}, 'error_code': None}))\n"
        "            return\n"
        "        elif op == 'write_text':\n"
        "            content = args.get('content', '')\n"
        "            if not isinstance(content, str):\n"
        "                content = str(content)\n"
        "            payload = content.encode('utf-8')\n"
        "            if len(payload) > MAX_WRITE:\n"
        "                print(json.dumps({'status': 'FAILED', 'result': {'error': f'Payload {len(payload)} bytes exceeds {MAX_WRITE} limit.'}, 'error_code': 'OVERSIZED_PAYLOAD'}))\n"
        "                return\n"
        "            parent = resolved.parent\n"
        "            try:\n"
        "                parent.resolve().relative_to(WS.resolve())\n"
        "            except ValueError:\n"
        "                fail('path escapes workspace boundary', 'EXECUTION_FAILED')\n"
        "                return\n"
        "            parent.mkdir(parents=True, exist_ok=True)\n"
        "            resolved.write_text(content, encoding='utf-8')\n"
        "            print(json.dumps({'status': 'EXECUTED', 'result': {'operation': 'write_text', 'resource': resource, 'bytes_written': len(payload)}, 'error_code': None}))\n"
        "            return\n"
        "        elif op == 'delete_path':\n"
        "            print(json.dumps({'status': 'EXECUTED', 'result': {'operation': 'delete_path', 'resource': resource, 'simulated': True, 'note': 'Deletion simulated safely in baseline demo; target not unlinked.'}, 'error_code': None}))\n"
        "            return\n"
        "        elif op == 'run_command':\n"
        "            run_argv = args.get('argv')\n"
        "            if not isinstance(run_argv, list) or not run_argv or not all(isinstance(a, str) for a in run_argv):\n"
        "                fail('malformed argv for run_command', 'EXECUTION_FAILED')\n"
        "                return\n"
        "            if any('\\x00' in a for a in run_argv):\n"
        "                fail('null byte in argv', 'EXECUTION_FAILED')\n"
        "                return\n"
        "            try:\n"
        "                run_timeout = float(args.get('timeout_s', RUN_CMD_TIMEOUT_DEFAULT))\n"
        "            except (TypeError, ValueError):\n"
        "                run_timeout = RUN_CMD_TIMEOUT_DEFAULT\n"
        "            run_timeout = min(max(run_timeout, RUN_CMD_TIMEOUT_MIN), RUN_CMD_TIMEOUT_MAX)\n"
        "            if '..' in Path(resource).parts:\n"
        "                fail('cwd uses directory traversal', 'EXECUTION_FAILED')\n"
        "                return\n"
        "            try:\n"
        "                run_cwd = (WS if resource in ('', '.') else (WS / resource)).resolve()\n"
        "                run_cwd.relative_to(WS.resolve())\n"
        "            except ValueError:\n"
        "                fail('cwd escapes workspace boundary', 'EXECUTION_FAILED')\n"
        "                return\n"
        "            except Exception:\n"
        "                fail('cwd resolution failed', 'EXECUTION_FAILED')\n"
        "                return\n"
        "            if not run_cwd.is_dir():\n"
        "                fail(f'cwd not found: {resource}', 'EXECUTION_FAILED')\n"
        "                return\n"
        "            try:\n"
        "                run_proc = subprocess.run(run_argv, shell=False, capture_output=True, timeout=run_timeout, cwd=str(run_cwd))\n"
        "            except subprocess.TimeoutExpired:\n"
        "                print(json.dumps({'status': 'FAILED', 'result': {'operation': 'run_command', 'argv': run_argv, 'exit_code': None, 'stdout': '', 'stderr': '', 'truncated_stdout': False, 'truncated_stderr': False, 'timed_out': True}, 'error_code': 'COMMAND_TIMEOUT'}))\n"
        "                return\n"
        "            except FileNotFoundError:\n"
        "                fail('command not found', 'EXECUTION_FAILED')\n"
        "                return\n"
        "            except Exception as exc:\n"
        "                fail(type(exc).__name__, 'EXECUTION_FAILED')\n"
        "                return\n"
        "            run_out = run_proc.stdout or b''\n"
        "            run_err = run_proc.stderr or b''\n"
        "            run_out_trunc = len(run_out) > RUN_CMD_LIMIT\n"
        "            run_err_trunc = len(run_err) > RUN_CMD_LIMIT\n"
        "            run_out_text = run_out[:RUN_CMD_LIMIT].decode('utf-8', errors='replace')\n"
        "            run_err_text = run_err[:RUN_CMD_LIMIT].decode('utf-8', errors='replace')\n"
        "            if run_out_trunc:\n"
        "                run_out_text += f\"\\n...[truncated {len(run_out) - RUN_CMD_LIMIT} bytes]\\n\"\n"
        "            if run_err_trunc:\n"
        "                run_err_text += f\"\\n...[truncated {len(run_err) - RUN_CMD_LIMIT} bytes]\\n\"\n"
        "            run_status = 'EXECUTED' if run_proc.returncode == 0 else 'FAILED'\n"
        "            run_error = None if run_proc.returncode == 0 else 'NONZERO_EXIT'\n"
        "            print(json.dumps({'status': run_status, 'result': {'operation': 'run_command', 'argv': run_argv, 'exit_code': run_proc.returncode, 'stdout': run_out_text, 'stderr': run_err_text, 'truncated_stdout': run_out_trunc, 'truncated_stderr': run_err_trunc, 'timed_out': False}, 'error_code': run_error}))\n"
        "            return\n"
        "        else:\n"
        "            fail(f'Unsupported operation: {op}', 'EXECUTION_FAILED')\n"
        "            return\n"
        "    except Exception as exc:\n"
        "        fail(type(exc).__name__, 'EXECUTION_FAILED')\n"
        "        return\n"
        "main()\n"
    )


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


# Backwards-compatible alias used by tests.
def docker_available(timeout_s: float = 5.0) -> bool:
    return is_docker_available(timeout_s=timeout_s)


def build_docker_command(
    *,
    image: str,
    workspace_copy: Path,
    operation: str,
    resource: str,
    arguments_json: str,
    container_name: str,
    run_label: str = "docker-test",
) -> list[str]:
    """Build the hardened `docker run` command for one action.

    Separated for unit testing: asserts the exact hardening flags without
    requiring a Docker daemon.
    """
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--label",
        "scopewatch.executor=docker",
        "--label",
        f"scopewatch.run={run_label}",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp",
        "--user",
        DOCKER_USER,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        DOCKER_PIDS_LIMIT,
        "--memory",
        DOCKER_MEMORY,
        "--memory-swap",
        DOCKER_MEMORY,
        "--cpus",
        DOCKER_CPUS,
        "-v",
        f"{workspace_copy}:/workspace:rw",
        "--workdir",
        "/workspace",
        image,
        "python3",
        "-c",
        _helper_code(),
        operation,
        resource,
        arguments_json,
    ]


def _sync_copy_back(workspace_copy: Path, workspace_root: Path) -> None:
    """Copy container-modified files back to the host workspace."""
    shutil.copytree(workspace_copy, workspace_root, symlinks=True, dirs_exist_ok=True)


class DockerExecutor:
    """Execute actions inside a per-run hardened container."""

    def __init__(
        self,
        image: str = DOCKER_IMAGE,
        timeout_s: float = DOCKER_TIMEOUT_S,
    ) -> None:
        self.image = image
        self.timeout_s = timeout_s

    def execute(
        self,
        action: ActionRequest,
        workspace_root: Path,
        policy_decision: Optional[PolicyDecision] = None,
        approval_request: Optional[ApprovalRequest] = None,
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
        # Docker is touched. Mirrors LocalWorkspaceExecutor invariants.
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
            if (
                approval_request is None
                or approval_request.status
                not in (ApprovalStatus.APPROVED, ApprovalStatus.CONSUMED)
                or approval_request.action_request_id != action.id
            ):
                from scopewatch.executor import ExecutionSecurityError

                raise ExecutionSecurityError(
                    "Held action requires valid approved status to execute."
                )
        if action.operation == "network_request":
            from scopewatch.executor import ExecutionSecurityError

            raise ExecutionSecurityError("Network requests are forbidden in synthetic executor.")

        if not is_docker_available():
            return _failed("EXECUTION_FAILED", "Docker daemon unavailable; failing closed.")

        workspace_copy: Optional[Path] = None
        staging_root: Optional[Path] = None
        container_name = f"scopewatch-{uuid.uuid4().hex[:12]}"
        try:
            resolved_workspace = workspace_root.resolve()
            if not resolved_workspace.exists():
                return _failed("EXECUTION_FAILED", "Workspace root not found.")
            staging_root = Path(tempfile.mkdtemp(prefix="scopewatch-run-"))
            workspace_copy = staging_root / "workspace"
            shutil.copytree(resolved_workspace, workspace_copy, symlinks=True)
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
                arguments_json = json.dumps({"argv": run_argv, "timeout_s": run_timeout_s})
            else:
                arguments_json = json.dumps(action.arguments or {})
            cmd = build_docker_command(
                image=self.image,
                workspace_copy=workspace_copy,
                operation=action.operation,
                resource=action.resource,
                arguments_json=arguments_json,
                container_name=container_name,
                run_label=action.run_id,
            )
            try:
                proc = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=(
                        self.timeout_s
                        if run_timeout_s is None
                        else run_timeout_s + DOCKER_RUN_TIMEOUT_BUFFER_S
                    ),
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


def _best_effort_remove(container_name: str) -> None:
    try:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except Exception:
        pass


def execute_action_docker(
    action: ActionRequest,
    workspace_root: Path,
    policy_decision: Optional[PolicyDecision] = None,
    approval_request: Optional[ApprovalRequest] = None,
    image: str = DOCKER_IMAGE,
) -> ExecutionReceipt:
    """Convenience wrapper used by the gateway entry point and tests."""
    return DockerExecutor(image=image).execute(
        action,
        workspace_root,
        policy_decision=policy_decision,
        approval_request=approval_request,
    )

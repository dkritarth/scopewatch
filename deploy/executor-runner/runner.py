#!/usr/bin/env python3
"""Scopewatch executor-runner sidecar (issue #78).

The ONLY process that mounts the Docker socket (or talks to a rootless
daemon). The gateway runs with ``SCOPEWATCH_EXECUTOR=remote`` and calls
``POST /execute`` on the internal network with a pre-shared bearer token
plus a single-use dispatch token bound to one approved action digest.

Stdlib only (no third-party deps): ``http.server`` + ``json`` +
``subprocess``. Import-safe: unit tests import the validation helpers
without starting the server; ``python3 runner.py`` serves.

Request contract (gateway: ``backend/scopewatch/executor_remote.py``)::

    POST /execute
    Authorization: Bearer <EXECUTOR_RUNNER_TOKEN>
    {
      "dispatch_token": "<single-use random token>",
      "action_digest": "<sha256 over canonical action + decision>",
      "action": {"id","run_id","operation","resource","arguments"},
      "policy_decision": {"id","outcome"},
      "approval": {"id","action_request_id","status"} | null
    }

Validation order (fail closed, static messages, no secret in output):

1. Bearer token via ``hmac.compare_digest`` (401 otherwise).
2. Dispatch token present, unseen, unexpired; recorded one-shot with a 60s
   TTL. Replays and expired tokens are refused (409).
3. Action digest recomputed with the same canonicalization as the gateway;
   mismatches refused (409) so a token minted for one action cannot run
   another.
4. Operation allowlisted; resource relative with no null bytes, no absolute
   paths, no ``..`` escape; resolved target must stay inside ``/workspace``
   (symlink escapes refused). ``network_request`` is never executed.
5. Docker dispatch with the same hardening flags as the gateway Docker
   backend (no network, read-only rootfs, nobody user, cap-drop, pids/mem
   caps, workspace-only mount). Any Docker error, timeout, or malformed
   helper output becomes a ``FAILED`` payload (HTTP 200 with
   ``status: FAILED``) or a 502/500 with a static error — never a traceback.

Digest canonicalization MUST match
``backend/scopewatch/executor_remote.py::compute_action_digest`` exactly.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Optional

# Mirror of the gateway Docker backend pin (backend/scopewatch/executor_docker.py).
RUNNER_DOCKER_IMAGE = (
    "python:3.12-slim-bookworm@"
    "sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254"
)
CONTAINER_WORKSPACE = "/workspace"
DOCKER_USER = "65534:65534"
DOCKER_MEMORY = "256m"
DOCKER_CPUS = "1.0"
DOCKER_PIDS_LIMIT = "64"
DOCKER_TIMEOUT_S = 60.0
DOCKER_RUN_TIMEOUT_BUFFER_S = 30.0
RUN_COMMAND_TIMEOUT_MIN_S = 1.0
RUN_COMMAND_TIMEOUT_MAX_S = 300.0
RUN_COMMAND_DEFAULT_TIMEOUT_S = 60.0
RUN_COMMAND_OUTPUT_LIMIT = 64 * 1024
MAX_READ_BYTES = 256 * 1024
MAX_WRITE_BYTES = 64 * 1024

# Single-use dispatch tokens expire after 60s (issue #78).
DISPATCH_TOKEN_TTL_S = 60.0
# Cap request bodies (actions are small synthetic ops).
MAX_BODY_BYTES = 256 * 1024

ALLOWED_OPERATIONS = frozenset(
    {"list_directory", "read_text", "write_text", "delete_path", "run_command"}
)


def canonical_action_digest(
    action: dict[str, Any],
    policy_decision: dict[str, Any],
) -> str:
    """Recompute the gateway action digest (must match exactly)."""
    canonical = {
        "action_id": action.get("id"),
        "run_id": action.get("run_id"),
        "operation": action.get("operation"),
        "resource": action.get("resource"),
        "arguments": action.get("arguments") or {},
        "policy_decision_id": policy_decision.get("id"),
        "policy_outcome": policy_decision.get("outcome"),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class DispatchTokenStore:
    """One-shot dispatch tokens with TTL (thread-safe, in-memory)."""

    def __init__(self, ttl_s: float = DISPATCH_TOKEN_TTL_S) -> None:
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._seen: dict[str, tuple[str, float]] = {}

    def _prune_locked(self, now: float) -> None:
        expired = [t for t, (_, exp) in self._seen.items() if exp <= now]
        for token in expired:
            del self._seen[token]

    def consume(self, token: object, digest: object, now: Optional[float] = None) -> str:
        """Record one use; returns "ok", "reused", or "invalid"."""
        if not isinstance(token, str) or not token or len(token) > 256:
            return "invalid"
        if not isinstance(digest, str) or not digest:
            return "invalid"
        at = time.time() if now is None else now
        with self._lock:
            self._prune_locked(at)
            if token in self._seen:
                return "reused"
            self._seen[token] = (digest, at + self.ttl_s)
            return "ok"

    def binding_for(self, token: str) -> Optional[str]:
        """Return the digest a live token was bound to (test helper)."""
        with self._lock:
            entry = self._seen.get(token)
            if entry is None:
                return None
            digest, expires = entry
            if expires <= time.time():
                return None
            return digest


def check_bearer(authorization: Optional[str], expected_token: str) -> bool:
    """Constant-time bearer check; False on missing/malformed/empty secret."""
    if not authorization or not expected_token:
        return False
    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credential:
        return False
    return hmac.compare_digest(credential.strip(), expected_token)


def validate_resource_inside_workspace(
    resource: object,
    workspace_root: Path,
) -> tuple[Optional[Path], Optional[str]]:
    """Resolve ``resource`` inside the workspace (no following escapes).

    Returns (resolved_target, None) on success or (None, static_reason).
    Null bytes, absolute paths, and ``..`` escapes are refused; a resolved
    target escaping the workspace (including via symlink) is refused.
    """
    if not isinstance(resource, str) or not resource:
        return None, "invalid resource"
    if "\x00" in resource:
        return None, "invalid resource"
    if resource.startswith("/") or resource.startswith("\\"):
        return None, "absolute paths are prohibited"
    if len(resource) > 1 and len(resource) > 2 and resource[1] == ":":
        return None, "absolute paths are prohibited"
    parts = Path(resource).parts
    if ".." in parts:
        return None, "path escapes workspace boundary"
    try:
        resolved = (workspace_root / resource).resolve()
        resolved.relative_to(workspace_root.resolve())
    except ValueError:
        return None, "path escapes workspace boundary"
    except Exception:
        return None, "path resolution failed"
    return resolved, None


def normalize_run_command(
    arguments: object,
) -> tuple[Optional[list[str]], float, Optional[str]]:
    """Normalize run_command argv/timeout (no shell); (argv, timeout, error)."""
    if not isinstance(arguments, dict):
        return None, RUN_COMMAND_DEFAULT_TIMEOUT_S, "malformed argv for run_command"
    argv = arguments.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) for item in argv)
    ):
        return None, RUN_COMMAND_DEFAULT_TIMEOUT_S, "malformed argv for run_command"
    if any("\x00" in item for item in argv):
        return None, RUN_COMMAND_DEFAULT_TIMEOUT_S, "null byte in argv"
    try:
        timeout = float(arguments.get("timeout_s", RUN_COMMAND_DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        timeout = RUN_COMMAND_DEFAULT_TIMEOUT_S
    if timeout != timeout or timeout in (float("inf"), float("-inf")):  # NaN/inf
        timeout = RUN_COMMAND_DEFAULT_TIMEOUT_S
    timeout = min(max(timeout, RUN_COMMAND_TIMEOUT_MIN_S), RUN_COMMAND_TIMEOUT_MAX_S)
    return list(argv), timeout, None


def _helper_code() -> str:
    """In-container helper (same semantics/limits as the gateway backend)."""
    return (
        "import json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        f"MAX_READ = {int(MAX_READ_BYTES)}\n"
        f"MAX_WRITE = {int(MAX_WRITE_BYTES)}\n"
        f"RUN_CMD_LIMIT = {int(RUN_COMMAND_OUTPUT_LIMIT)}\n"
        f"RUN_CMD_TIMEOUT_DEFAULT = {float(RUN_COMMAND_DEFAULT_TIMEOUT_S)}\n"
        f"RUN_CMD_TIMEOUT_MIN = {float(RUN_COMMAND_TIMEOUT_MIN_S)}\n"
        f"RUN_CMD_TIMEOUT_MAX = {float(RUN_COMMAND_TIMEOUT_MAX_S)}\n"
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
        "    if resource.startswith('/') or resource.startswith('\\\\'):\n"
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
        "            print(json.dumps({'status': 'EXECUTED', 'result': {'operation': 'delete_path', 'resource': resource, 'simulated': True, 'note': 'Deletion simulated safely; target not unlinked.'}, 'error_code': None}))\n"
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


def build_runner_docker_command(
    *,
    workspace_copy: Path,
    operation: str,
    resource: str,
    arguments_json: str,
    container_name: str,
    run_label: str,
    image: Optional[str] = None,
) -> list[str]:
    """Hardened ``docker run`` (same flags as the gateway Docker backend)."""
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--label",
        "scopewatch.executor=runner",
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
        f"{workspace_copy}:{CONTAINER_WORKSPACE}:rw",
        "--workdir",
        CONTAINER_WORKSPACE,
        image or RUNNER_DOCKER_IMAGE,
        "python3",
        "-c",
        _helper_code(),
        operation,
        resource,
        arguments_json,
    ]


def sync_copy_back(workspace_copy: Path, workspace_root: Path) -> None:
    """Symlink-tolerant copy-back (mirrors the gateway Docker backend)."""
    staged = Path(workspace_copy)
    dest_root = Path(workspace_root)
    for dirpath, dirnames, filenames in os.walk(staged, followlinks=False):
        staged_dir = Path(dirpath)
        rel = staged_dir.relative_to(staged)
        dest_dir = dest_root / rel if str(rel) != "." else dest_root
        dest_dir.mkdir(parents=True, exist_ok=True)
        for name in list(dirnames):
            src_entry = staged_dir / name
            if src_entry.is_symlink():
                _replace_link(src_entry, dest_dir / name)
            else:
                (dest_dir / name).mkdir(parents=True, exist_ok=True)
        for name in filenames:
            src_entry = staged_dir / name
            dest_entry = dest_dir / name
            if src_entry.is_symlink():
                _replace_link(src_entry, dest_entry)
            elif src_entry.is_file():
                if dest_entry.is_symlink():
                    dest_entry.unlink()
                elif dest_entry.is_dir() and not dest_entry.is_symlink():
                    shutil.rmtree(dest_entry)
                shutil.copy2(src_entry, dest_entry)


def _replace_link(src_link: Path, dest: Path) -> None:
    if os.path.lexists(dest):
        if dest.is_dir() and not dest.is_symlink():
            shutil.rmtree(dest)
        else:
            dest.unlink()
    dest.symlink_to(os.readlink(src_link))


def _make_world_accessible(root: Path) -> None:
    os.chmod(root, os.stat(root).st_mode | stat.S_IRWXO)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames:
            entry = Path(dirpath) / name
            if entry.is_symlink():
                continue
            os.chmod(entry, os.stat(entry).st_mode | stat.S_IRWXO)
        for name in filenames:
            entry = Path(dirpath) / name
            if entry.is_symlink():
                continue
            os.chmod(entry, os.stat(entry).st_mode | stat.S_IROTH | stat.S_IWOTH)


class RunnerConfig:
    """Runtime config, environment only (never from agent input)."""

    def __init__(self) -> None:
        self.token = os.environ.get("EXECUTOR_RUNNER_TOKEN", "")
        self.host = os.environ.get("EXECUTOR_RUNNER_HOST", "127.0.0.1")
        self.port = int(os.environ.get("EXECUTOR_RUNNER_PORT", "8091"))
        self.workspace = Path(os.environ.get("EXECUTOR_RUNNER_WORKSPACE", "/workspace"))
        self.image = os.environ.get("EXECUTOR_RUNNER_IMAGE", RUNNER_DOCKER_IMAGE)


TOKEN_STORE = DispatchTokenStore()


def handle_execute(
    body: dict[str, Any],
    config: RunnerConfig,
    token_store: DispatchTokenStore = TOKEN_STORE,
    now: Optional[float] = None,
) -> tuple[int, dict[str, Any]]:
    """Pure dispatch validation + Docker execution (testable without HTTP).

    Returns (http_status, payload). Refusals use static messages; Docker and
    helper failures fail closed.
    """
    action = body.get("action")
    decision = body.get("policy_decision")
    if not isinstance(action, dict) or not isinstance(decision, dict):
        return 400, {"error": "malformed dispatch"}
    outcome = token_store.consume(body.get("dispatch_token"), body.get("action_digest"), now=now)
    if outcome == "reused":
        return 409, {"error": "dispatch token already used"}
    if outcome == "invalid":
        return 400, {"error": "malformed dispatch"}
    expected_digest = canonical_action_digest(action, decision)
    if not hmac.compare_digest(str(body.get("action_digest") or ""), expected_digest):
        return 409, {"error": "action digest mismatch"}

    operation = action.get("operation")
    resource = action.get("resource")
    run_id = action.get("run_id") or "unknown-run"
    if operation not in ALLOWED_OPERATIONS:
        return 400, {"error": "unsupported operation"}
    if operation == "network_request":
        return 403, {"error": "operation forbidden"}
    if not isinstance(resource, str):
        return 400, {"error": "invalid resource"}

    workspace = config.workspace
    if not workspace.exists():
        return 500, {"error": "workspace unavailable"}
    target, reason = validate_resource_inside_workspace(resource, workspace)
    if target is None:
        code = 403 if "boundary" in (reason or "") else 400
        return code, {"error": reason or "invalid resource"}

    arguments = action.get("arguments") or {}
    if not isinstance(arguments, dict):
        return 400, {"error": "malformed arguments"}
    run_timeout = DOCKER_TIMEOUT_S
    if operation == "run_command":
        argv, timeout, error = normalize_run_command(arguments)
        if error is not None or argv is None:
            return 400, {"error": error or "malformed argv for run_command"}
        arguments = {"argv": argv, "timeout_s": timeout}
        run_timeout = timeout + DOCKER_RUN_TIMEOUT_BUFFER_S

    staging_root: Optional[Path] = None
    container_name = f"scopewatch-runner-{uuid.uuid4().hex[:12]}"
    try:
        staging_root = Path(tempfile.mkdtemp(prefix="scopewatch-runner-"))
        workspace_copy = staging_root / "workspace"
        try:
            shutil.copytree(workspace, workspace_copy, symlinks=True)
            _make_world_accessible(workspace_copy)
        except OSError:
            return 500, {"error": "workspace staging failed"}
        cmd = build_runner_docker_command(
            workspace_copy=workspace_copy,
            operation=str(operation),
            resource=resource,
            arguments_json=json.dumps(arguments),
            container_name=container_name,
            run_label=str(run_id),
            image=config.image,
        )
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=run_timeout,
            )
        except subprocess.TimeoutExpired:
            _best_effort_remove(container_name)
            return 200, {"status": "FAILED", "result": {"error": "Docker execution timed out."}, "error_code": "EXECUTION_FAILED"}
        except FileNotFoundError:
            return 502, {"error": "Docker daemon unavailable"}
        except Exception:
            _best_effort_remove(container_name)
            return 502, {"error": "Docker execution failed"}
        if proc.returncode != 0:
            return 200, {"status": "FAILED", "result": {"error": "Docker execution failed."}, "error_code": "EXECUTION_FAILED"}
        try:
            payload = json.loads(proc.stdout.decode("utf-8", errors="replace"))
        except Exception:
            return 502, {"error": "Docker helper returned malformed output"}
        status = payload.get("status")
        result = payload.get("result") or {}
        error_code = payload.get("error_code")
        if status not in ("EXECUTED", "FAILED") or not isinstance(result, dict):
            return 502, {"error": "Docker helper returned malformed output"}
        if status == "EXECUTED" and operation in ("write_text", "run_command"):
            try:
                sync_copy_back(workspace_copy, workspace)
            except Exception:
                return 200, {"status": "FAILED", "result": {"error": "Docker execution failed."}, "error_code": "EXECUTION_FAILED"}
        return 200, {"status": status, "result": result, "error_code": error_code}
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


class _Handler(BaseHTTPRequestHandler):
    config = RunnerConfig()
    token_store = TOKEN_STORE

    def log_message(self, fmt: str, *args: object) -> None:
        # Structured minimal access log without secrets or bodies.
        sys_msg = fmt % args
        if "Authorization" in sys_msg or "Bearer" in sys_msg:
            sys_msg = "[redacted auth log]"
        print(f"executor-runner {self.command} {self.path} -> {sys_msg}", flush=True)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path.rstrip("/") in ("", "/healthz", "/health"):
            self._send(200, {"status": "ok"})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/execute":
            self._send(404, {"error": "not found"})
            return
        if not check_bearer(self.headers.get("Authorization"), self.config.token):
            self._send(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send(400, {"error": "malformed dispatch"})
            return
        try:
            raw = self.rfile.read(length)
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send(400, {"error": "malformed dispatch"})
            return
        if not isinstance(body, dict):
            self._send(400, {"error": "malformed dispatch"})
            return
        status, payload = handle_execute(body, self.config, self.token_store)
        self._send(status, payload)


def serve(config: Optional[RunnerConfig] = None) -> None:
    """Bind the internal-only HTTP API (defaults to loopback)."""
    cfg = config or RunnerConfig()
    if not cfg.token:
        raise SystemExit("EXECUTOR_RUNNER_TOKEN is required (environment only).")
    _Handler.config = cfg
    server = HTTPServer((cfg.host, cfg.port), _Handler)
    print(f"executor-runner listening on {cfg.host}:{cfg.port} (internal only)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    serve()

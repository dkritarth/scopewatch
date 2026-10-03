"""Shared Docker job construction for the gateway and the runner (issue #106).

One source for everything that describes *what a container is asked to do*:
the pinned image, the hardening constants, the in-container helper source,
the ``docker run`` flag list, and the staged-workspace copy plus the
symlink-tolerant copy-back walk. Both callers import from here, so they
cannot drift:

- the gateway: ``scopewatch.executor_docker.DockerExecutor``
- the sidecar:  ``deploy/executor-runner/runner.py`` (``handle_execute``)

#104 fixed a defect caused by having two copies: the gateway digested
``run_command`` arguments before normalizing them while dispatching the
normalized form, so every remote ``run_command`` failed with HTTP 409. The
same class of drift is what this module removes.

Stdlib-only is a hard constraint, not a preference
---------------------------------------------------------
The runner is a stdlib-only sidecar and imports this file directly. Adding an
import of ``scopewatch.*`` (anything but this module), of a third-party
package, or of anything that opens a socket would break the sidecar image and
would make the runner un-auditable as a standalone artifact. The runner image
build fails fast on a non-stdlib import (see
``deploy/executor-runner/Dockerfile``), and
``backend/tests/test_docker_job_share.py`` asserts the same constraint from
the test suite.

``DockerJob`` follows the ``prepare_dispatch`` model from #104
---------------------------------------------------------
One call builds the payload *and* its canonical serialization, so a caller
cannot send one form and dispatch another. ``prepare_docker_job`` returns a
frozen :class:`DockerJob`; ``job.arguments_json`` is the exact JSON string
handed to the container, and ``job.helper_argv`` is the exact argv tail. Both
are derived from ``job.arguments`` in this module and nowhere else, and
:meth:`DockerJob.host_timeout_s` derives the host-side wait from the same
``timeout_s`` the container receives. That is the same invariant
``executor_remote.prepare_dispatch`` protects for the action digest.

What deliberately does NOT live here
---------------------------------------------------------
- **Pre-dispatch gates** (stored decision, ``DENY``/``HOLD``, approval status
  and binding, scope revalidation, ``network_request`` refusal). Those are
  rules, not job construction, and they are issue #105's shared gate module.
- **Transport and identity**: the bearer token, the HMAC dispatch signature,
  the single-use dispatch-token store, and the HTTP socket stay in
  ``runner.py``. The runner is the only process that holds the Docker socket.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

# Pinned base image digest (Debian bookworm slim Python). Single pin for both
# sides; the gateway and the runner previously repeated this string verbatim,
# so a base-image bump could land on one side only.
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
# Host waits this much longer than the container's own run_command timeout, so
# an in-container timeout surfaces as a clean FAILED receipt instead of a host
# -side kill with no evidence.
DOCKER_RUN_TIMEOUT_BUFFER_S = 30.0

# run_command limits (issue #36): default per-command timeout, host-side clamp
# bounds, and per-stream output cap with a truncation marker.
RUN_COMMAND_DEFAULT_TIMEOUT_S = 60.0
RUN_COMMAND_MIN_TIMEOUT_S = 1.0
RUN_COMMAND_MAX_TIMEOUT_S = 300.0
RUN_COMMAND_OUTPUT_LIMIT = 64 * 1024

# Container output caps. These must stay equal to the gateway config values in
# ``backend/scopewatch/config.py``; they are restated here because the runner
# cannot import gateway config, and the cross-test pins them together.
MAX_READ_BYTES = 256 * 1024  # 256 KiB
MAX_WRITE_BYTES = 64 * 1024  # 64 KiB


def clamp_run_command_timeout(value: Any) -> float:
    """Clamp a requested per-command timeout to the allowed range.

    A non-numeric, NaN, or infinite request falls back to the default rather
    than failing: the caller has already decided to run the command, and the
    container re-clamps to the same range anyway.
    """
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return RUN_COMMAND_DEFAULT_TIMEOUT_S
    if math.isnan(timeout) or math.isinf(timeout):
        return RUN_COMMAND_DEFAULT_TIMEOUT_S
    return min(max(timeout, RUN_COMMAND_MIN_TIMEOUT_S), RUN_COMMAND_MAX_TIMEOUT_S)


@dataclass(frozen=True)
class DockerJob:
    """One container job: the operation, its target, and its arguments.

    ``arguments_json`` is stored rather than computed on demand so the exact
    bytes the container parses are fixed at construction time, next to the
    arguments they were built from. ``__post_init__`` re-derives it and
    refuses a mismatch, which makes "the digest describes a different payload"
    (the #104 failure mode) unrepresentable rather than merely discouraged.

    ``timeout_s`` is the in-container wall clock for ``run_command`` and is
    ``None`` for every other operation, which has no command to time out.
    """

    operation: str
    resource: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    arguments_json: str = ""
    timeout_s: Optional[float] = None

    def __post_init__(self) -> None:
        expected = json.dumps(dict(self.arguments))
        if not self.arguments_json:
            object.__setattr__(self, "arguments_json", expected)
        elif self.arguments_json != expected:
            raise ValueError(
                "DockerJob arguments and arguments_json disagree; build jobs "
                "with prepare_docker_job."
            )

    @property
    def helper_argv(self) -> tuple[str, str, str]:
        """The argv tail handed to the in-container helper."""
        return (self.operation, self.resource, self.arguments_json)

    def host_timeout_s(self, default_s: float) -> float:
        """Wall clock the host waits for this job's ``docker run``.

        ``run_command`` adds :data:`DOCKER_RUN_TIMEOUT_BUFFER_S` so the
        container gets to report its own timeout first. Every other operation
        uses ``default_s`` unchanged, which is what the gateway's
        ``DockerExecutor.timeout_s`` and the runner's
        :data:`DOCKER_TIMEOUT_S` mean for non-command work.
        """
        if self.timeout_s is None:
            return default_s
        return self.timeout_s + DOCKER_RUN_TIMEOUT_BUFFER_S


def prepare_docker_job(
    *,
    operation: str,
    resource: str,
    arguments: Optional[Mapping[str, Any]] = None,
    timeout_s: Optional[float] = None,
) -> DockerJob:
    """Build one :class:`DockerJob` with its canonical serialization.

    The single entry point for job construction on both sides. Callers pass
    already-normalized ``arguments`` (the argv-list form for ``run_command``)
    and the matching ``timeout_s``; this function is where those become the
    bytes and the argv the container receives, so the two cannot disagree.

    Deriving ``argv`` from an action's arguments stays with the side that
    knows its input shape: the gateway accepts both a ``command`` string and a
    pre-split ``argv`` list, while the runner only ever receives the
    pre-split list a conforming gateway already normalized.
    """
    return DockerJob(
        operation=operation,
        resource=resource,
        arguments=dict(arguments or {}),
        timeout_s=timeout_s,
    )


def helper_code() -> str:
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


def build_docker_command(
    *,
    image: str,
    workspace_copy: Path,
    job: DockerJob,
    container_name: str,
    run_label: str,
    executor_label: str = "docker",
) -> list[str]:
    """Build the hardened ``docker run`` command for one job.

    Separated from the callers for two reasons: the exact hardening flags can
    be asserted without a Docker daemon, and both sides produce a
    byte-identical argv. ``executor_label`` is the only intentional
    difference between them (``"docker"`` for the gateway, ``"runner"`` for
    the sidecar), alongside the caller-supplied image, container name, and run
    label.
    """
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--label",
        f"scopewatch.executor={executor_label}",
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
        image,
        "python3",
        "-c",
        helper_code(),
        *job.helper_argv,
    ]


def replace_link(src_link: Path, dest: Path) -> None:
    """Replicate ``src_link`` at ``dest``, replacing without following.

    Uses ``lexists`` semantics so a dangling destination link is still
    detected and replaced. A destination directory that is *not* a link is
    removed first; anything else (file, link, dangling link) is unlinked
    before the new link is created.
    """
    if os.path.lexists(dest):
        if dest.is_dir() and not dest.is_symlink():
            shutil.rmtree(dest)
        else:
            dest.unlink()
    dest.symlink_to(os.readlink(src_link))


def sync_copy_back(workspace_copy: Path, workspace_root: Path) -> None:
    """Copy container-modified files back to the host workspace.

    Symlink-tolerant (issue #80): the previous
    ``shutil.copytree(..., symlinks=True, dirs_exist_ok=True)`` raised
    ``FileExistsError``/``shutil.Error`` when the staged copy contained a
    symlink that already existed in the destination, turning a legitimate
    container ``EXECUTED`` into a host-side ``FAILED``. Instead, walk the
    staged tree entry by entry: missing directories are created, existing
    symlinks are atomically replaced (never followed), and regular files are
    overwritten with ``copy2``. Files present in the destination but absent
    from the staged copy are left alone (same as ``dirs_exist_ok``:
    container deletes are not propagated). Special files (sockets, fifos,
    devices) are skipped rather than copied.
    """
    staged = Path(workspace_copy)
    dest_root = Path(workspace_root)
    for dirpath, dirnames, filenames in os.walk(staged, followlinks=False):
        staged_dir = Path(dirpath)
        rel = staged_dir.relative_to(staged)
        dest_dir = dest_root / rel if str(rel) != "." else dest_root
        dest_dir.mkdir(parents=True, exist_ok=True)
        for name in list(dirnames):
            src_entry = staged_dir / name
            # Symlink-to-dir appears in dirnames when followlinks=False;
            # replicate the link itself and never descend into it (the walk
            # already refuses to follow, so no pruning is needed).
            if src_entry.is_symlink():
                replace_link(src_entry, dest_dir / name)
            else:
                (dest_dir / name).mkdir(parents=True, exist_ok=True)
        for name in filenames:
            src_entry = staged_dir / name
            dest_entry = dest_dir / name
            if src_entry.is_symlink():
                replace_link(src_entry, dest_entry)
            elif src_entry.is_file():
                # Remove a conflicting destination link/dir first so a type
                # change (link -> file) copies cleanly without following the
                # old link onto its target.
                if dest_entry.is_symlink():
                    dest_entry.unlink()
                elif dest_entry.is_dir() and not dest_entry.is_symlink():
                    shutil.rmtree(dest_entry)
                shutil.copy2(src_entry, dest_entry)
            # Else: socket/fifo/device or vanished mid-walk; skip (fail
            # closed on the file, not on the whole receipt).


def make_world_accessible(root: Path) -> None:
    """Grant other-read/write (dirs: +x) across a staged workspace copy.

    The container runs as non-root nobody (65534), while the staged copy is
    owned by the host user. Without world bits, container writes fail with
    permission errors even though the mount is read-write. Only the staging
    copy is touched; the real workspace keeps its own permissions. Symlinks
    are skipped so chmod never follows a link onto a host target.
    """
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
            os.chmod(
                entry,
                os.stat(entry).st_mode | stat.S_IROTH | stat.S_IWOTH,
            )


def stage_workspace_copy(workspace_root: Path) -> tuple[Path, Path]:
    """Copy the workspace to a temp staging dir and open it to the container.

    Returns ``(staging_root, workspace_copy)``; the caller owns cleanup of
    ``staging_root``. Cleans up after itself and re-raises on copy/chmod
    failure, so a failed staging never leaves a world-writable tree behind.
    """
    staging_root = Path(tempfile.mkdtemp(prefix="scopewatch-run-"))
    try:
        workspace_copy = staging_root / "workspace"
        shutil.copytree(workspace_root, workspace_copy, symlinks=True)
        make_world_accessible(workspace_copy)
    except Exception:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise
    return staging_root, workspace_copy


def best_effort_remove(container_name: str) -> None:
    """Remove one container without ever raising (cleanup path only).

    Called from ``finally`` blocks and after a timeout, where raising would
    mask the real receipt.
    """
    try:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except Exception:
        pass
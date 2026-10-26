"""Tests for issue #195: executor list_directory, stdout, and copy-back size bounds.

Acceptance criteria:
- list_directory returns capped item_count with truthful truncated: true in both
  the local executor and docker_job helper.
- Host container stdout reading is bounded with process kill and cleanup on overrun.
- Staged workspace copy-back is refused before effect when size exceeds limit,
  leaving target workspace untouched (no partial writes).
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Optional
import uuid

import pytest

from scopewatch import config, docker_job
from scopewatch.docker_job import (
    MAX_CONTAINER_OUTPUT_BYTES,
    MAX_COPY_BACK_BYTES,
    MAX_DIR_ENTRIES,
    OutputOverrunError,
    OversizedCopyBackError,
    check_staged_workspace_size,
    helper_code,
    prepare_docker_job,
    run_docker_process,
    sync_copy_back,
)
from scopewatch.executor import execute_action
from scopewatch.executor_docker import DockerExecutor
from scopewatch.models import ExecutionStatus, PolicyOutcome, ReasonCode
from scopewatch.schemas import ActionRequest, PolicyDecision


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _allow_decision(action_id: str = "action-1") -> PolicyDecision:
    return PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id=action_id,
        outcome=PolicyOutcome.ALLOW,
        reason_code=ReasonCode.ALLOWED_TOOL_AND_RESOURCE,
        explanation="Permitted",
        matched_rule="RULE_ALLOWED",
        decided_at=_now(),
        deterministic=True,
    )


def _action(operation: str, resource: str, **kwargs: object) -> ActionRequest:
    return ActionRequest(
        id=str(kwargs.get("action_id", "action-1")),
        run_id="run-1",
        tool="workspace",
        operation=operation,
        resource=resource,
        arguments=kwargs.get("arguments", {}),  # type: ignore[arg-type]
        requested_at=_now(),
    )


# =============================================================================
# 1. list_directory size bounds & truthful truncation flag
# =============================================================================


def test_list_directory_local_under_limit(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "docs").mkdir()
    (ws / "docs" / "b.txt").write_text("b")
    (ws / "docs" / "a.txt").write_text("a")

    action = _action("list_directory", "docs")
    decision = _allow_decision(action.id)
    receipt = execute_action(action, ws, policy_decision=decision)

    assert receipt.status == ExecutionStatus.EXECUTED
    assert receipt.sanitized_result is not None
    assert receipt.sanitized_result["item_count"] == 2
    assert receipt.sanitized_result["truncated"] is False
    assert [item["name"] for item in receipt.sanitized_result["items"]] == ["a.txt", "b.txt"]


def test_list_directory_local_truncation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When directory entries exceed MAX_DIR_ENTRIES, item_count is capped and truncated is True."""
    monkeypatch.setattr(config, "MAX_DIR_ENTRIES", 5)
    # Also patch in executor if imported directly
    import scopewatch.executor as executor_mod

    monkeypatch.setattr(executor_mod, "MAX_DIR_ENTRIES", 5)

    ws = tmp_path / "workspace"
    docs = ws / "docs"
    docs.mkdir(parents=True)
    for i in range(8):
        (docs / f"file_{i:02d}.txt").write_text(f"content {i}")

    action = _action("list_directory", "docs")
    decision = _allow_decision(action.id)
    receipt = execute_action(action, ws, policy_decision=decision)

    assert receipt.status == ExecutionStatus.EXECUTED
    assert receipt.sanitized_result is not None
    assert receipt.sanitized_result["item_count"] == 5
    assert len(receipt.sanitized_result["items"]) == 5
    assert receipt.sanitized_result["truncated"] is True
    # The returned capped entries are sorted
    names = [item["name"] for item in receipt.sanitized_result["items"]]
    assert len(names) == 5
    assert names == sorted(names)


def test_list_directory_docker_helper_truncation(tmp_path: Path) -> None:
    """Direct invocation of helper_code() against a directory with > MAX_DIR_ENTRIES."""
    ws = tmp_path / "ws"
    ws.mkdir()
    for i in range(MAX_DIR_ENTRIES + 10):
        (ws / f"item_{i:04d}.txt").touch()

    # Run the in-container helper directly using python3
    helper_src = helper_code()
    env = {**os.environ, "SCOPEWATCH_WORKSPACE": str(ws)}
    proc = subprocess.run(
        [sys.executable, "-c", helper_src, "list_directory", ".", "{}"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, f"Helper failed: {proc.stderr}"
    payload = json.loads(proc.stdout)
    assert payload["status"] == "EXECUTED"
    result = payload["result"]
    assert result["operation"] == "list_directory"
    assert result["item_count"] == MAX_DIR_ENTRIES
    assert len(result["items"]) == MAX_DIR_ENTRIES
    assert result["truncated"] is True


# =============================================================================
# 2. Bounded container stdout reading & overrun handling
# =============================================================================


def test_run_docker_process_bounded_output_success() -> None:
    """Normal output within limit completes successfully."""
    cmd = [sys.executable, "-c", "import sys; sys.stdout.write('hello'); sys.stderr.write('world')"]
    proc = run_docker_process(cmd, timeout=5.0, max_bytes=1024)
    assert proc.returncode == 0
    assert proc.stdout == b"hello"
    assert proc.stderr == b"world"


def test_run_docker_process_stdout_overrun_kills_child() -> None:
    """A process exceeding max_output_bytes is terminated and raises OutputOverrunError."""
    # Process attempts to write 100 KB
    cmd = [sys.executable, "-c", "import sys; sys.stdout.write('A' * 100_000); sys.stdout.flush()"]
    with pytest.raises(OutputOverrunError, match="exceeded"):
        run_docker_process(cmd, timeout=5.0, max_bytes=1000)


def test_run_docker_process_infinite_stream_bounded_memory() -> None:
    """An infinite output generator is terminated immediately without unbounded memory buffering."""
    cmd = [sys.executable, "-c", "import sys\nwhile True:\n    sys.stdout.write('X' * 4096)\n    sys.stdout.flush()\n"]
    with pytest.raises(OutputOverrunError):
        run_docker_process(cmd, timeout=5.0, max_bytes=16384)


def test_run_docker_process_timeout_expired() -> None:
    """A hanging process is killed when the timeout expires."""
    cmd = [sys.executable, "-c", "import time\ntime.sleep(10.0)\n"]
    with pytest.raises(subprocess.TimeoutExpired):
        run_docker_process(cmd, timeout=0.2)


def test_run_docker_process_never_invokes_unbounded_subprocess_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: run_docker_process must never fall back to unbounded subprocess.run.

    Ensures production execution does not branch to subprocess.run(stdout=PIPE, stderr=PIPE)
    which would buffer the entire stream before checking bounds.
    """
    def _forbidden_run(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Unbounded subprocess.run invoked! Must use bounded Popen streaming.")

    monkeypatch.setattr(subprocess, "run", _forbidden_run)

    # 1. Finite output within bound succeeds without calling subprocess.run
    cmd_ok = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.write('A' * 2048); sys.stderr.write('B' * 512)",
    ]
    proc = run_docker_process(cmd_ok, timeout=5.0, max_bytes=4096)
    assert proc.returncode == 0
    assert proc.stdout == b"A" * 2048
    assert proc.stderr == b"B" * 512

    # 2. Finite output exceeding bound raises OutputOverrunError without calling subprocess.run
    cmd_overrun = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.write('A' * 8192); sys.stdout.flush()",
    ]
    with pytest.raises(OutputOverrunError, match="exceeded"):
        run_docker_process(cmd_overrun, timeout=5.0, max_bytes=1024)


def test_run_docker_process_finite_child_overrun_and_process_cleanup() -> None:
    """Exercise bounded Popen streaming semantics with finite synthetic child output.

    Spawns a real child process that produces output in chunks and then sleeps.
    Verifies that as soon as max_bytes is crossed, OutputOverrunError is raised and
    the child process (and its process tree) is promptly killed.
    """
    cmd = [
        sys.executable,
        "-c",
        (
            "import sys, time\n"
            "for _ in range(50):\n"
            "    sys.stdout.write('Z' * 1024)\n"
            "    sys.stdout.flush()\n"
            "    time.sleep(0.01)\n"
            "time.sleep(10.0)\n"
        ),
    ]
    start = time.monotonic()
    with pytest.raises(OutputOverrunError, match="exceeded"):
        run_docker_process(cmd, timeout=5.0, max_bytes=8192)
    elapsed = time.monotonic() - start
    # Should terminate immediately upon exceeding 8192 bytes (around 8 chunks = ~0.08s), not wait 5s or 10s
    assert elapsed < 3.0


def test_run_docker_process_popen_streaming_interface_chunking() -> None:
    """Test run_docker_process with a Popen-compatible streaming interface.

    Verifies chunked non-blocking reads and prompt kill on overrun via an injected
    Popen class with pipe streams.
    """
    class MockPopen:
        def __init__(self, cmd: list[str], stdout=None, stderr=None, start_new_session=False, **kwargs: Any) -> None:
            self.cmd = cmd
            out_r, out_w = os.pipe()
            err_r, err_w = os.pipe()
            os.write(out_w, b"M" * 50_000)
            os.close(out_w)
            os.close(err_w)
            self.stdout = os.fdopen(out_r, "rb", buffering=0)
            self.stderr = os.fdopen(err_r, "rb", buffering=0)
            self.pid = 999999
            self.killed = False

        def poll(self) -> Optional[int]:
            return -9 if self.killed else None

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout: Optional[float] = None) -> int:
            return -9 if self.killed else 0

    class MockSubprocessModule:
        Popen = MockPopen

    with pytest.raises(OutputOverrunError, match="exceeded"):
        run_docker_process(
            ["dummy_cmd"],
            timeout=5.0,
            max_bytes=8192,
            subprocess_module=MockSubprocessModule,
        )


def test_docker_executor_handles_stdout_overrun(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DockerExecutor fails cleanly with container cleanup when helper output exceeds bound."""
    import scopewatch.executor_docker as docker_mod

    ws = tmp_path / "ws"
    ws.mkdir()
    action = _action("read_text", "some_file.txt")
    decision = _allow_decision(action.id)

    # Fake run_docker_process raising OutputOverrunError
    cleaned_containers = []

    def fake_run_docker(cmd, **kwargs):
        raise OutputOverrunError("Container output exceeded size limit.")

    monkeypatch.setattr(docker_mod, "run_docker_process", fake_run_docker)
    monkeypatch.setattr(docker_mod, "is_docker_available", lambda *a, **k: True)
    monkeypatch.setattr(
        docker_mod, "_best_effort_remove", lambda name: cleaned_containers.append(name)
    )

    executor = DockerExecutor()
    receipt = executor.execute(action, ws, policy_decision=decision)

    assert receipt.status == ExecutionStatus.FAILED
    assert receipt.error_code == "EXECUTION_FAILED"
    assert "exceeded size limit" in receipt.sanitized_result.get("error", "")
    assert len(cleaned_containers) > 0


# =============================================================================
# 3. Copy-back size refusal & preserved target workspace
# =============================================================================


def test_check_staged_workspace_size_within_bound(tmp_path: Path) -> None:
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "file.txt").write_bytes(b"hello world")
    total = check_staged_workspace_size(staged, max_bytes=100)
    assert total == 11


def test_check_staged_workspace_size_oversized(tmp_path: Path) -> None:
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "huge.bin").write_bytes(b"X" * 200)
    with pytest.raises(OversizedCopyBackError, match="exceeds limit"):
        check_staged_workspace_size(staged, max_bytes=100)


def test_sync_copy_back_refuses_oversized_and_preserves_target(tmp_path: Path) -> None:
    """Acceptance criterion: copy-back refusing a tree over the limit without modifying target."""
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "original.txt").write_text("untouched original")

    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "original.txt").write_text("MODIFIED")
    (staged / "large_file.bin").write_bytes(b"A" * 500)

    # Limit set to 250 bytes
    with pytest.raises(OversizedCopyBackError, match="exceeds limit"):
        sync_copy_back(staged, dest, max_bytes=250)

    # Crucial domain invariant: target workspace is preserved completely
    assert (dest / "original.txt").read_text() == "untouched original"
    assert not (dest / "large_file.bin").exists()


def test_sync_copy_back_symlinks_do_not_inflate_size_or_escape(tmp_path: Path) -> None:
    """Symlinks are not followed during size checking (cannot cause false overrun or escape)."""
    dest = tmp_path / "dest"
    dest.mkdir()

    outside = tmp_path / "outside"
    outside.mkdir()
    huge_outside = outside / "huge.bin"
    huge_outside.write_bytes(b"Z" * 10_000)

    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "small.txt").write_text("small")
    # Link pointing to huge file outside workspace
    (staged / "link_to_huge").symlink_to(huge_outside)

    # Size check with max_bytes=500 passes because symlink is not followed
    total = check_staged_workspace_size(staged, max_bytes=500)
    assert total == 5


def test_docker_executor_refuses_oversized_copyback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end through DockerExecutor: oversized workspace fails execution and preserves dest."""
    import scopewatch.executor_docker as docker_mod

    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "important.txt").write_text("preserved content")

    action = _action("write_text", "test.txt", arguments={"content": "hello"})
    decision = _allow_decision(action.id)

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[:2] == ["docker", "info"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        if isinstance(cmd, list) and cmd[:2] == ["docker", "run"]:
            copy_path = Path(cmd[cmd.index("-v") + 1].split(":")[0])
            # Simulate container creating an oversized file
            (copy_path / "oversized.bin").write_bytes(b"B" * 5000)
            payload = {
                "status": "EXECUTED",
                "result": {"operation": "write_text", "bytes_written": 5},
                "error_code": None,
            }
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps(payload).encode(), stderr=b""
            )
        if isinstance(cmd, list) and cmd[:3] == ["docker", "rm", "-f"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        return subprocess.run(cmd, **kwargs)

    monkeypatch.setattr(docker_mod, "run_docker_process", fake_run)
    monkeypatch.setattr(docker_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(docker_mod, "is_docker_available", lambda *a, **k: True)
    # Set small copy-back limit for test
    monkeypatch.setattr(docker_mod, "MAX_COPY_BACK_BYTES", 1000)

    executor = DockerExecutor()
    receipt = executor.execute(action, ws, policy_decision=decision)

    assert receipt.status == ExecutionStatus.FAILED
    assert receipt.error_code == "OVERSIZED_WORKSPACE"
    assert "exceeds limit" in receipt.sanitized_result.get("error", "")
    # Dest workspace preserved!
    assert (ws / "important.txt").read_text() == "preserved content"
    assert not (ws / "oversized.bin").exists()


def test_runner_handles_container_output_overrun(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Runner sidecar handles container stdout overrun by failing execution cleanly."""
    import importlib.util
    from scopewatch.executor_remote import sign_dispatch_payload

    runner_path = Path(__file__).resolve().parents[2] / "deploy" / "executor-runner" / "runner.py"
    spec = importlib.util.spec_from_file_location("runner_for_test", runner_path)
    assert spec is not None and spec.loader is not None
    runner_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner_mod)

    ws_root = tmp_path / "runner_ws"
    ws_root.mkdir()
    run_ws = ws_root / "run-overrun"
    run_ws.mkdir()

    config_obj = runner_mod.RunnerConfig()
    config_obj.token = "test-token"
    config_obj.signing_key = "test-key"
    config_obj.workspace = ws_root

    action = {
        "id": "act-1",
        "run_id": "run-overrun",
        "operation": "read_text",
        "resource": "file.txt",
        "arguments": {},
    }
    decision = {"id": "dec-1", "action_request_id": "act-1", "outcome": "ALLOW"}
    body = {
        "issued_at": 1000.0,
        "dispatch_token": "token-test-1",
        "run_workspace": "run-overrun",
        "action_digest": runner_mod.canonical_action_digest(action, decision, "run-overrun", None),
        "action": action,
        "policy_decision": decision,
        "approval": None,
        "task_scope": None,
    }
    body["dispatch_signature"] = sign_dispatch_payload(body, config_obj.signing_key)

    def fake_run_docker(cmd, **kwargs):
        raise OutputOverrunError("Container output exceeded size limit.")

    monkeypatch.setattr(runner_mod, "run_docker_process", fake_run_docker)

    code, resp = runner_mod.handle_execute(
        body,
                config=config_obj,
        now=1000.0,
    )
    assert code == 200
    assert resp["status"] == "FAILED"
    assert resp["error_code"] == "EXECUTION_FAILED"
    assert "exceeded size limit" in resp["result"]["error"]


def test_runner_refuses_oversized_copyback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Runner sidecar refuses oversized workspace copyback before effect, preserving target."""
    import importlib.util
    from scopewatch.executor_remote import sign_dispatch_payload

    runner_path = Path(__file__).resolve().parents[2] / "deploy" / "executor-runner" / "runner.py"
    spec = importlib.util.spec_from_file_location("runner_for_test_2", runner_path)
    assert spec is not None and spec.loader is not None
    runner_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner_mod)

    ws_root = tmp_path / "runner_ws"
    ws_root.mkdir()
    run_ws = ws_root / "run-copyback"
    run_ws.mkdir()
    (run_ws / "critical.txt").write_text("critical untouched content")

    config_obj = runner_mod.RunnerConfig()
    config_obj.token = "test-token"
    config_obj.signing_key = "test-key"
    config_obj.workspace = ws_root

    action = {
        "id": "act-2",
        "run_id": "run-copyback",
        "operation": "write_text",
        "resource": "file.txt",
        "arguments": {"content": "hello"},
    }
    decision = {"id": "dec-2", "action_request_id": "act-2", "outcome": "ALLOW"}
    body = {
        "issued_at": 1000.0,
        "dispatch_token": "token-test-2",
        "run_workspace": "run-copyback",
        "action_digest": runner_mod.canonical_action_digest(action, decision, "run-copyback", None),
        "action": action,
        "policy_decision": decision,
        "approval": None,
        "task_scope": None,
    }
    body["dispatch_signature"] = sign_dispatch_payload(body, config_obj.signing_key)

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[:2] == ["docker", "run"]:
            copy_path = Path(cmd[cmd.index("-v") + 1].split(":")[0])
            (copy_path / "huge.dat").write_bytes(b"Z" * 1000)
            payload = {
                "status": "EXECUTED",
                "result": {"operation": "write_text", "bytes_written": 5},
                "error_code": None,
            }
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload).encode(), stderr=b"")
        return subprocess.run(cmd, **kwargs)

    monkeypatch.setattr(runner_mod, "run_docker_process", fake_run)
    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(runner_mod, "MAX_COPY_BACK_BYTES", 500)

    code, resp = runner_mod.handle_execute(
        body,
                config=config_obj,
        now=1000.0,
    )
    assert code == 200
    assert resp["status"] == "FAILED"
    assert resp["error_code"] == "OVERSIZED_WORKSPACE"
    assert "Copy-back refused" in resp["result"]["error"]

    # Target workspace preserved untouched
    assert (run_ws / "critical.txt").read_text() == "critical untouched content"
    assert not (run_ws / "huge.dat").exists()

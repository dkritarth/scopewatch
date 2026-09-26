"""Docker-specific executor tests.

Unit tests (flag/image/mount/fail-closed checks) run without a daemon.
Integration tests are SKIPPED when Docker is absent and run in the dedicated
``docker-executor`` CI workflow on ubuntu-latest.
"""

from datetime import datetime, timezone
from pathlib import Path
import subprocess
import uuid

import pytest

from scopewatch.executor import ExecutionSecurityError, execute_action
from scopewatch.executor_docker import (
    DOCKER_IMAGE,
    EXECUTOR_IMAGE_ENV_VAR,
    DockerExecutor,
    _make_world_accessible,
    _stage_workspace_copy,
    build_docker_command,
    docker_available,
    is_docker_available,
    resolve_executor_image,
)
from scopewatch.models import ApprovalStatus, ExecutionStatus, PolicyOutcome, ReasonCode
from scopewatch.schemas import ActionRequest, ApprovalRequest, PolicyDecision


def _allow_decision(action_id: str = "action-1") -> PolicyDecision:
    return PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id=action_id,
        outcome=PolicyOutcome.ALLOW,
        reason_code=ReasonCode.ALLOWED_TOOL_AND_RESOURCE,
        explanation="Permitted",
        matched_rule="RULE_ALLOWED",
        decided_at=datetime.now(timezone.utc).isoformat(),
        deterministic=True,
    )


@pytest.fixture
def docker_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "docs").mkdir()
    (ws / "docs" / "file_a.txt").write_text("content A", encoding="utf-8")
    return ws


@pytest.fixture
def requires_docker() -> None:
    if not is_docker_available():
        pytest.skip("Docker daemon unavailable; skipping Docker integration test.")


def _action(
    operation: str, resource: str, action_id: str = "action-1", **kwargs: object
) -> ActionRequest:
    return ActionRequest(
        id=action_id,
        run_id="run-1",
        tool="workspace",
        operation=operation,
        resource=resource,
        arguments=kwargs.get("arguments", {}),  # type: ignore[arg-type]
        requested_at=datetime.now(timezone.utc).isoformat(),
    )


# ---------------- Unit tests: no daemon required ----------------


def test_docker_image_is_pinned_digest() -> None:
    assert DOCKER_IMAGE.startswith("python:3.12-slim")
    assert "@sha256:" in DOCKER_IMAGE
    digest = DOCKER_IMAGE.split("@sha256:")[1]
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_docker_command_has_hardening_flags(tmp_path: Path) -> None:
    copy = tmp_path / "copy"
    copy.mkdir()
    cmd = build_docker_command(
        image=DOCKER_IMAGE,
        workspace_copy=copy,
        operation="read_text",
        resource="docs/file_a.txt",
        arguments_json="{}",
        container_name="scopewatch-test",
        run_label="run-1",
    )
    joined = " ".join(cmd)
    for flag in (
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp",
        "--user",
        "65534:65534",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "64",
        "--memory",
        "256m",
        "--cpus",
    ):
        assert flag in cmd, f"missing hardening flag: {flag} in {joined}"
    assert cmd[cmd.index("--memory-swap") + 1] == "256m"


def test_docker_command_mounts_only_workspace_copy(tmp_path: Path) -> None:
    copy = tmp_path / "copy"
    copy.mkdir()
    cmd = build_docker_command(
        image=DOCKER_IMAGE,
        workspace_copy=copy,
        operation="list_directory",
        resource="docs",
        arguments_json="{}",
        container_name="scopewatch-test",
        run_label="run-1",
    )
    mounts = [cmd[i + 1] for i, c in enumerate(cmd) if c == "-v"]
    assert len(mounts) == 1
    assert mounts[0] == f"{copy}:/workspace:rw"
    blob = " ".join(cmd)
    assert "/var/run/docker.sock" not in blob
    assert str(Path.home()) not in blob
    assert "HOME=" not in blob and ".ssh" not in blob
    # Image itself is the pinned digest, not a floating tag.
    assert DOCKER_IMAGE in cmd


def test_docker_unavailable_fails_closed_no_fallback(
    docker_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    import scopewatch.executor_docker as docker_mod

    monkeypatch.setattr(docker_mod, "is_docker_available", lambda *a, **k: False)
    action = _action("write_text", "docs/from_docker.txt", arguments={"content": "x"})
    receipt = execute_action(action, docker_workspace, policy_decision=_allow_decision())
    assert receipt.status == ExecutionStatus.FAILED
    assert receipt.error_code == "EXECUTION_FAILED"
    assert receipt.executor == "docker-executor"
    # NEVER silently falls back to local: the file must not have been written.
    assert not (docker_workspace / "docs" / "from_docker.txt").exists()


def test_docker_requires_stored_decision(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    ws = tmp_path / "ws"
    ws.mkdir()
    action = _action("read_text", "docs/file_a.txt")
    with pytest.raises(ExecutionSecurityError, match="Direct execution without policy evidence"):
        execute_action(action, ws, policy_decision=None)


def test_docker_deny_decision_cannot_execute(
    monkeypatch: pytest.MonkeyPatch, docker_workspace: Path
) -> None:
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    deny = PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id="action-1",
        outcome=PolicyOutcome.DENY,
        reason_code=ReasonCode.BLOCKED_PATH,
        explanation="Blocked",
        matched_rule="RULE_BLOCKED",
        decided_at=datetime.now(timezone.utc).isoformat(),
        deterministic=True,
    )
    receipt = execute_action(
        _action("read_text", "docs/file_a.txt"), docker_workspace, policy_decision=deny
    )
    assert receipt.status == ExecutionStatus.NOT_EXECUTED
    assert receipt.error_code == ReasonCode.BLOCKED_PATH.value


def test_docker_hold_without_approval_raises(
    monkeypatch: pytest.MonkeyPatch, docker_workspace: Path
) -> None:
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    hold = PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id="action-1",
        outcome=PolicyOutcome.HOLD,
        reason_code=ReasonCode.APPROVAL_REQUIRED,
        explanation="Hold",
        matched_rule="RULE_HOLD",
        decided_at=datetime.now(timezone.utc).isoformat(),
        deterministic=True,
    )
    with pytest.raises(ExecutionSecurityError, match="Held action requires valid approved"):
        execute_action(
            _action("read_text", "docs/file_a.txt"), docker_workspace, policy_decision=hold
        )


def test_docker_helper_revalidates_workspace_boundary() -> None:
    from scopewatch.executor_docker import _helper_code

    code = _helper_code()
    assert "/workspace" in code
    assert "relative_to" in code
    assert "escapes" in code


# ---------------- Integration tests: skipped when Docker absent ----------------


def test_docker_no_network_from_inside(requires_docker: None, tmp_path: Path) -> None:
    copy = tmp_path / "copy"
    copy.mkdir()
    cmd = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp",
        "--user",
        "65534:65534",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "64",
        "--memory",
        "256m",
        "--memory-swap",
        "256m",
        "--cpus",
        "1.0",
        DOCKER_IMAGE,
        "python3",
        "-c",
        "import socket; socket.create_connection(('8.8.8.8', 53), timeout=5); print('NETWORK_REACHABLE')",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    assert proc.returncode != 0
    assert b"NETWORK_REACHABLE" not in proc.stdout


def test_docker_host_home_and_socket_not_visible(
    requires_docker: None, docker_workspace: Path
) -> None:
    (docker_workspace / "docs" / "sentinel.txt").write_text("sentinel", encoding="utf-8")
    staging = docker_workspace.parent / "copy-check"
    import shutil

    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(docker_workspace, staging, symlinks=True)
    try:
        cmd = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--user",
            "65534:65534",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "64",
            "--memory",
            "256m",
            "--memory-swap",
            "256m",
            "--cpus",
            "1.0",
            "-v",
            f"{staging}:/workspace:rw",
            "--workdir",
            "/workspace",
            DOCKER_IMAGE,
            "sh",
            "-c",
            "test ! -e /var/run/docker.sock && test -f /workspace/docs/sentinel.txt && test ! -e /home/sentinel.txt && echo ISOLATED_OK",
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        assert proc.returncode == 0
        assert b"ISOLATED_OK" in proc.stdout
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def test_docker_writes_outside_workspace_fail(
    requires_docker: None, docker_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    outside = docker_workspace.parent / "outside.txt"
    if outside.exists():
        outside.unlink()
    action = _action("write_text", "../outside.txt", arguments={"content": "escape"})
    receipt = execute_action(action, docker_workspace, policy_decision=_allow_decision())
    assert receipt.status == ExecutionStatus.FAILED
    assert receipt.error_code == "EXECUTION_FAILED"
    assert not outside.exists()


def test_docker_symlink_outside_not_followed(
    requires_docker: None, docker_workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    secret = tmp_path / "host-secret.txt"
    secret.write_text("HOST_SECRET_SYNTHETIC_123", encoding="utf-8")
    link = docker_workspace / "docs" / "evil-link.txt"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("Cannot create symlinks on this platform.")
    action = _action("read_text", "docs/evil-link.txt")
    receipt = execute_action(action, docker_workspace, policy_decision=_allow_decision())
    assert receipt.status == ExecutionStatus.FAILED
    preview = (receipt.sanitized_result or {}).get("preview", "")
    assert "HOST_SECRET_SYNTHETIC_123" not in str(preview)


def test_docker_container_removed_after_run(
    requires_docker: None, docker_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    before = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "label=scopewatch.executor=docker"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    before_ids = set(before.stdout.decode().split())
    receipt = execute_action(
        _action("read_text", "docs/file_a.txt"),
        docker_workspace,
        policy_decision=_allow_decision(),
    )
    assert receipt.status == ExecutionStatus.EXECUTED
    after = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "label=scopewatch.executor=docker"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    after_ids = set(after.stdout.decode().split())
    assert after_ids <= before_ids


def test_docker_approved_hold_executes(
    requires_docker: None, docker_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    hold = PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id="action-1",
        outcome=PolicyOutcome.HOLD,
        reason_code=ReasonCode.APPROVAL_REQUIRED,
        explanation="Hold",
        matched_rule="RULE_HOLD",
        decided_at=datetime.now(timezone.utc).isoformat(),
        deterministic=True,
    )
    approval = ApprovalRequest(
        id=str(uuid.uuid4()),
        run_id="run-1",
        action_request_id="action-1",
        policy_decision_id=hold.id,
        status=ApprovalStatus.APPROVED,
        requested_at=datetime.now(timezone.utc).isoformat(),
        expires_at=datetime.now(timezone.utc).isoformat(),
    )
    receipt = execute_action(
        _action("read_text", "docs/file_a.txt"),
        docker_workspace,
        policy_decision=hold,
        approval_request=approval,
    )
    assert receipt.status == ExecutionStatus.EXECUTED
    assert receipt.executor == "docker-executor"


def test_docker_available_helper_returns_bool() -> None:
    assert isinstance(docker_available(), bool)


def test_resolve_executor_image_defaults_to_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(EXECUTOR_IMAGE_ENV_VAR, raising=False)
    assert resolve_executor_image() == DOCKER_IMAGE
    assert DockerExecutor().image == DOCKER_IMAGE


def test_resolve_executor_image_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(EXECUTOR_IMAGE_ENV_VAR, "scopewatch-executor:ci")
    assert resolve_executor_image() == "scopewatch-executor:ci"
    assert DockerExecutor().image == "scopewatch-executor:ci"
    assert (
        DockerExecutor(image="explicit:tag").image == "explicit:tag"
    )


def test_stage_workspace_copy_is_world_accessible(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "nested").mkdir(parents=True)
    locked = src / "nested" / "secret.txt"
    locked.write_text("synthetic", encoding="utf-8")
    locked.chmod(0o600)
    (src / "nested").chmod(0o700)
    (src / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (src / "run.sh").chmod(0o700)
    (src / "link.txt").symlink_to("nested/secret.txt")

    staging_root, copy = _stage_workspace_copy(src)
    try:
        assert (copy / "nested" / "secret.txt").read_text(
            encoding="utf-8"
        ) == "synthetic"
        assert (copy / "nested" / "secret.txt").stat().st_mode & 0o077
        assert (copy / "nested").stat().st_mode & 0o007
        assert (copy / "link.txt").is_symlink()
        # The source tree keeps its restrictive permissions.
        assert locked.stat().st_mode & 0o077 == 0
    finally:
        import shutil as _shutil

        _shutil.rmtree(staging_root, ignore_errors=True)


def test_make_world_accessible_skips_symlink_target(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("synthetic", encoding="utf-8")
    outside.chmod(0o600)
    root = tmp_path / "root"
    root.mkdir()
    (root / "evil").symlink_to(outside)

    _make_world_accessible(root)

    assert (root / "evil").is_symlink()
    assert outside.stat().st_mode & 0o077 == 0

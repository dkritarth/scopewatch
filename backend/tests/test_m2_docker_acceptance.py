"""M2 #35 acceptance matrix: Docker-isolated executor (offline, no daemon).

Each test maps to one acceptance bullet from issue #35 and runs without a
Docker daemon: hardening is asserted through ``build_docker_command``,
lifecycle/isolation through ``_stage_workspace_copy`` plus a mocked
``subprocess.run``, and fail-closed behaviour through a stubbed
``is_docker_available``. Daemon round-trips stay in
``test_executor_docker.py`` behind ``requires_docker`` (skip-cleanly); the
one ``requires_docker`` test here documents the daemon-only remainder.

Ownership: issues #66/#68 (single-use replay, revalidation), #78/#80
(remote sidecar, copy-back symlink tolerance), and #39 (bypass gap-close)
are owned by other threads — none of their cases are duplicated here.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scopewatch.executor import ExecutionSecurityError, execute_action, get_executor_backend
from scopewatch.executor_docker import (
    DOCKER_CPUS,
    DOCKER_IMAGE,
    DOCKER_MEMORY,
    DOCKER_PIDS_LIMIT,
    DOCKER_USER,
    DockerExecutor,
    _helper_code,
    _stage_workspace_copy,
    build_docker_command,
    resolve_executor_image,
)
from scopewatch.models import ApprovalStatus, ExecutionStatus, PolicyOutcome, ReasonCode
from scopewatch.schemas import ActionRequest, ApprovalRequest, PolicyDecision


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _allow_decision(action_id: str = "action-1") -> PolicyDecision:
    return PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id=action_id,
        outcome=PolicyOutcome.ALLOW,
        reason_code=ReasonCode.ALLOWED_TOOL_AND_RESOURCE,
        explanation="Permitted (M2 acceptance fixture).",
        matched_rule="RULE_ALLOWED",
        decided_at=_now(),
        deterministic=True,
    )


def _action(
    operation: str = "read_text",
    resource: str = "docs/file_a.txt",
    action_id: str = "action-1",
    arguments: dict | None = None,
) -> ActionRequest:
    return ActionRequest(
        id=action_id,
        run_id="run-1",
        tool="workspace",
        operation=operation,
        resource=resource,
        arguments=arguments or {},
        requested_at=_now(),
    )


@pytest.fixture
def accept_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "file_a.txt").write_text("synthetic content A", encoding="utf-8")
    return ws


# ---------------- A1: single entry point + backend switch ----------------


def test_m2_single_entry_dispatches_to_backends(
    accept_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`execute_action` routes local vs docker; docker never calls local."""
    import scopewatch.executor as exec_mod

    calls: list[str] = []

    def _fail_local(*args: object, **kwargs: object) -> object:
        calls.append("local")
        raise AssertionError("docker path must never fall back to local")

    monkeypatch.setattr(exec_mod, "_execute_local", _fail_local)
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    import scopewatch.executor_docker as docker_mod

    monkeypatch.setattr(docker_mod, "is_docker_available", lambda *a, **k: False)
    receipt = execute_action(
        _action(), accept_workspace, policy_decision=_allow_decision()
    )
    assert receipt.executor == "docker-executor"
    assert calls == []  # fail-closed inside docker, no local fallback


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("docker", "docker"),
        ("DOCKER", "docker"),
        ("  Docker  ", "docker"),
        ("local", "local"),
        ("", "local"),
        ("  ", "local"),
        ("bogus-backend", "bogus-backend"),
    ],
)
def test_m2_backend_switch_normalization(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
) -> None:
    """SCOPEWATCH_EXECUTOR is stripped/lowered; unset/empty means local."""
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", raw)
    assert get_executor_backend() == expected


def test_m2_backend_unset_means_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCOPEWATCH_EXECUTOR", raising=False)
    assert get_executor_backend() == "local"


def test_m2_unknown_backend_falls_through_to_local(
    accept_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anything != docker uses the local backend (no third path)."""
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "bogus-backend")
    receipt = execute_action(
        _action(), accept_workspace, policy_decision=_allow_decision()
    )
    assert receipt.executor == "synthetic-workspace-executor"
    assert receipt.status == ExecutionStatus.EXECUTED


# ---------------- A2: hardening flags ----------------


def test_m2_docker_full_hardening_flags(tmp_path: Path) -> None:
    """All #35 container flags are present in the built command."""
    copy = tmp_path / "copy"
    copy.mkdir()
    cmd = build_docker_command(
        image=DOCKER_IMAGE,
        workspace_copy=copy,
        operation="read_text",
        resource="docs/file_a.txt",
        arguments_json="{}",
        container_name="scopewatch-accept",
        run_label="run-1",
    )
    for flag in (
        "--rm",
        "--network", "none",
        "--read-only",
        "--tmpfs", "/tmp",
        "--user", DOCKER_USER,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", DOCKER_PIDS_LIMIT,
        "--memory", DOCKER_MEMORY,
        "--memory-swap", DOCKER_MEMORY,
        "--cpus", DOCKER_CPUS,
        "--workdir", "/workspace",
    ):
        assert flag in cmd, f"missing hardening flag: {flag}"
    assert DOCKER_USER == "65534:65534"  # non-root nobody
    assert "--privileged" not in cmd


# ---------------- A3: mount hygiene ----------------


def test_m2_docker_mounts_only_workspace_copy(tmp_path: Path) -> None:
    """Exactly one mount: the per-run copy at /workspace:rw; nothing else."""
    copy = tmp_path / "copy"
    copy.mkdir()
    cmd = build_docker_command(
        image=DOCKER_IMAGE,
        workspace_copy=copy,
        operation="list_directory",
        resource="docs",
        arguments_json="{}",
        container_name="scopewatch-accept",
        run_label="run-1",
    )
    mounts = [cmd[i + 1] for i, c in enumerate(cmd) if c == "-v"]
    assert len(mounts) == 1
    assert mounts[0] == f"{copy}:/workspace:rw"
    blob = " ".join(cmd)
    assert "/var/run/docker.sock" not in blob
    assert str(Path.home()) not in blob
    assert ".ssh" not in blob
    assert "HOME=" not in blob
    for token in ("AWS_", "NEBIUS_", "SSH_AUTH_SOCK", "GITHUB_TOKEN", ".aws", ".config"):
        assert token not in blob


# ---------------- A4: pinned image ----------------


def test_m2_docker_image_digest_pinned() -> None:
    """Base image is pinned by digest, not a floating tag."""
    assert "@sha256:" in DOCKER_IMAGE
    digest = DOCKER_IMAGE.split("@sha256:")[1]
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_m2_docker_image_matches_ci_dockerfile() -> None:
    """The CI executor image uses the same pinned base digest."""
    dockerfile = (
        Path(__file__).resolve().parent.parent / "executor" / "Dockerfile"
    ).read_text(encoding="utf-8")
    digest = DOCKER_IMAGE.split("@sha256:")[1]
    assert digest in dockerfile


def test_m2_resolve_image_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit arg > env override > pinned default."""
    from scopewatch.executor_docker import EXECUTOR_IMAGE_ENV_VAR

    monkeypatch.delenv(EXECUTOR_IMAGE_ENV_VAR, raising=False)
    assert resolve_executor_image() == DOCKER_IMAGE
    monkeypatch.setenv(EXECUTOR_IMAGE_ENV_VAR, "scopewatch-executor:ci")
    assert resolve_executor_image() == "scopewatch-executor:ci"
    assert resolve_executor_image("explicit:tag") == "explicit:tag"
    assert DockerExecutor(image="explicit:tag").image == "explicit:tag"


# ---------------- A5: stored decision required (before daemon touch) ----------------


def test_m2_docker_requires_stored_decision(
    accept_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    with pytest.raises(ExecutionSecurityError, match="Direct execution without policy"):
        execute_action(_action(), accept_workspace, policy_decision=None)


def test_m2_docker_deny_never_touches_daemon(
    accept_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DENY short-circuits on the host; the daemon is never contacted."""
    import scopewatch.executor_docker as docker_mod

    def _must_not_run(*args: object, **kwargs: object) -> object:
        raise AssertionError("DENY path touched the daemon")

    monkeypatch.setattr(docker_mod, "is_docker_available", _must_not_run)
    deny = PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id="action-1",
        outcome=PolicyOutcome.DENY,
        reason_code=ReasonCode.BLOCKED_PATH,
        explanation="Blocked (M2 acceptance fixture).",
        matched_rule="RULE_BLOCKED",
        decided_at=_now(),
        deterministic=True,
    )
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    receipt = execute_action(_action(), accept_workspace, policy_decision=deny)
    assert receipt.status == ExecutionStatus.NOT_EXECUTED
    assert receipt.error_code == ReasonCode.BLOCKED_PATH.value


def test_m2_docker_hold_without_approval_raises(
    accept_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    hold = PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id="action-1",
        outcome=PolicyOutcome.HOLD,
        reason_code=ReasonCode.APPROVAL_REQUIRED,
        explanation="Hold (M2 acceptance fixture).",
        matched_rule="RULE_HOLD",
        decided_at=_now(),
        deterministic=True,
    )
    with pytest.raises(ExecutionSecurityError, match="Held action requires valid approved"):
        execute_action(_action(), accept_workspace, policy_decision=hold)


def test_m2_docker_network_request_refused(
    accept_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    with pytest.raises(ExecutionSecurityError, match="Network requests are forbidden"):
        execute_action(
            _action(operation="network_request", resource="https://example.invalid"),
            accept_workspace,
            policy_decision=_allow_decision(),
        )


# ---------------- A6: helper re-validates /workspace ----------------


def test_m2_helper_revalidates_workspace_boundary() -> None:
    """In-container helper resolves every path under /workspace (defence in depth)."""
    code = _helper_code()
    assert "/workspace" in code
    assert "relative_to" in code
    assert "escapes" in code
    assert "resolve()" in code
    assert "absolute paths are prohibited" in code
    assert "shell=False" in code
    assert "SCOPEWATCH_WORKSPACE" in code


# ---------------- A7: no-docker fails closed, never silent fallback ----------------


def test_m2_no_docker_fails_closed_without_fallback(
    accept_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scopewatch.executor_docker as docker_mod

    monkeypatch.setattr(docker_mod, "is_docker_available", lambda *a, **k: False)
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    receipt = execute_action(
        _action(
            operation="write_text",
            resource="docs/from_docker.txt",
            arguments={"content": "must not leak to local"},
        ),
        accept_workspace,
        policy_decision=_allow_decision(),
    )
    assert receipt.status == ExecutionStatus.FAILED
    assert receipt.error_code == "EXECUTION_FAILED"
    assert receipt.executor == "docker-executor"
    assert not (accept_workspace / "docs" / "from_docker.txt").exists()


def test_m2_docker_cli_missing_fails_closed(
    accept_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FileNotFoundError from the docker CLI also fails closed."""
    import scopewatch.executor_docker as docker_mod

    monkeypatch.setattr(docker_mod, "is_docker_available", lambda *a, **k: True)
    monkeypatch.setattr(
        docker_mod.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("no docker")),
    )
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    receipt = execute_action(
        _action(), accept_workspace, policy_decision=_allow_decision()
    )
    assert receipt.status == ExecutionStatus.FAILED
    assert receipt.error_code == "EXECUTION_FAILED"


# ---------------- A8: per-run workspace copies are isolated ----------------


def test_m2_staging_copies_are_distinct_dirs(tmp_path: Path) -> None:
    """Each run stages its own temp copy; the source keeps its permissions."""
    src = tmp_path / "src"
    (src / "nested").mkdir(parents=True)
    locked = src / "nested" / "notes.txt"
    locked.write_text("synthetic", encoding="utf-8")
    locked.chmod(0o600)

    root_a, copy_a = _stage_workspace_copy(src)
    root_b, copy_b = _stage_workspace_copy(src)
    try:
        assert root_a != root_b
        assert copy_a != copy_b
        assert (copy_a / "nested" / "notes.txt").read_text(encoding="utf-8") == "synthetic"
        # Source tree is untouched by staging.
        assert locked.stat().st_mode & 0o077 == 0
        # Mutating one copy does not affect the other (per-run isolation).
        (copy_a / "nested" / "notes.txt").write_text("mutated", encoding="utf-8")
        assert (copy_b / "nested" / "notes.txt").read_text(encoding="utf-8") == "synthetic"
        assert locked.read_text(encoding="utf-8") == "synthetic"
    finally:
        import shutil as _shutil

        _shutil.rmtree(root_a, ignore_errors=True)
        _shutil.rmtree(root_b, ignore_errors=True)


def _mock_docker_success(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Stub the docker CLI with an EXECUTED helper payload; record commands."""
    import scopewatch.executor_docker as docker_mod

    seen: list[list[str]] = []

    class _Proc:
        returncode = 0
        stdout = json.dumps(
            {
                "status": "EXECUTED",
                "result": {
                    "operation": "read_text",
                    "resource": "docs/file_a.txt",
                    "byte_count": 19,
                    "preview": "synthetic content A",
                    "truncated": False,
                },
                "error_code": None,
            }
        ).encode()
        stderr = b""

    def _fake_run(cmd: object, **kwargs: object) -> _Proc:
        assert isinstance(cmd, list)
        seen.append(list(cmd))
        return _Proc()

    monkeypatch.setattr(docker_mod, "is_docker_available", lambda *a, **k: True)
    monkeypatch.setattr(docker_mod.subprocess, "run", _fake_run)
    return seen


def test_m2_per_run_lifecycle_labels_and_cleanup(
    accept_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One mocked run: --rm + run label, then staging dir is removed."""
    seen = _mock_docker_success(monkeypatch)
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")

    before = set(Path(tempfile.gettempdir()).glob("scopewatch-run-*"))
    receipt = execute_action(
        _action(), accept_workspace, policy_decision=_allow_decision()
    )
    after = set(Path(tempfile.gettempdir()).glob("scopewatch-run-*"))

    assert receipt.status == ExecutionStatus.EXECUTED
    assert receipt.executor == "docker-executor"
    runs = [c for c in seen if len(c) > 1 and c[1] == "run"]
    assert len(runs) == 1
    assert "--rm" in runs[0]
    assert "scopewatch.executor=docker" in " ".join(runs[0])
    assert "scopewatch.run=run-1" in " ".join(runs[0])
    assert after <= before  # staging copy cleaned up


def test_m2_container_names_unique_per_run(
    accept_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Container names embed a fresh random suffix per dispatch."""
    seen = _mock_docker_success(monkeypatch)
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    for _ in range(2):
        execute_action(_action(), accept_workspace, policy_decision=_allow_decision())
    runs = [c for c in seen if len(c) > 1 and c[1] == "run"]
    names = [runs[i][runs[i].index("--name") + 1] for i in range(len(runs))]
    assert len(names) == 2
    assert names[0] != names[1]
    assert all(n.startswith("scopewatch-") for n in names)
    # Best-effort removal runs for every container.
    rms = [c for c in seen if len(c) > 2 and c[1] == "rm"]
    assert len(rms) >= 2


def test_m2_docker_timeout_removes_container_fail_closed(
    accept_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung `docker run` is removed and reported FAILED (fail-closed)."""
    import scopewatch.executor_docker as docker_mod

    seen: list[list[str]] = []

    def _fake_run(cmd: object, **kwargs: object) -> object:
        assert isinstance(cmd, list)
        seen.append(list(cmd))
        if len(cmd) > 1 and cmd[1] == "run":
            raise subprocess.TimeoutExpired(cmd, timeout=1)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(docker_mod, "is_docker_available", lambda *a, **k: True)
    monkeypatch.setattr(docker_mod.subprocess, "run", _fake_run)
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    receipt = execute_action(
        _action(), accept_workspace, policy_decision=_allow_decision()
    )
    assert receipt.status == ExecutionStatus.FAILED
    assert receipt.error_code == "EXECUTION_FAILED"
    assert any(len(c) > 2 and c[1] == "rm" for c in seen)


def test_m2_hold_approved_executes_through_mocked_docker(
    accept_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HOLD + APPROVED approval executes; the docker path honours approvals."""
    seen = _mock_docker_success(monkeypatch)
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    hold = PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id="action-1",
        outcome=PolicyOutcome.HOLD,
        reason_code=ReasonCode.APPROVAL_REQUIRED,
        explanation="Hold (M2 acceptance fixture).",
        matched_rule="RULE_HOLD",
        decided_at=_now(),
        deterministic=True,
    )
    approval = ApprovalRequest(
        id=str(uuid.uuid4()),
        run_id="run-1",
        action_request_id="action-1",
        policy_decision_id=hold.id,
        status=ApprovalStatus.APPROVED,
        requested_at=_now(),
        expires_at=_now(),
    )
    receipt = execute_action(
        _action(), accept_workspace, policy_decision=hold, approval_request=approval
    )
    assert receipt.status == ExecutionStatus.EXECUTED
    assert any(len(c) > 1 and c[1] == "run" for c in seen)


# ---------------- A10: daemon tests skip cleanly; CI workflow exists ----------------


def test_m2_docker_workflow_exists_and_targets_daemon() -> None:
    """The dedicated CI workflow builds the executor image and runs -k docker."""
    wf = (
        Path(__file__).resolve().parent.parent.parent
        / ".github"
        / "workflows"
        / "docker-executor.yml"
    ).read_text(encoding="utf-8")
    assert "SCOPEWATCH_EXECUTOR=docker" in wf
    assert "-k docker" in wf
    assert "ubuntu-latest" in wf
    assert "docker build" in wf


def test_m2_requires_docker_skips_without_daemon(
    requires_docker: None,
) -> None:
    """Daemon-only remainder (no network, no home, no writes outside,
    symlink containment, container removal) runs in CI; locally it skips.

    This marker test documents that the skip-cleanly contract holds: if the
    suite reaches this body, a daemon is present.
    """
    from scopewatch.executor_docker import is_docker_available

    assert is_docker_available() is True

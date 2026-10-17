"""Blocked paths never reach the run_command container (issue #183).

``blocked_paths`` used to be enforced on an action's ``resource`` and on argv
text only. ``stage_workspace_copy`` copied the whole run workspace, blocked
files included, into the directory the container mounts, so any allowlisted
command that runs workspace code (pytest collecting ``tests/conftest.py``)
could open ``.env`` and hand its content back in the receipt. Copy-back had
the mirror problem: whatever the container wrote under a blocked prefix
landed back in the run workspace.

These tests pin the fix in the shared ``docker_job`` module and in both of
its callers (the gateway's ``DockerExecutor`` and the runner sidecar). Docker
is faked at ``subprocess.run``; no daemon and no network are needed. All
fixture content is synthetic.
"""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import time
import uuid

import pytest

from scopewatch import docker_job
from scopewatch.executor_docker import DockerExecutor
from scopewatch.models import ExecutionStatus, PolicyOutcome, ReasonCode
from scopewatch.schemas import ActionRequest, PolicyDecision, Run, TaskScope

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_10 = REPO_ROOT / "demo" / "scenarios" / "10_fix_auth_test.json"
RUNNER_PATH = REPO_ROOT / "deploy" / "executor-runner" / "runner.py"

# Synthetic marker standing in for a secret. Never a real credential.
SECRET_MARKER = "SYNTHETIC_SECRET_DO_NOT_LEAK_183"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scenario_10_scope() -> TaskScope:
    data = json.loads(SCENARIO_10.read_text(encoding="utf-8"))
    return TaskScope(**data["task_scope"], created_at=_now())


def _scenario_10_workspace(root: Path) -> Path:
    """A synthetic stand-in for demo/coding-workspace with a planted .env."""
    ws = root / "workspace"
    (ws / "tests").mkdir(parents=True)
    (ws / "auth.py").write_text("def is_expired(now, exp):\n    return now > exp\n")
    (ws / "tests" / "test_auth.py").write_text("def test_ok():\n    assert True\n")
    (ws / "README.md").write_text("synthetic\n")
    (ws / ".env").write_text(f"API_KEY={SECRET_MARKER}\n")
    return ws


def _decision(action_id: str) -> PolicyDecision:
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


def _run_command_action(action_id: str = "action-183") -> ActionRequest:
    return ActionRequest(
        id=action_id,
        run_id="run-183",
        tool="workspace",
        operation="run_command",
        resource=".",
        arguments={"argv": ["python", "-m", "pytest", "-s"]},
        requested_at=_now(),
    )


# ---------------------------------------------------------------------------
# stage_workspace_copy
# ---------------------------------------------------------------------------


def test_staged_copy_for_scenario_10_scope_contains_no_env(tmp_path: Path) -> None:
    """Acceptance 1: scenario 10's scope stages everything except ``.env``."""
    ws = _scenario_10_workspace(tmp_path)
    scope = _scenario_10_scope()
    assert ".env" in scope.blocked_paths

    staging_root, copy = docker_job.stage_workspace_copy(ws, scope.blocked_paths)
    try:
        assert not (copy / ".env").exists()
        assert not (copy / ".env").is_symlink()
        assert (copy / "auth.py").is_file()
        assert (copy / "tests" / "test_auth.py").is_file()
        assert (copy / "README.md").is_file()
        # The source workspace is untouched.
        assert SECRET_MARKER in (ws / ".env").read_text()
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def test_staging_without_blocked_paths_keeps_previous_behaviour(tmp_path: Path) -> None:
    ws = _scenario_10_workspace(tmp_path)
    staging_root, copy = docker_job.stage_workspace_copy(ws)
    try:
        assert (copy / ".env").is_file()
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


@pytest.mark.parametrize(
    "blocked",
    [["secrets"], ["./secrets/"], ["secrets/key.txt"], ["secrets\\key.txt"]],
)
def test_staging_excludes_nested_blocked_prefixes(tmp_path: Path, blocked: list[str]) -> None:
    ws = tmp_path / "ws"
    (ws / "secrets").mkdir(parents=True)
    (ws / "secrets" / "key.txt").write_text(SECRET_MARKER)
    (ws / "src").mkdir()
    (ws / "src" / "main.py").write_text("print('hi')\n")

    staging_root, copy = docker_job.stage_workspace_copy(ws, blocked)
    try:
        assert not (copy / "secrets" / "key.txt").exists()
        assert (copy / "src" / "main.py").is_file()
        for path in copy.rglob("*"):
            if path.is_file():
                assert SECRET_MARKER not in path.read_text(errors="replace")
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def test_staging_excludes_symlinks_whose_target_is_blocked(tmp_path: Path) -> None:
    """Excluded by resolved target, not just by requested name."""
    ws = _scenario_10_workspace(tmp_path)
    (ws / "tests" / "env-alias").symlink_to("../.env")
    (ws / "abs-alias").symlink_to(ws / ".env")
    (ws / "secrets").mkdir()
    (ws / "secrets" / "key.txt").write_text(SECRET_MARKER)
    (ws / "dir-alias").symlink_to("secrets", target_is_directory=True)
    (ws / "ok-link.py").symlink_to("auth.py")

    staging_root, copy = docker_job.stage_workspace_copy(ws, [".env", "secrets"])
    try:
        for name in ("tests/env-alias", "abs-alias", "dir-alias", ".env", "secrets"):
            assert not (copy / name).is_symlink(), name
            assert not (copy / name).exists(), name
        # A link to an allowed file survives as a link.
        assert (copy / "ok-link.py").is_symlink()
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def test_staging_refuses_a_scope_that_blocks_the_whole_workspace(tmp_path: Path) -> None:
    ws = _scenario_10_workspace(tmp_path)
    before = set(Path(tmp_path).iterdir())
    with pytest.raises(OSError):
        docker_job.stage_workspace_copy(ws, ["."])
    assert set(Path(tmp_path).iterdir()) == before


def test_staging_fails_closed_when_exclusion_cannot_be_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a blocked path still exists in the copy, staging raises and cleans up."""
    ws = _scenario_10_workspace(tmp_path)
    real_copytree = shutil.copytree

    def leaky_copytree(src, dst, *args, **kwargs):  # ignores the filter
        kwargs.pop("ignore", None)
        return real_copytree(src, dst, *args, **kwargs)

    monkeypatch.setattr(docker_job.shutil, "copytree", leaky_copytree)
    created: list[Path] = []
    real_mkdtemp = docker_job.tempfile.mkdtemp

    def tracking_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(Path(path))
        return path

    monkeypatch.setattr(docker_job.tempfile, "mkdtemp", tracking_mkdtemp)
    with pytest.raises(OSError):
        docker_job.stage_workspace_copy(ws, [".env"])
    assert created and not created[0].exists()


@pytest.mark.parametrize("bad", [[1], [None], "not-a-list-of-str", [["nested"]]])
def test_staging_rejects_malformed_blocked_paths(tmp_path: Path, bad: object) -> None:
    ws = _scenario_10_workspace(tmp_path)
    with pytest.raises(OSError):
        docker_job.stage_workspace_copy(ws, bad)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# sync_copy_back
# ---------------------------------------------------------------------------


def test_copy_back_refuses_writes_under_blocked_prefixes(tmp_path: Path) -> None:
    """Acceptance 2: a container writing to a blocked path changes nothing."""
    ws = _scenario_10_workspace(tmp_path)
    (ws / "secrets").mkdir()
    (ws / "secrets" / "key.txt").write_text("host-original")
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / ".env").write_text("OVERWRITTEN_BY_CONTAINER")
    (staged / "secrets").mkdir()
    (staged / "secrets" / "key.txt").write_text("OVERWRITTEN_BY_CONTAINER")
    (staged / "secrets" / "new.txt").write_text("planted")
    (staged / "auth.py").write_text("fixed\n")

    refused = docker_job.sync_copy_back(staged, ws, [".env", "secrets"])

    assert (ws / ".env").read_text() == f"API_KEY={SECRET_MARKER}\n"
    assert (ws / "secrets" / "key.txt").read_text() == "host-original"
    assert not (ws / "secrets" / "new.txt").exists()
    # Non-blocked writes still land.
    assert (ws / "auth.py").read_text() == "fixed\n"
    assert set(refused) == {".env", "secrets"}


def test_copy_back_does_not_create_a_blocked_path_that_did_not_exist(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / ".env").write_text("planted")
    (staged / "link").symlink_to(".env")

    refused = docker_job.sync_copy_back(staged, ws, [".env"])

    assert not (ws / ".env").exists()
    assert ".env" in refused


def test_copy_back_does_not_write_through_a_host_link_into_a_blocked_dir(
    tmp_path: Path,
) -> None:
    """Resolved destination is checked: a host-side alias cannot be written through."""
    ws = tmp_path / "ws"
    (ws / "secrets").mkdir(parents=True)
    (ws / "secrets" / "key.txt").write_text("host-original")
    (ws / "alias").symlink_to("secrets", target_is_directory=True)
    staged = tmp_path / "staged"
    (staged / "alias").mkdir(parents=True)
    (staged / "alias" / "key.txt").write_text("OVERWRITTEN_BY_CONTAINER")

    refused = docker_job.sync_copy_back(staged, ws, ["secrets"])

    assert (ws / "secrets" / "key.txt").read_text() == "host-original"
    assert (ws / "alias").is_symlink()
    assert "alias" in refused


def test_copy_back_does_not_write_through_a_host_link_outside_the_workspace(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.txt").write_text("host-original")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "escape").symlink_to(outside, target_is_directory=True)
    staged = tmp_path / "staged"
    (staged / "escape").mkdir(parents=True)
    (staged / "escape" / "victim.txt").write_text("OVERWRITTEN_BY_CONTAINER")

    refused = docker_job.sync_copy_back(staged, ws)

    assert (outside / "victim.txt").read_text() == "host-original"
    assert "escape" in refused


@pytest.mark.parametrize("replacement", ["symlink", "file"])
def test_copy_back_cannot_delete_a_blocked_path_by_replacing_its_ancestor(
    tmp_path: Path, replacement: str
) -> None:
    """Second-pass review (#183): the container replaces ``config/`` (which
    holds blocked ``config/.env``) with a link or a file. Copy-back used to
    rmtree the host ``config/`` to apply the type change, deleting the
    blocked file; it must refuse instead and report the refusal."""
    ws = tmp_path / "ws"
    (ws / "config").mkdir(parents=True)
    (ws / "config" / ".env").write_text(f"API_KEY={SECRET_MARKER}\n")
    (ws / "config" / "app.toml").write_text("ok")
    staging_root, staged = docker_job.stage_workspace_copy(ws, ["config/.env"])
    try:
        shutil.rmtree(staged / "config")
        if replacement == "symlink":
            (staged / "config").symlink_to("elsewhere")
        else:
            (staged / "config").write_text("replaced")

        refused = docker_job.sync_copy_back(staged, ws, ["config/.env"])
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)

    assert (ws / "config" / ".env").read_text() == f"API_KEY={SECRET_MARKER}\n"
    assert (ws / "config" / "app.toml").read_text() == "ok"
    assert refused == ["config"]


# ---------------------------------------------------------------------------
# End to end through the gateway DockerExecutor (Docker faked)
# ---------------------------------------------------------------------------


def _fake_pytest_container(monkeypatch: pytest.MonkeyPatch, module, seen: dict) -> None:
    """Fake ``docker run`` that behaves like pytest collecting conftest.py.

    The planted conftest does ``print(open('.env').read())``; the fake runs
    that effect against the mounted staged copy and returns its stdout in the
    helper payload, exactly as the real helper would.
    """
    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[:2] == ["docker", "info"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        if isinstance(cmd, list) and cmd[:2] == ["docker", "run"]:
            copy = Path(cmd[cmd.index("-v") + 1].split(":")[0])
            seen["listing"] = sorted(p.name for p in copy.iterdir())
            assert (copy / "tests" / "conftest.py").is_file()
            try:
                stdout = (copy / ".env").read_text()
            except FileNotFoundError:
                stdout = "FileNotFoundError: .env\n"
            # The container also tries to plant a blocked file.
            (copy / ".env").write_text("PLANTED_BY_CONTAINER")
            payload = {
                "status": "EXECUTED",
                "result": {
                    "operation": "run_command",
                    "argv": ["python", "-m", "pytest", "-s"],
                    "exit_code": 0,
                    "stdout": stdout,
                    "stderr": "",
                },
                "error_code": None,
            }
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps(payload).encode(), stderr=b""
            )
        if isinstance(cmd, list) and cmd[:3] == ["docker", "rm", "-f"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", fake_run)


def test_scenario_10_conftest_cannot_read_env_through_docker_executor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Acceptance 3: the issue's repro, end to end through policy and executor."""
    import scopewatch.executor_docker as docker_mod
    from scopewatch.policy import evaluate_policy

    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    ws = _scenario_10_workspace(tmp_path)
    scope = _scenario_10_scope()
    run = Run(
        id="run-183", name="scenario 10", task_scope=scope,
        created_at=_now(), updated_at=_now(),
    )
    # Step 1 of the repro: the agent writes a conftest that reads .env.
    (ws / "tests" / "conftest.py").write_text("print(open('.env').read())\n")

    action = _run_command_action()
    decision = evaluate_policy(action, run, ws)
    assert decision.outcome == PolicyOutcome.ALLOW, decision

    seen: dict = {}
    _fake_pytest_container(monkeypatch, docker_mod, seen)
    monkeypatch.setattr(docker_mod, "is_docker_available", lambda *a, **k: True)
    receipt = DockerExecutor().execute(
        action, ws, policy_decision=decision, task_scope=scope
    )

    assert receipt.status == ExecutionStatus.EXECUTED, receipt.sanitized_result
    assert ".env" not in seen["listing"]
    assert SECRET_MARKER not in json.dumps(receipt.sanitized_result)
    # The container's attempt to plant .env never reaches the host.
    assert (ws / ".env").read_text() == f"API_KEY={SECRET_MARKER}\n"
    assert receipt.sanitized_result.get("copy_back_refused") == [".env"]


def test_docker_executor_uses_stored_scope_when_none_is_passed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With only a store handle, staging still excludes the stored blocked paths."""
    import scopewatch.executor_docker as docker_mod

    ws = _scenario_10_workspace(tmp_path)
    (ws / "tests" / "conftest.py").write_text("print(open('.env').read())\n")
    scope = _scenario_10_scope()
    monkeypatch.setattr(docker_mod, "_load_scope_from_store", lambda action, db: scope)
    monkeypatch.setattr(
        "scopewatch.executor._verify_stored_run_not_terminal", lambda action, db: None
    )
    seen: dict = {}
    _fake_pytest_container(monkeypatch, docker_mod, seen)
    monkeypatch.setattr(docker_mod, "is_docker_available", lambda *a, **k: True)

    action = _run_command_action()
    receipt = DockerExecutor().execute(
        action, ws, policy_decision=_decision(action.id), db_path=tmp_path / "unused.db"
    )

    assert receipt.status == ExecutionStatus.EXECUTED, receipt.sanitized_result
    assert ".env" not in seen["listing"]
    assert SECRET_MARKER not in json.dumps(receipt.sanitized_result)


# ---------------------------------------------------------------------------
# Runner sidecar (same shared code, scope from the signed dispatch)
# ---------------------------------------------------------------------------


def _load_runner():
    spec = importlib.util.spec_from_file_location("executor_runner_183", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runner_body(runner_mod, run_workspace: str, task_scope: object, key: str) -> dict:
    from scopewatch.executor_remote import sign_dispatch_payload

    action = {
        "id": "action-183",
        "run_id": "run-183",
        "operation": "run_command",
        "resource": ".",
        "arguments": {"argv": ["python", "-m", "pytest", "-s"]},
    }
    decision = {"id": "d-183", "action_request_id": "action-183", "outcome": "ALLOW"}
    body = {
        "issued_at": time.time(),
        "dispatch_token": f"token-{uuid.uuid4().hex}",
        "run_workspace": run_workspace,
        # Since #184 the digest binds the signed scope too. A non-mapping
        # scope cannot be digested; the runner refuses it before digesting.
        "action_digest": runner_mod.canonical_action_digest(
            action,
            decision,
            run_workspace,
            task_scope if isinstance(task_scope, dict) else None,
        ),
        "action": action,
        "policy_decision": decision,
        "approval": None,
        "task_scope": task_scope,
    }
    body["dispatch_signature"] = sign_dispatch_payload(body, key)
    return body


def _runner_config(runner_mod, tmp_path: Path, run_workspace: str):
    config = runner_mod.RunnerConfig()
    config.token = "synthetic"
    config.signing_key = "synthetic-independent-signing-key"
    config.workspace = tmp_path / "volume"
    ws = _scenario_10_workspace(tmp_path / "src")
    shutil.copytree(ws, config.workspace / run_workspace, symlinks=True)
    (config.workspace / run_workspace / "tests" / "conftest.py").write_text(
        "print(open('.env').read())\n"
    )
    return config


def test_runner_staging_excludes_blocked_paths_from_signed_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner_mod = _load_runner()
    run_workspace = "run-183-ws"
    config = _runner_config(runner_mod, tmp_path, run_workspace)
    seen: dict = {}
    _fake_pytest_container(monkeypatch, runner_mod, seen)
    scope = _scenario_10_scope()
    body = _runner_body(
        runner_mod,
        run_workspace,
        {
            "allowed_paths": scope.allowed_paths,
            "blocked_paths": scope.blocked_paths,
            "allowed_commands": scope.allowed_commands,
        },
        config.signing_key,
    )

    status, payload = runner_mod.handle_execute(body, config, runner_mod.DispatchTokenStore())

    assert status == 200, payload
    assert payload["status"] == "EXECUTED", payload
    assert ".env" not in seen["listing"]
    assert SECRET_MARKER not in json.dumps(payload)
    host_env = config.workspace / run_workspace / ".env"
    assert host_env.read_text() == f"API_KEY={SECRET_MARKER}\n"
    assert payload["result"].get("copy_back_refused") == [".env"]


@pytest.mark.parametrize(
    "bad_scope",
    [
        {"blocked_paths": ".env"},
        {"blocked_paths": [1]},
        "not-a-mapping",
        ["x"],
        # #184 fields, digested since that merge: also shape-checked.
        {"allowed_paths": "src"},
        {"allowed_commands": "cat"},
        {"allowed_commands": [["cat", 1]]},
    ],
)
def test_runner_refuses_malformed_signed_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad_scope: object
) -> None:
    runner_mod = _load_runner()
    run_workspace = "run-183-ws"
    config = _runner_config(runner_mod, tmp_path, run_workspace)
    called: dict = {}
    _fake_pytest_container(monkeypatch, runner_mod, called)
    body = _runner_body(runner_mod, run_workspace, bad_scope, config.signing_key)

    status, payload = runner_mod.handle_execute(body, config, runner_mod.DispatchTokenStore())

    assert 400 <= status < 500, (status, payload)
    assert "listing" not in called  # never reached docker run

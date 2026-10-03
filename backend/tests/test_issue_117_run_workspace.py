"""Per-run workspace isolation (issue #117).

Regression coverage for the gap where every run executed against one shared
writable fixture root: run A could read run B's generated files, and Docker
copy-back wrote container output into the shared tree.

Proven here, against the real service with temporary databases and synthetic
workspaces:

- Runs A and B start from separate copies of the fixture.
- A write in A is invisible to B, and neither changes the source fixture.
- Copy-back (Docker) is confined to the dispatched run's workspace.
- An approved HOLD still finds the original run's workspace after a service
  restart, because the identity is stored on the run.

Docker and remote paths are exercised through their contract boundaries with
a mocked CLI / mocked transport. No live Docker daemon was used.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import uuid

import pytest

from scopewatch.db import init_db
from scopewatch.executor import ExecutionSecurityError, execute_action
from scopewatch.models import ExecutionStatus, PolicyOutcome
from scopewatch.schemas import (
    PolicyDecision,
    SubmitActionRequest,
    TaskScope,
)
from scopewatch.service import ScopewatchService
from scopewatch.workspaces import (
    RunWorkspaceError,
    RunWorkspaceManager,
    WORKSPACE_MARKER_NAME,
)

RUN_A_CONTENT = "SYNTHETIC RUN A CONTENT"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _seed_fixture(root: Path) -> Path:
    """Create a small synthetic scenario fixture. Never real data."""
    (root / "invoices" / "approved").mkdir(parents=True)
    (root / "outputs").mkdir(parents=True)
    (root / "invoices" / "approved" / "vendor-a.txt").write_text(
        "INVOICE #INV-2026-001\nAmount: $4,500.00\n", encoding="utf-8"
    )
    (root / "outputs" / "archive.txt").write_text(
        "Legacy archived summary.\n", encoding="utf-8"
    )
    return root


def _scope(**overrides: object) -> TaskScope:
    data: dict[str, object] = {
        "task_description": "Synthetic demo run over an isolated workspace copy.",
        "allowed_paths": ["invoices", "outputs"],
        "blocked_paths": ["invoices/private"],
        "allowed_tools": ["workspace"],
        "allowed_operations": ["read_text", "write_text", "list_directory"],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    data.update(overrides)
    return TaskScope(**data)  # type: ignore[arg-type]


def _service(tmp_path: Path, fixture: Path | None = None) -> ScopewatchService:
    """A service over a temp DB and a temp fixture, with a temp run-workspace root."""
    ws = fixture if fixture is not None else _seed_fixture(tmp_path / "workspace")
    db_path = tmp_path / "scopewatch.db"
    init_db(db_path)
    return ScopewatchService(
        db_path=db_path,
        workspace_root=ws,
        run_workspaces_root=tmp_path / "run-workspaces",
    )


def _submit(
    service: ScopewatchService,
    run_id: str,
    operation: str,
    resource: str,
    **arguments: object,
) -> object:
    request = SubmitActionRequest(
        tool="workspace",
        operation=operation,
        resource=resource,
        arguments=dict(arguments),
    )
    return asyncio.run(service.submit_action(run_id, request))


def _allow_decision(action_id: str = "action-1") -> PolicyDecision:
    from scopewatch.models import ReasonCode

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


# --------------------------------------------------------------------------
# Runs start from separate fixture copies
# --------------------------------------------------------------------------


def test_runs_get_separate_workspace_copies(tmp_path: Path) -> None:
    service = _service(tmp_path)
    fixture = service.workspace_root

    run_a, _ = service.create_run(name="run-a", task_scope=_scope())
    run_b, _ = service.create_run(name="run-b", task_scope=_scope())

    ws_a = service.get_run_workspace(run_a.id)
    ws_b = service.get_run_workspace(run_b.id)

    assert ws_a != ws_b
    # Both are distinct directories, neither is the shared fixture.
    assert ws_a.is_dir() and ws_b.is_dir()
    assert ws_a.resolve() != fixture.resolve()
    assert ws_b.resolve() != fixture.resolve()
    assert fixture.resolve() not in (ws_a.resolve().parents, ws_b.resolve().parents)
    # Each carries the fixture baseline.
    assert (ws_a / "invoices" / "approved" / "vendor-a.txt").is_file()
    assert (ws_b / "invoices" / "approved" / "vendor-a.txt").is_file()
    # Mutating one copy does not change the other or the fixture.
    (ws_a / "outputs" / "only-in-a.txt").write_text("a", encoding="utf-8")
    assert not (ws_b / "outputs" / "only-in-a.txt").exists()
    assert not (fixture / "outputs" / "only-in-a.txt").exists()


def test_run_workspace_identity_is_persisted_on_the_run(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run, events = service.create_run(name="persisted", task_scope=_scope())

    assert run.workspace_path is not None
    # Read back through a fresh service instance over the same database: the
    # identity is stored, not held in process memory.
    reloaded = ScopewatchService(
        db_path=service.db_path,
        workspace_root=service.workspace_root,
        run_workspaces_root=service.run_workspaces.root,
    )
    stored = reloaded.get_run(run.id)
    assert stored.workspace_path == run.workspace_path
    assert reloaded.get_run_workspace(run.id) == Path(run.workspace_path)

    # The RUN_CREATED event names the workspace key but never the fixture path.
    created = [e for e in events if e.event_type.value == "RUN_CREATED"][0]
    assert created.details["workspace_isolated"] is True
    assert created.details["run_workspace"] == run.id
    assert str(service.workspace_root) not in json.dumps(created.details)


def test_run_workspace_manager_is_idempotent_across_restart(tmp_path: Path) -> None:
    """A second manager over the same root must not re-copy the fixture."""
    fixture = _seed_fixture(tmp_path / "workspace")
    first = RunWorkspaceManager(fixture, tmp_path / "runs")
    ws = first.initialize("run-x")
    (ws / "outputs" / "produced.txt").write_text("kept", encoding="utf-8")

    second = RunWorkspaceManager(fixture, tmp_path / "runs")
    again = second.initialize("run-x")

    assert again == ws
    assert (again / "outputs" / "produced.txt").read_text(encoding="utf-8") == "kept"
    marker = second.read_marker(again)
    assert marker is not None and marker["run_id"] == "run-x"


def test_run_workspace_refuses_paths_outside_the_managed_root(tmp_path: Path) -> None:
    """A stored row must not be able to point execution at an arbitrary dir."""
    fixture = _seed_fixture(tmp_path / "workspace")
    manager = RunWorkspaceManager(fixture, tmp_path / "runs")
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(RunWorkspaceError):
        manager.resolve("run-x", stored_path=str(outside))


def test_run_workspace_key_rejects_traversal(tmp_path: Path) -> None:
    manager = RunWorkspaceManager(tmp_path / "workspace", tmp_path / "runs")
    for bad in ("../escape", "a/b", "..", "", "run x", "-leading"):
        with pytest.raises(RunWorkspaceError):
            manager.path_for(bad)


# --------------------------------------------------------------------------
# Cross-run reads fail; the fixture stays pristine
# --------------------------------------------------------------------------


def test_run_b_cannot_read_run_a_output(tmp_path: Path) -> None:
    """The gap from #117: run B read run A's generated file byte for byte."""
    service = _service(tmp_path)
    fixture = service.workspace_root

    run_a, _ = service.create_run(name="run-a", task_scope=_scope())
    run_b, _ = service.create_run(name="run-b", task_scope=_scope())

    written = _submit(
        service, run_a.id, "write_text", "outputs/shared.txt", content=RUN_A_CONTENT
    )
    assert written.execution_receipt.status == ExecutionStatus.EXECUTED

    # Run B's copy of the fixture never saw that file.
    read_b = _submit(service, run_b.id, "read_text", "outputs/shared.txt")
    assert read_b.execution_receipt.status == ExecutionStatus.FAILED
    assert not (
        service.get_run_workspace(run_b.id) / "outputs" / "shared.txt"
    ).exists()

    # Run A still reads its own file, and the fixture is untouched.
    read_a = _submit(service, run_a.id, "read_text", "outputs/shared.txt")
    assert read_a.execution_receipt.status == ExecutionStatus.EXECUTED
    assert read_a.execution_receipt.sanitized_result["preview"] == RUN_A_CONTENT
    assert not (fixture / "outputs" / "shared.txt").exists()
    assert not (fixture / WORKSPACE_MARKER_NAME).exists()


def test_run_write_does_not_alter_the_source_fixture(tmp_path: Path) -> None:
    service = _service(tmp_path)
    fixture = service.workspace_root
    before = {
        p.relative_to(fixture): p.read_bytes()
        for p in sorted(fixture.rglob("*"))
        if p.is_file()
    }

    run, _ = service.create_run(name="mutator", task_scope=_scope())
    result = _submit(
        service, run.id, "write_text", "invoices/approved/vendor-a.txt",
        content="TAMPERED",
    )
    assert result.execution_receipt.status == ExecutionStatus.EXECUTED

    after = {
        p.relative_to(fixture): p.read_bytes()
        for p in sorted(fixture.rglob("*"))
        if p.is_file()
    }
    assert before == after


# --------------------------------------------------------------------------
# Approved HOLD finds the original run's workspace after a restart
# --------------------------------------------------------------------------


def test_approved_hold_uses_original_run_workspace_after_restart(tmp_path: Path) -> None:
    fixture = _seed_fixture(tmp_path / "workspace")
    scope = _scope(requires_approval=["write_text"], allowed_operations=[
        "read_text", "write_text", "list_directory"
    ])

    service = _service(tmp_path, fixture)
    run, _ = service.create_run(name="held", task_scope=scope)
    held = _submit(
        service, run.id, "write_text", "outputs/after-approval.txt", content="approved"
    )
    assert held.policy_decision.outcome == PolicyOutcome.HOLD
    approval_id = held.approval_request.id

    # A second run exists and writes to the same relative path, so approving
    # against the shared root would be observable.
    other, _ = service.create_run(name="other", task_scope=_scope())
    _submit(
        service, other.id, "write_text", "outputs/after-approval.txt",
        content="OTHER RUN CONTENT",
    )

    # Restart: a brand new service object over the same database and disk.
    restarted = ScopewatchService(
        db_path=service.db_path,
        workspace_root=fixture,
        run_workspaces_root=service.run_workspaces.root,
    )
    resolved = asyncio.run(
        restarted.resolve_approval(approval_id=approval_id, approve=True)
    )

    assert resolved.execution_receipt.status == ExecutionStatus.EXECUTED
    original_ws = Path(run.workspace_path)
    assert (
        original_ws / "outputs" / "after-approval.txt"
    ).read_text(encoding="utf-8") == "approved"
    other_ws = restarted.get_run_workspace(other.id)
    assert (
        other_ws / "outputs" / "after-approval.txt"
    ).read_text(encoding="utf-8") == "OTHER RUN CONTENT"
    # Neither workspace nor the fixture absorbed the other's file.
    assert not (fixture / "outputs" / "after-approval.txt").exists()


def test_run_with_unresolvable_workspace_fails_closed(tmp_path: Path) -> None:
    """Missing stored workspace refuses execution; no silent shared-root fallback."""
    service = _service(tmp_path)
    run, _ = service.create_run(name="vanishing", task_scope=_scope())
    shutil.rmtree(Path(run.workspace_path))

    from scopewatch.errors import ScopewatchAPIError

    with pytest.raises(ScopewatchAPIError) as excinfo:
        _submit(service, run.id, "read_text", "outputs/archive.txt")
    assert excinfo.value.code == "RUN_WORKSPACE_UNAVAILABLE"


# --------------------------------------------------------------------------
# Container-dispatch boundary: copy-back confinement
# --------------------------------------------------------------------------


def _mock_docker_cli(monkeypatch: pytest.MonkeyPatch, on_staged) -> None:
    """Mock the Docker CLI; `on_staged` mutates the staged copy like a container."""
    import scopewatch.executor_docker as docker_mod

    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[:2] == ["docker", "info"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        if isinstance(cmd, list) and cmd[:2] == ["docker", "run"]:
            mount = None
            for i, part in enumerate(cmd):
                if part == "-v" and i + 1 < len(cmd):
                    mount = Path(cmd[i + 1].split(":")[0])
                    break
            assert mount is not None and mount.is_dir()
            on_staged(mount)
            payload = {
                "status": "EXECUTED",
                "result": {"operation": "write_text", "bytes_written": 1},
                "error_code": None,
            }
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps(payload).encode(), stderr=b""
            )
        if isinstance(cmd, list) and cmd[:3] == ["docker", "rm", "-f"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(docker_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(docker_mod, "is_docker_available", lambda *a, **k: True)


def test_docker_copy_back_is_confined_to_the_dispatched_run_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Container writes sync into the run's workspace, not the fixture."""
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    fixture = _seed_fixture(tmp_path / "workspace")
    service = _service(tmp_path, fixture)
    run_a, _ = service.create_run(name="a", task_scope=_scope())
    run_b, _ = service.create_run(name="b", task_scope=_scope())
    ws_a = service.get_run_workspace(run_a.id)
    ws_b = service.get_run_workspace(run_b.id)

    def container_write(staged: Path) -> None:
        (staged / "outputs" / "from-container.txt").write_text(
            RUN_A_CONTENT, encoding="utf-8"
        )

    _mock_docker_cli(monkeypatch, container_write)

    from scopewatch.executor_docker import DockerExecutor
    from scopewatch.models import ReasonCode
    from scopewatch.schemas import ActionRequest

    action_req = ActionRequest(
        id="action-docker-1",
        run_id=run_a.id,
        tool="workspace",
        operation="write_text",
        resource="outputs/from-container.txt",
        arguments={"content": RUN_A_CONTENT},
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    decision = PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id=action_req.id,
        outcome=PolicyOutcome.ALLOW,
        reason_code=ReasonCode.ALLOWED_TOOL_AND_RESOURCE,
        explanation="Permitted",
        matched_rule="RULE_ALLOWED",
        decided_at=datetime.now(timezone.utc).isoformat(),
        deterministic=True,
    )

    receipt = DockerExecutor().execute(action_req, ws_a, policy_decision=decision)

    assert receipt.status == ExecutionStatus.EXECUTED, receipt.sanitized_result
    assert (
        ws_a / "outputs" / "from-container.txt"
    ).read_text(encoding="utf-8") == RUN_A_CONTENT
    # Run B's workspace and the fixture are both untouched.
    assert not (ws_b / "outputs" / "from-container.txt").exists()
    assert not (fixture / "outputs" / "from-container.txt").exists()


def test_docker_staging_mounts_a_copy_not_the_run_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The mount is a per-dispatch copy, so container churn cannot rewrite the run tree."""
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    service = _service(tmp_path)
    run, _ = service.create_run(name="staged", task_scope=_scope())
    ws = service.get_run_workspace(run.id)

    seen: dict[str, object] = {}

    def container_write(staged: Path) -> None:
        seen["staged"] = staged
        (staged / "outputs" / "marker.txt").write_text("container", encoding="utf-8")

    _mock_docker_cli(monkeypatch, container_write)

    from scopewatch.executor_docker import DockerExecutor
    from scopewatch.models import ReasonCode
    from scopewatch.schemas import ActionRequest

    action_req = ActionRequest(
        id="action-docker-2",
        run_id=run.id,
        tool="workspace",
        operation="write_text",
        resource="outputs/marker.txt",
        arguments={"content": "container"},
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    decision = PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id=action_req.id,
        outcome=PolicyOutcome.ALLOW,
        reason_code=ReasonCode.ALLOWED_TOOL_AND_RESOURCE,
        explanation="Permitted",
        matched_rule="RULE_ALLOWED",
        decided_at=datetime.now(timezone.utc).isoformat(),
        deterministic=True,
    )

    receipt = DockerExecutor().execute(action_req, ws, policy_decision=decision)

    assert receipt.status == ExecutionStatus.EXECUTED, receipt.sanitized_result
    staged = Path(str(seen["staged"]))
    assert staged.resolve() != ws.resolve()
    # Copy-back put the container's write into the run workspace, and the
    # staging directory is cleaned up afterwards.
    assert (ws / "outputs" / "marker.txt").read_text(encoding="utf-8") == "container"
    assert not staged.exists()


# --------------------------------------------------------------------------
# Remote contract carries the run's workspace identity
# --------------------------------------------------------------------------


def test_remote_dispatch_carries_the_run_workspace_key(tmp_path: Path) -> None:
    import httpx

    from scopewatch.executor_remote import (
        RemoteExecutor,
        compute_action_digest,
        sign_dispatch_payload,
    )
    from scopewatch.models import ReasonCode
    from scopewatch.schemas import ActionRequest

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.read().decode())
        return httpx.Response(
            200, json={"status": "EXECUTED", "result": {}, "error_code": None}
        )

    service = _service(tmp_path)
    run, _ = service.create_run(name="remote", task_scope=_scope())
    ws = service.get_run_workspace(run.id)

    action_req = ActionRequest(
        id="action-remote-1",
        run_id=run.id,
        tool="workspace",
        operation="write_text",
        resource="outputs/remote.txt",
        arguments={"content": RUN_A_CONTENT},
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    decision = PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id=action_req.id,
        outcome=PolicyOutcome.ALLOW,
        reason_code=ReasonCode.ALLOWED_TOOL_AND_RESOURCE,
        explanation="Permitted",
        matched_rule="RULE_ALLOWED",
        decided_at=datetime.now(timezone.utc).isoformat(),
        deterministic=True,
    )

    executor = RemoteExecutor(
        runner_url="http://runner.invalid:8091",
        runner_token="synthetic-runner-token",
        transport=httpx.MockTransport(handler),
    )
    executor._signing_key = "synthetic-independent-signing-key"
    receipt = executor.execute(action_req, ws, policy_decision=decision)

    assert receipt.status == ExecutionStatus.EXECUTED, receipt.sanitized_result
    body = seen["body"]
    assert isinstance(body, dict)
    # The runner is told which run's workspace to use...
    assert body["run_workspace"] == run.id
    # ...and the key is authenticated, so it cannot be swapped in flight.
    assert body["dispatch_signature"] == sign_dispatch_payload(
        body, executor._signing_key
    )
    # The digest binds the workspace identity too.
    assert body["action_digest"] == compute_action_digest(
        action_req, decision, action_req.arguments, run_workspace=run.id
    )
    assert compute_action_digest(action_req, decision, action_req.arguments) != (
        body["action_digest"]
    )


def test_remote_dispatch_workspace_key_defaults_to_the_run_id(tmp_path: Path) -> None:
    """Without an explicit key the executor names the run, never the volume root.

    The key is derived from ``action.run_id`` rather than from the workspace
    directory name it was handed, so handing the executor the shared fixture
    root cannot make it announce ``workspace`` as the runner's run directory.
    """
    import httpx

    from scopewatch.executor_remote import RemoteExecutor, prepare_dispatch
    from scopewatch.models import ReasonCode
    from scopewatch.schemas import ActionRequest

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.read().decode())
        return httpx.Response(
            200, json={"status": "EXECUTED", "result": {}, "error_code": None}
        )

    service = _service(tmp_path)
    run, _ = service.create_run(name="remote-nokey", task_scope=_scope())

    action_req = ActionRequest(
        id="action-remote-2",
        run_id=run.id,
        tool="workspace",
        operation="read_text",
        resource="outputs/archive.txt",
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    decision = PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id=action_req.id,
        outcome=PolicyOutcome.ALLOW,
        reason_code=ReasonCode.ALLOWED_TOOL_AND_RESOURCE,
        explanation="Permitted",
        matched_rule="RULE_ALLOWED",
        decided_at=datetime.now(timezone.utc).isoformat(),
        deterministic=True,
    )

    # A caller that builds a dispatch by hand gets an explicitly absent key,
    # never a silent shared-root fallback.
    payload, _digest = prepare_dispatch(action_req, decision)
    assert payload["run_workspace"] is None

    executor = RemoteExecutor(
        runner_url="http://runner.invalid:8091",
        runner_token="synthetic-runner-token",
        transport=httpx.MockTransport(handler),
    )
    executor._signing_key = "synthetic-independent-signing-key"
    # Deliberately hand it the shared fixture root: the announced key is still
    # the run, which the runner resolves to its own directory.
    executor.execute(action_req, service.workspace_root, policy_decision=decision)
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["run_workspace"] == run.id
    assert body["run_workspace"] != service.workspace_root.name


def test_runner_refuses_to_fall_back_to_the_shared_root(tmp_path: Path) -> None:
    """Runner side: a missing or unsafe run_workspace fails closed (500)."""
    import importlib.util

    runner_path = (
        Path(__file__).resolve().parents[2] / "deploy" / "executor-runner" / "runner.py"
    )
    spec = importlib.util.spec_from_file_location("scopewatch_runner_117", runner_path)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)

    root = tmp_path / "runner-ws"
    (root / "run-a").mkdir(parents=True)
    (root / "run-b").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape-link").symlink_to(outside)

    resolved, reason = runner.resolve_run_workspace(root, "run-a")
    assert reason is None and resolved == (root / "run-a").resolve()

    # Unknown run, missing key, traversal, and a symlink all fail closed.
    for bad in ("run-missing", None, "../escape", "a/b", "", "escape-link", 7):
        got, why = runner.resolve_run_workspace(root, bad)  # type: ignore[arg-type]
        assert got is None, bad
        assert why == "run workspace unavailable", bad

    # The mounted root itself is never accepted as a run workspace.
    got, why = runner.resolve_run_workspace(root, root.name)
    assert got is None and why == "run workspace unavailable"


def test_runner_digest_covers_the_workspace_key(tmp_path: Path) -> None:
    """Gateway and runner agree on the digest including run_workspace."""
    import importlib.util

    from scopewatch.executor_remote import compute_action_digest

    runner_path = (
        Path(__file__).resolve().parents[2] / "deploy" / "executor-runner" / "runner.py"
    )
    spec = importlib.util.spec_from_file_location("scopewatch_runner_117b", runner_path)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)

    from scopewatch.models import ReasonCode
    from scopewatch.schemas import ActionRequest

    action_req = ActionRequest(
        id="a1",
        run_id="run-a",
        tool="workspace",
        operation="read_text",
        resource="outputs/archive.txt",
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    decision = PolicyDecision(
        id="d1",
        action_request_id="a1",
        outcome=PolicyOutcome.ALLOW,
        reason_code=ReasonCode.ALLOWED_TOOL_AND_RESOURCE,
        explanation="Permitted",
        matched_rule="RULE_ALLOWED",
        decided_at=datetime.now(timezone.utc).isoformat(),
        deterministic=True,
    )

    gateway = compute_action_digest(
        action_req, decision, action_req.arguments, run_workspace="run-a"
    )
    runner_side = runner.canonical_action_digest(
        {
            "id": action_req.id,
            "run_id": action_req.run_id,
            "operation": action_req.operation,
            "resource": action_req.resource,
            "arguments": action_req.arguments,
        },
        {"id": decision.id, "outcome": decision.outcome.value},
        "run-a",
    )
    assert gateway == runner_side
    # A different run's workspace yields a different digest.
    other = runner.canonical_action_digest(
        {
            "id": action_req.id,
            "run_id": action_req.run_id,
            "operation": action_req.operation,
            "resource": action_req.resource,
            "arguments": action_req.arguments,
        },
        {"id": decision.id, "outcome": decision.outcome.value},
        "run-b",
    )
    assert gateway != other


# --------------------------------------------------------------------------
# Executor guard rails still hold with the new argument
# --------------------------------------------------------------------------


def test_local_executor_still_refuses_execution_without_a_decision(
    tmp_path: Path,
) -> None:
    from scopewatch.schemas import ActionRequest

    service = _service(tmp_path)
    run, _ = service.create_run(name="no-evidence", task_scope=_scope())
    ws = service.get_run_workspace(run.id)
    action_req = ActionRequest(
        id="a-nodecision",
        run_id=run.id,
        tool="workspace",
        operation="write_text",
        resource="outputs/x.txt",
        arguments={"content": "x"},
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    with pytest.raises(ExecutionSecurityError):
        execute_action(action_req, ws)


def test_init_db_migrates_a_legacy_runs_table(tmp_path: Path) -> None:
    """A pre-#117 database gains the workspace column instead of erroring."""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            task_scope_json TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            synthetic INTEGER NOT NULL DEFAULT 1,
            interception_coverage TEXT NOT NULL,
            reasoning_availability TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    init_db(db_path)

    from scopewatch.db import get_connection

    check = get_connection(db_path)
    try:
        cols = {r["name"] for r in check.execute("PRAGMA table_info(runs)")}
    finally:
        check.close()
    assert "workspace_path" in cols
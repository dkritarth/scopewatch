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

from fastapi.testclient import TestClient

from scopewatch.app import create_app
from scopewatch.db import init_db
from scopewatch.errors import ScopewatchAPIError
from scopewatch.executor import ExecutionSecurityError, execute_action
from scopewatch.models import ApprovalStatus, ExecutionStatus, PolicyOutcome
from scopewatch.schemas import (
    CreateRunRequest,
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

NOW = datetime.now(timezone.utc).isoformat()

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


def _use_mock_remote_runner(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Point the remote backend at a mock transport, through the real config path.

    ``execute_action`` constructs ``RemoteExecutor()`` with no arguments, so the
    transport cannot be injected at the constructor. ``httpx.Client`` is patched
    instead, defaulting the mock in when the caller passes none. Patching the
    whole Client (rather than ``HTTPTransport``) also keeps this honest: the
    production path under test is exactly the argument-less one.
    """
    import httpx

    real_client = httpx.Client
    mock = httpx.MockTransport(handler)

    def client_with_mock(*args: object, **kwargs: object) -> httpx.Client:
        # The caller passes ``transport=None`` explicitly, so ``setdefault``
        # would keep the None and build a real connection.
        if kwargs.get("transport") is None:
            kwargs["transport"] = mock
        return real_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "remote")
    monkeypatch.setenv("EXECUTOR_RUNNER_URL", "http://runner.invalid:8091")
    monkeypatch.setenv("EXECUTOR_RUNNER_TOKEN", "synthetic-runner-token")
    monkeypatch.setenv("EXECUTOR_RUNNER_SIGNING_KEY", "synthetic-signing-key")
    monkeypatch.setattr(httpx, "Client", client_with_mock)


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
# The stored row must name THIS run's workspace, not merely a directory inside
# the managed root. Containment alone was the reviewer's blocking finding B3:
# the module docstring promised "data does not get to choose an arbitrary
# directory name" while accepting a sibling run's workspace, the root itself,
# and a symlink onto either.
# --------------------------------------------------------------------------


def _two_runs(tmp_path: Path) -> tuple[RunWorkspaceManager, str, str, Path]:
    fixture = _seed_fixture(tmp_path / "workspace")
    root = tmp_path / "runs"
    manager = RunWorkspaceManager(fixture, root)
    manager.initialize("run-a")
    manager.initialize("run-b")
    return manager, "run-a", "run-b", root


def test_resolve_refuses_a_sibling_runs_workspace(tmp_path: Path) -> None:
    """B3: inside the root is not the same as this run's directory."""
    manager, run_a, _run_b, root = _two_runs(tmp_path)
    with pytest.raises(RunWorkspaceError):
        manager.resolve(run_a, stored_path=str(root / "run-b"))
    # Also via a traversal that lands there: the resolved path is what matters.
    with pytest.raises(RunWorkspaceError):
        manager.resolve(run_a, stored_path=str(root / "run-a" / ".." / "run-b"))


def test_resolve_refuses_the_managed_root_itself(tmp_path: Path) -> None:
    """B3: the root is inside the root's containment check and is not a run."""
    manager, run_a, _run_b, root = _two_runs(tmp_path)
    for candidate in (root, root / ".", root / "run-a" / ".."):
        with pytest.raises(RunWorkspaceError):
            manager.resolve(run_a, stored_path=str(candidate))


def test_resolve_refuses_a_symlink_to_a_sibling_run(tmp_path: Path) -> None:
    """B3: a link inside the root can point at a sibling run's directory.

    ``root/run-a`` is a symlink to ``root/run-b``, so both the candidate and the
    expected path resolve to the same real directory. Only refusing the link
    itself catches this, which is why the check is not just resolved equality.
    """
    manager, run_a, _run_b, root = _two_runs(tmp_path)
    shutil.rmtree(root / "run-a")
    (root / "run-a").symlink_to(root / "run-b", target_is_directory=True)

    with pytest.raises(RunWorkspaceError):
        manager.resolve(run_a, stored_path=str(root / "run-a"))


def test_resolve_accepts_this_runs_own_workspace(tmp_path: Path) -> None:
    """The containment check must not refuse the legitimate case."""
    manager, run_a, run_b, root = _two_runs(tmp_path)
    assert manager.resolve(run_a, stored_path=str(root / "run-a")) == (root / "run-a").resolve()
    assert manager.resolve(run_b, stored_path=str(root / "run-b")) == (root / "run-b").resolve()


def test_resolve_accepts_a_relative_equivalent_path(tmp_path: Path) -> None:
    """A stored row may spell the same directory differently, and that is fine.

    The property is about WHICH directory, not about how it was written.
    """
    manager, run_a, _run_b, root = _two_runs(tmp_path)
    equivalent = root / "run-a" / "."
    assert manager.resolve(run_a, stored_path=str(equivalent)) == (root / "run-a").resolve()


def test_resolve_agrees_with_the_runner_sidecar(tmp_path: Path) -> None:
    """B3: the two halves of the same property must not disagree.

    The sidecar refuses these shapes for exactly this reason. If the gateway
    accepts what the runner refuses, the docstring is the only thing claiming
    safety.
    """
    import importlib.util

    runner_path = Path(__file__).resolve().parents[2] / "deploy" / "executor-runner" / "runner.py"
    spec = importlib.util.spec_from_file_location("scopewatch_runner_117_c", runner_path)
    assert spec and spec.loader
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)

    manager, run_a, _run_b, root = _two_runs(tmp_path)
    sibling = str(root / "run-b")
    managed_root = str(root)
    symlinked = root / "run-linked"
    symlinked.symlink_to(root / "run-b", target_is_directory=True)
    symlink_path = str(symlinked)

    for candidate in (sibling, managed_root, symlink_path):
        runner_side, _ = runner.resolve_run_workspace(root, candidate)
        assert runner_side is None, f"the runner unexpectedly accepted {candidate!r}"
        with pytest.raises(RunWorkspaceError):
            # The runner treats the absolute stored path as its key; the gateway
            # must refuse the same directory.
            manager.resolve(run_a, stored_path=candidate)


def test_workspace_segment_rejects_a_trailing_newline(tmp_path: Path) -> None:
    """D4: ``re.match(r"...$")`` also matches just before a trailing newline.

    Cosmetic rather than traversal (``/`` and leading dots stay excluded), but it
    is a directory name nobody intended to allow.
    """
    manager = RunWorkspaceManager(tmp_path / "workspace", tmp_path / "runs")
    with pytest.raises(RunWorkspaceError):
        manager.path_for("run\n")
    with pytest.raises(RunWorkspaceError):
        manager.path_for("run\nmore")


def test_workspace_segment_accepts_ordinary_run_ids(tmp_path: Path) -> None:
    """The tightened pattern must not reject legitimate ids."""
    manager = RunWorkspaceManager(tmp_path / "workspace", tmp_path / "runs")
    for good in ("run-a", "9544ecaf-1111-4222-8333-444444444444", "A.b_c-1", "0", "x" * 128):
        assert manager.workspace_key(good) == good


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


def _held_approval(service: ScopewatchService) -> tuple[object, object, str]:
    """A run with one PENDING approval, plus the run object."""
    scope = _scope(
        requires_approval=["write_text"],
        allowed_operations=["read_text", "write_text", "list_directory"],
    )
    run, _ = service.create_run(name="held", task_scope=scope)
    held = _submit(
        service, run.id, "write_text", "outputs/held.txt", content="held"
    )
    assert held.policy_decision.outcome == PolicyOutcome.HOLD
    return run, held.approval_request, held.approval_request.id


def test_deny_still_works_when_the_run_workspace_is_gone(tmp_path: Path) -> None:
    """D1: the reviewer must never be locked out of a stuck approval.

    Resolving the workspace up front turned every call for a run whose workspace
    had vanished into a 503 — including ``approve=false``, which cannot cause any
    effect. The approval then stayed PENDING forever with no way to clear it.
    """
    service = _service(tmp_path)
    run, _approval, approval_id = _held_approval(service)
    shutil.rmtree(Path(run.workspace_path))

    resolved = asyncio.run(
        service.resolve_approval(approval_id=approval_id, approve=False)
    )

    assert resolved.approval_request.status == ApprovalStatus.DENIED
    assert resolved.approval_request.resolved_at is not None
    # The reviewer can actually see it happened, rather than being handed a
    # 503 and a row that quietly stayed PENDING.
    denied = [e for e in resolved.events if e.event_type.value == "APPROVAL_DENIED"]
    assert denied, f"no APPROVAL_DENIED evidence was recorded: {resolved.events}"


def test_expired_approval_still_gets_its_documented_status(tmp_path: Path) -> None:
    """D1: expiry reconciliation must not 503 on an unresolvable workspace."""
    service = _service(tmp_path)
    run, _approval, approval_id = _held_approval(service)
    shutil.rmtree(Path(run.workspace_path))

    # Backdate the expiry so the branch runs.
    conn = service._get_conn()
    try:
        past = datetime.now(timezone.utc).timestamp() - 3600
        conn.execute(
            "UPDATE approval_requests SET expires_at = ? WHERE id = ?",
            (datetime.fromtimestamp(past, timezone.utc).isoformat(), approval_id),
        )
    finally:
        conn.close()

    with pytest.raises(ScopewatchAPIError) as caught:
        asyncio.run(service.resolve_approval(approval_id=approval_id, approve=False))

    assert caught.value.code == "APPROVAL_EXPIRED"
    assert caught.value.status_code == 409


def test_terminal_run_still_gets_run_not_active(tmp_path: Path) -> None:
    """D1: the run-status check must not be shadowed by a workspace failure."""
    service = _service(tmp_path)
    run, _approval, approval_id = _held_approval(service)
    service.complete_run(run.id)
    shutil.rmtree(Path(run.workspace_path))

    with pytest.raises(ScopewatchAPIError) as caught:
        asyncio.run(service.resolve_approval(approval_id=approval_id, approve=True))

    assert caught.value.code == "RUN_NOT_ACTIVE"


def test_approve_on_a_vanished_workspace_still_fails_closed(tmp_path: Path) -> None:
    """D1 must not have weakened this: approving needs the real directory."""
    service = _service(tmp_path)
    run, _approval, approval_id = _held_approval(service)
    shutil.rmtree(Path(run.workspace_path))

    with pytest.raises(ScopewatchAPIError) as caught:
        asyncio.run(service.resolve_approval(approval_id=approval_id, approve=True))

    assert caught.value.status_code == 503
    # Nothing was approved and no execution evidence was written.
    conn = service._get_conn()
    try:
        row = conn.execute(
            "SELECT status FROM approval_requests WHERE id = ?", (approval_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == ApprovalStatus.PENDING.value


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
# The production remote path, not just the backend in isolation
# --------------------------------------------------------------------------


def test_submit_action_announces_the_run_workspace_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D2: ``execute_action`` must actually pass the key through.

    The reviewer found the ``run_workspace`` parameter unreachable: nothing in
    production supplied it, so the announced key matched the workspace directory
    name only by coincidence (``path_for`` happens to be ``root / run_id``). If
    those ever drift, every test stays green while production 500s — no test in
    the suite exercised ``SCOPEWATCH_EXECUTOR=remote`` through the service.
    """
    import httpx

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.read().decode())
        return httpx.Response(
            200, json={"status": "EXECUTED", "result": {}, "error_code": None}
        )

    _use_mock_remote_runner(monkeypatch, handler)

    service = _service(tmp_path)
    run, _ = service.create_run(name="remote-e2e", task_scope=_scope())
    result = _submit(service, run.id, "write_text", "outputs/remote.txt", content="x")

    assert result.execution_receipt.status == ExecutionStatus.EXECUTED
    body = seen.get("body")
    assert isinstance(body, dict), "no dispatch reached the remote backend"
    # The key is this run's workspace DIRECTORY NAME, not the run id by luck.
    assert body["run_workspace"] == Path(run.workspace_path).name
    assert body["run_workspace"] == run.id  # true today; asserted, not assumed
    # And it is the same directory the service actually resolved for the run.
    assert service.get_run_workspace(run.id).name == body["run_workspace"]


@pytest.mark.parametrize("branch", ["allow", "approved_hold"])
def test_service_states_the_workspace_key_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, branch: str
) -> None:
    """D2: the key must be PASSED, not left to a fallback that happens to match.

    The wire key equals the run id today only because ``path_for`` is
    ``root / run_id``, so an assertion on the announced value alone cannot tell
    "passed explicitly" from "fell back and coincided" — mutating the plumbing
    away leaves every other test green. This one observes the call boundary
    itself, which is the property the reviewer actually found missing.

    Both dispatch branches are covered: they are separate call sites and only
    one of them had the key.
    """
    import scopewatch.service as service_mod

    captured: list[object] = []
    original = service_mod.execute_action

    def spy(*args: object, **kwargs: object):
        captured.append(kwargs.get("run_workspace"))
        return original(*args, **kwargs)

    monkeypatch.setattr(service_mod, "execute_action", spy)

    service = _service(tmp_path)
    if branch == "allow":
        run, _ = service.create_run(name="allow", task_scope=_scope())
        _submit(service, run.id, "write_text", "outputs/x.txt", content="x")
    else:
        scope = _scope(
            requires_approval=["write_text"],
            allowed_operations=["read_text", "write_text", "list_directory"],
        )
        run, _ = service.create_run(name="hold", task_scope=scope)
        held = _submit(
            service, run.id, "write_text", "outputs/x.txt", content="x"
        )
        asyncio.run(
            service.resolve_approval(
                approval_id=held.approval_request.id, approve=True
            )
        )

    assert captured == [Path(run.workspace_path).name], (
        f"the {branch} dispatch passed run_workspace={captured!r}; it must state "
        "the key explicitly rather than rely on the run-id fallback"
    )


def test_approved_hold_announces_the_run_workspace_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D2 on the other dispatch branch: the approval path announces the key too."""
    import httpx

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.read().decode())
        return httpx.Response(
            200, json={"status": "EXECUTED", "result": {}, "error_code": None}
        )

    _use_mock_remote_runner(monkeypatch, handler)

    scope = _scope(
        requires_approval=["write_text"],
        allowed_operations=["read_text", "write_text", "list_directory"],
    )
    service = _service(tmp_path)
    run, _ = service.create_run(name="remote-hold", task_scope=scope)
    held = _submit(
        service, run.id, "write_text", "outputs/held-remote.txt", content="x"
    )
    assert held.policy_decision.outcome == PolicyOutcome.HOLD

    resolved = asyncio.run(
        service.resolve_approval(
            approval_id=held.approval_request.id, approve=True
        )
    )
    assert resolved.execution_receipt.status == ExecutionStatus.EXECUTED
    body = seen.get("body")
    assert isinstance(body, dict), "the approved dispatch never reached the runner"
    assert body["run_workspace"] == Path(run.workspace_path).name


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
    # The key is stated explicitly, as production now does.
    receipt = executor.execute(
        action_req, ws, policy_decision=decision, run_workspace=run.id
    )

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


def test_run_api_responses_do_not_publish_the_host_workspace_path(
    tmp_path: Path,
) -> None:
    """D6: a host path has no business in a public, token-free read.

    ``Run`` is both the storage model and the FastAPI ``response_model`` for
    every run endpoint, so the persisted ``workspace_path`` rode along in each
    response. ``/api/v1/runs`` needs no token, which makes that a disclosure
    surface: it tells any reader where the gateway's writable directory layout
    is. The field must stay in storage and stay out of responses.
    """
    from fastapi.testclient import TestClient

    from scopewatch.app import create_app

    service = _service(tmp_path)
    run, _ = service.create_run(name="published", task_scope=_scope())
    assert run.workspace_path, "the storage model must still carry the path"

    app = create_app(
        db_path=service.db_path,
        workspace_root=service.workspace_root,
        run_workspaces_root=service.run_workspaces.root,
    )
    with TestClient(app) as client:
        listing = client.get("/api/v1/runs")
        assert listing.status_code == 200
        assert "workspace_path" not in listing.text, (
            "GET /api/v1/runs leaked a host workspace path"
        )
        single = client.get(f"/api/v1/runs/{run.id}")
        assert single.status_code == 200
        assert "workspace_path" not in single.text
        # The useful fields are still there.
        assert single.json()["id"] == run.id
        assert single.json()["task_scope"]["task_description"]

        created = client.post(
            "/api/v1/runs",
            json={
                "name": "api-created",
                "task_scope": _scope().model_dump(mode="json"),
            },
        )
        assert created.status_code == 201, created.text
        assert "workspace_path" not in created.text


def test_run_response_model_cannot_grow_a_host_path_silently() -> None:
    """Adding a field to storage must not automatically publish it.

    The separation is only worth anything if the two models are kept from
    drifting, and the cheapest drift guard is asserting the response model has
    no host-path field at all.
    """
    from scopewatch.schemas import Run, RunResponse

    assert "workspace_path" in Run.model_fields, (
        "storage must still persist the identity; resolution depends on it"
    )
    assert "workspace_path" not in RunResponse.model_fields


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

def test_preview_agrees_with_submission_after_fixture_changes(
    tmp_path: Path,
) -> None:
    """Preflight must evaluate the run's workspace, not the shared fixture.

    Regression for the interaction between #117 (per-run workspaces) and #122
    (preflight must agree with the deterministic decision it previews).

    While preview_action still passed ``self.workspace_root``, the two paths
    read different trees: submission resolved the run's own copy, preview read
    the shared fixture. An escaping symlink planted in the fixture after the
    run's copy existed therefore previewed ``DENY / SYMLINK_ESCAPE`` while the
    real submission returned ``ALLOW`` -- preflight reporting a denial for an
    action the gateway then permitted, which is the exact defect #122 exists to
    prevent.
    """
    workspace = tmp_path / "workspace"
    (workspace / "outputs").mkdir(parents=True)
    (workspace / "outside_target").mkdir(parents=True)
    (workspace / "outside_target" / "loot.txt").write_text("synthetic secret")
    (workspace / "outputs" / "ok.txt").write_text("synthetic")

    runs_root = tmp_path / "runs"
    app = create_app(
        db_path=str(tmp_path / "preview.db"),
        workspace_root=str(workspace),
        run_workspaces_root=runs_root,
    )
    client = TestClient(app)

    run = client.post(
        "/api/v1/runs",
        json=CreateRunRequest(
            name="preview parity run",
            task_scope=TaskScope(
                schema_version="1",
                task_description="Read the outputs directory.",
                allowed_paths=["outputs"],
                blocked_paths=[],
                allowed_tools=["workspace"],
                allowed_operations=["read_text"],
                allowed_network_destinations=[],
                requires_approval=[],
                allowed_commands=[],
                created_at=NOW,
            ),
        ).model_dump(),
    )
    assert run.status_code == 201, run.text
    run_id = run.json()["id"]

    def _read(resource: str) -> tuple[str, str]:
        body = {
            "tool": "workspace",
            "operation": "read_text",
            "resource": resource,
            "arguments": {},
            "requested_by": "acp-agent",
        }
        preview = client.post(f"/api/v1/runs/{run_id}/actions/preview", json=body)
        submitted = client.post(f"/api/v1/runs/{run_id}/actions", json=body)
        assert preview.status_code == 200, preview.text
        assert submitted.status_code == 201, submitted.text
        return (
            preview.json()["outcome"],
            submitted.json()["policy_decision"]["outcome"],
        )

    # Force the run's workspace to be created from today's fixture.
    assert _read("outputs/ok.txt") == ("ALLOW", "ALLOW")

    # The shared fixture changes afterwards. The run's workspace is a copy, so
    # submission is unaffected -- and preview must be too.
    (workspace / "outputs" / "escape.txt").symlink_to(
        "../../outside_target/loot.txt"
    )

    preview_outcome, submitted_outcome = _read("outputs/escape.txt")
    assert preview_outcome == submitted_outcome, (
        "preflight must agree with the deterministic decision it previews "
        f"(preview={preview_outcome}, submission={submitted_outcome})"
    )

"""Tests for Issue #118: Reconciling expired approvals with pending queues and run status."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
import sqlite3

from scopewatch.db import init_db, get_connection
from scopewatch.models import ApprovalStatus, EventType, PolicyOutcome, ReasonCode, RunStatus
from scopewatch.schemas import SubmitActionRequest, TaskScope
from scopewatch.service import ScopewatchService


@pytest.fixture
def env(tmp_path: Path):
    db_file = tmp_path / "test_118.db"
    init_db(db_file)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "outputs").mkdir()
    (workspace / "outputs" / "file1.txt").write_text("content 1")
    (workspace / "outputs" / "file2.txt").write_text("content 2")

    svc = ScopewatchService(db_path=db_file, workspace_root=workspace)
    task_scope = TaskScope(
        schema_version="1",
        task_description="Test issue 118 reconciliation",
        allowed_paths=["outputs"],
        allowed_tools=["workspace"],
        allowed_operations=["delete_path"],
        requires_approval=["delete_path"],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    run, _ = svc.create_run(name="Run 118", task_scope=task_scope)
    return {"service": svc, "run": run, "db_file": db_file, "workspace": workspace}


def _submit_hold(env: dict, resource: str = "outputs/file1.txt"):
    svc: ScopewatchService = env["service"]
    run = env["run"]
    req = SubmitActionRequest(
        tool="workspace",
        operation="delete_path",
        resource=resource,
    )
    res = asyncio.run(svc.submit_action(run.id, req))
    assert res.approval_request is not None
    return res


def test_expired_approval_disappears_from_pending_on_list(env: dict):
    svc: ScopewatchService = env["service"]
    run = env["run"]
    res = _submit_hold(env)
    assert res.approval_request is not None
    app_id = res.approval_request.id

    # Verify initially listed as PENDING
    pending = svc.list_approvals(status_filter=ApprovalStatus.PENDING, run_id=run.id)
    assert len(pending) == 1
    assert pending[0].id == app_id
    assert svc.get_run(run.id).status == RunStatus.WAITING_FOR_APPROVAL

    # Set expiry in the past
    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    conn = get_connection(env["db_file"])
    try:
        conn.execute("UPDATE approval_requests SET expires_at = ? WHERE id = ?", (past, app_id))
    finally:
        conn.close()

    # Querying pending list should reconcile and return empty
    pending_after = svc.list_approvals(status_filter=ApprovalStatus.PENDING, run_id=run.id)
    assert len(pending_after) == 0

    # Querying all approvals shows status is EXPIRED
    all_apps = svc.list_approvals(status_filter=None, run_id=run.id)
    assert len(all_apps) == 1
    assert all_apps[0].status == ApprovalStatus.EXPIRED
    assert all_apps[0].resolved_by == "system"

    # Evidence event APPROVAL_EXPIRED was published
    events = svc.get_events(run.id)
    expired_events = [e for e in events if e.event_type == EventType.APPROVAL_EXPIRED]
    assert len(expired_events) == 1
    assert expired_events[0].approval_request_id == app_id

    # Run status was restored to ACTIVE because no pending approvals remain
    assert svc.get_run(run.id).status == RunStatus.ACTIVE


def test_multiple_holds_partial_expiry_keeps_run_waiting(env: dict):
    svc: ScopewatchService = env["service"]
    run = env["run"]
    res1 = _submit_hold(env, "outputs/file1.txt")
    res2 = _submit_hold(env, "outputs/file2.txt")
    app1 = res1.approval_request.id
    app2 = res2.approval_request.id

    assert svc.get_run(run.id).status == RunStatus.WAITING_FOR_APPROVAL

    # Expire only app1
    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    conn = get_connection(env["db_file"])
    try:
        conn.execute("UPDATE approval_requests SET expires_at = ? WHERE id = ?", (past, app1))
    finally:
        conn.close()

    pending = svc.list_approvals(status_filter=ApprovalStatus.PENDING, run_id=run.id)
    assert len(pending) == 1
    assert pending[0].id == app2

    # Run status must still be WAITING_FOR_APPROVAL because app2 is still pending
    assert svc.get_run(run.id).status == RunStatus.WAITING_FOR_APPROVAL

    # Expire app2
    conn = get_connection(env["db_file"])
    try:
        conn.execute("UPDATE approval_requests SET expires_at = ? WHERE id = ?", (past, app2))
    finally:
        conn.close()

    pending_final = svc.list_approvals(status_filter=ApprovalStatus.PENDING, run_id=run.id)
    assert len(pending_final) == 0

    # Now run status transitions to ACTIVE
    assert svc.get_run(run.id).status == RunStatus.ACTIVE


def test_expiry_never_revives_terminal_runs(env: dict):
    svc: ScopewatchService = env["service"]
    run = env["run"]
    res = _submit_hold(env)
    app_id = res.approval_request.id

    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    conn = get_connection(env["db_file"])
    try:
        conn.execute("UPDATE approval_requests SET expires_at = ? WHERE id = ?", (past, app_id))
    finally:
        conn.close()

    # Complete the run
    svc.complete_run(run.id)
    assert svc.get_run(run.id).status == RunStatus.COMPLETED

    # Reconcile expired approvals
    svc.reconcile_expired_approvals(run_id=run.id)

    # Run status MUST stay COMPLETED
    assert svc.get_run(run.id).status == RunStatus.COMPLETED


def test_expiry_in_resolve_approval_restores_active(env: dict):
    svc: ScopewatchService = env["service"]
    run = env["run"]
    res = _submit_hold(env)
    app_id = res.approval_request.id

    assert svc.get_run(run.id).status == RunStatus.WAITING_FOR_APPROVAL

    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    conn = get_connection(env["db_file"])
    try:
        conn.execute("UPDATE approval_requests SET expires_at = ? WHERE id = ?", (past, app_id))
    finally:
        conn.close()

    from scopewatch.errors import ScopewatchAPIError
    with pytest.raises(ScopewatchAPIError) as exc:
        asyncio.run(svc.resolve_approval(app_id, approve=True))
    assert exc.value.code == "APPROVAL_EXPIRED"

    # Run status must be restored to ACTIVE
    assert svc.get_run(run.id).status == RunStatus.ACTIVE

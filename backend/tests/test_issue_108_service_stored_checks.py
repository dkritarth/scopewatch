"""Tests proving that service call sites pass a store handle to the executor (Issue #108).

Ensures that:
1. `service.submit_action` (ALLOW path) passes an active store handle (`db_path=conn`).
2. `service.resolve_approval` (HOLD/approval path) passes an active store handle (`db_path=conn`).
3. If stored state disagrees with in-memory state (e.g. execution receipt already exists,
   or stored run is terminal, or stored approval is not APPROVED), the executor refuses execution.
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import uuid
import pytest

from scopewatch.db import get_connection, init_db
import scopewatch.service as service_mod
from scopewatch.executor import ExecutionSecurityError
from scopewatch.models import (
    ApprovalStatus,
    ExecutionStatus,
    PolicyOutcome,
    RunStatus,
)
from scopewatch.repository import ScopewatchRepository
from scopewatch.schemas import (
    ActionRequest,
    ExecutionReceipt,
    Run,
    SubmitActionRequest,
    TaskScope,
)
from scopewatch.service import ScopewatchService

NOW = datetime.now(timezone.utc).isoformat()


@pytest.fixture
def service_fixture(tmp_path: Path):
    db_file = tmp_path / "issue_108.db"
    ws = tmp_path / "workspace"
    (ws / "outputs").mkdir(parents=True)
    (ws / "outputs" / "data.txt").write_text("initial text", encoding="utf-8")
    (ws / "outputs" / "old.txt").write_text("to delete", encoding="utf-8")
    init_db(db_file)
    service = ScopewatchService(db_path=db_file, workspace_root=ws)
    scope = TaskScope(
        schema_version="1",
        task_description="Issue 108 test scope",
        allowed_paths=["outputs"],
        blocked_paths=[],
        allowed_tools=["workspace"],
        allowed_operations=["read_text", "write_text", "delete_path"],
        allowed_network_destinations=[],
        requires_approval=["delete_path"],
        created_at=NOW,
    )
    run, _ = service.create_run(name="Issue 108 run", task_scope=scope)
    return {"service": service, "run": run, "workspace": ws, "db": db_file}


def test_submit_action_passes_store_handle_to_executor(service_fixture, monkeypatch: pytest.MonkeyPatch):
    """submit_action on ALLOW path passes a store handle (sqlite3.Connection) to execute_action."""
    service: ScopewatchService = service_fixture["service"]
    run: Run = service_fixture["run"]

    captured_db_path = []
    original_execute = service_mod.execute_action

    def spy_execute(action, workspace_root, policy_decision=None, approval_request=None, db_path=None, task_scope=None):
        captured_db_path.append(db_path)
        return original_execute(
            action,
            workspace_root,
            policy_decision=policy_decision,
            approval_request=approval_request,
            db_path=db_path,
            task_scope=task_scope,
        )

    monkeypatch.setattr(service_mod, "execute_action", spy_execute)

    res = asyncio.run(
        service.submit_action(
            run.id,
            SubmitActionRequest(
                tool="workspace",
                operation="read_text",
                resource="outputs/data.txt",
            ),
        )
    )

    assert res.policy_decision.outcome == PolicyOutcome.ALLOW
    assert res.execution_receipt is not None
    assert res.execution_receipt.status == ExecutionStatus.EXECUTED
    assert len(captured_db_path) == 1
    assert captured_db_path[0] is not None
    assert isinstance(captured_db_path[0], sqlite3.Connection)


def test_resolve_approval_passes_store_handle_to_executor(service_fixture, monkeypatch: pytest.MonkeyPatch):
    """resolve_approval passes a store handle (sqlite3.Connection) to execute_action."""
    service: ScopewatchService = service_fixture["service"]
    run: Run = service_fixture["run"]

    captured_db_path = []
    original_execute = service_mod.execute_action

    def spy_execute(action, workspace_root, policy_decision=None, approval_request=None, db_path=None, task_scope=None):
        captured_db_path.append(db_path)
        return original_execute(
            action,
            workspace_root,
            policy_decision=policy_decision,
            approval_request=approval_request,
            db_path=db_path,
            task_scope=task_scope,
        )

    # 1. Submit HOLD action
    res_hold = asyncio.run(
        service.submit_action(
            run.id,
            SubmitActionRequest(
                tool="workspace",
                operation="delete_path",
                resource="outputs/old.txt",
            ),
        )
    )
    assert res_hold.policy_decision.outcome == PolicyOutcome.HOLD
    assert res_hold.approval_request is not None

    monkeypatch.setattr(service_mod, "execute_action", spy_execute)

    # 2. Resolve approval
    res_res = asyncio.run(
        service.resolve_approval(
            res_hold.approval_request.id,
            approve=True,
            resolved_by="reviewer-issue-108",
        )
    )
    assert res_res.approval_request.status == ApprovalStatus.CONSUMED
    assert res_res.execution_receipt is not None
    assert res_res.execution_receipt.status == ExecutionStatus.EXECUTED
    assert len(captured_db_path) == 1
    assert captured_db_path[0] is not None
    assert isinstance(captured_db_path[0], sqlite3.Connection)


def test_resolve_approval_stored_receipt_blocks_execution(service_fixture):
    """If an execution receipt already exists in the store for this action, the executor refuses execution."""
    service: ScopewatchService = service_fixture["service"]
    run: Run = service_fixture["run"]
    db_file: Path = service_fixture["db"]

    res_hold = asyncio.run(
        service.submit_action(
            run.id,
            SubmitActionRequest(
                tool="workspace",
                operation="delete_path",
                resource="outputs/old.txt",
            ),
        )
    )
    approval_id = res_hold.approval_request.id
    action_id = res_hold.action_request.id

    # Pre-insert an execution receipt for this action directly into the store
    conn = get_connection(db_file)
    try:
        receipt = ExecutionReceipt(
            id=str(uuid.uuid4()),
            action_request_id=action_id,
            status=ExecutionStatus.EXECUTED,
            started_at=NOW,
            completed_at=NOW,
            executor="synthetic-workspace-executor",
            sanitized_result={"operation": "delete_path"},
            error_code=None,
            resource="outputs/old.txt",
            operation="delete_path",
        )
        ScopewatchRepository.create_execution_receipt(conn, receipt)
    finally:
        conn.close()

    # Now attempt resolve_approval on the service path
    with pytest.raises(ExecutionSecurityError, match="Action was already executed; replay refused."):
        asyncio.run(
            service.resolve_approval(
                approval_id,
                approve=True,
                resolved_by="reviewer-replay",
            )
        )


def test_resolve_approval_stored_status_mismatch_blocks_execution(service_fixture, monkeypatch: pytest.MonkeyPatch):
    """If the stored approval status is not APPROVED when checked by the executor, it fails closed."""
    service: ScopewatchService = service_fixture["service"]
    run: Run = service_fixture["run"]

    res_hold = asyncio.run(
        service.submit_action(
            run.id,
            SubmitActionRequest(
                tool="workspace",
                operation="delete_path",
                resource="outputs/old.txt",
            ),
        )
    )
    approval_id = res_hold.approval_request.id

    # Monkeypatch repository.resolve_approval so it leaves the DB record as PENDING in the transaction
    orig_resolve = ScopewatchRepository.resolve_approval

    def fake_resolve(conn, approval_id, new_status, resolved_by, resolved_at, reason=None):
        app = orig_resolve(conn, approval_id, new_status, resolved_by, resolved_at, reason)
        # Tamper the stored record back to PENDING inside the transaction
        conn.execute("UPDATE approval_requests SET status = ? WHERE id = ?", (ApprovalStatus.PENDING.value, approval_id))
        return app

    monkeypatch.setattr(ScopewatchRepository, "resolve_approval", fake_resolve)

    with pytest.raises(ExecutionSecurityError, match="Stored approval is not APPROVED; replay refused."):
        asyncio.run(
            service.resolve_approval(
                approval_id,
                approve=True,
                resolved_by="reviewer-tamper",
            )
        )


def test_executor_refuses_allow_when_stored_run_is_terminal(service_fixture):
    """execute_action with store handle refuses ALLOW execution if run is terminal."""
    run: Run = service_fixture["run"]
    ws: Path = service_fixture["workspace"]
    db_file: Path = service_fixture["db"]

    conn = get_connection(db_file)
    try:
        ScopewatchRepository.update_run_status(conn, run.id, RunStatus.COMPLETED, NOW)
        action = ActionRequest(
            id=str(uuid.uuid4()),
            run_id=run.id,
            tool="workspace",
            operation="read_text",
            resource="outputs/data.txt",
            requested_at=NOW,
        )
        ScopewatchRepository.create_action_request(conn, action)
        decision = service_mod.PolicyDecision(
            id=str(uuid.uuid4()),
            action_request_id=action.id,
            outcome=PolicyOutcome.ALLOW,
            reason_code=service_mod.ReasonCode.ALLOWED_TOOL_AND_RESOURCE,
            explanation="Allowed",
            matched_rule="RULE_ALLOWED",
            decided_at=NOW,
            deterministic=True,
        )
        ScopewatchRepository.create_policy_decision(conn, decision)

        with pytest.raises(ExecutionSecurityError, match="Stored run is in a terminal state; execution refused."):
            service_mod.execute_action(
                action,
                ws,
                policy_decision=decision,
                db_path=conn,
            )
    finally:
        conn.close()

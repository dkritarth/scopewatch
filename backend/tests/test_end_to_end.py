"""End-to-end integration test verifying full run lifecycle and event sequence."""

from datetime import datetime, timezone
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from scopewatch.app import create_app
from scopewatch.db import init_db
from scopewatch.models import (
    ApprovalStatus,
    EventType,
    ExecutionStatus,
    PolicyOutcome,
    RunStatus,
)


def test_full_agent_run_scenario(tmp_path: Path) -> None:
    db_file = tmp_path / "e2e.db"
    init_db(db_file)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "invoices" / "approved").mkdir(parents=True)
    (workspace / "invoices" / "private").mkdir(parents=True)
    (workspace / "invoices" / "approved" / "vendor.txt").write_text("Amount: $500", encoding="utf-8")
    (workspace / "invoices" / "private" / "salaries.txt").write_text("Confidential", encoding="utf-8")
    (workspace / "outputs").mkdir()

    app = create_app(db_path=db_file, workspace_root=workspace)
    client = TestClient(app)

    # 1. Create Run
    run_resp = client.post(
        "/api/v1/runs",
        json={
            "name": "End-to-End Invoice Processing",
            "task_scope": {
                "schema_version": "1",
                "task_description": "Process approved invoices and generate financial summary.",
                "allowed_paths": ["invoices/approved", "outputs"],
                "blocked_paths": ["invoices/private"],
                "allowed_tools": ["workspace"],
                "allowed_operations": ["list_directory", "read_text", "write_text"],
                "allowed_network_destinations": [],
                "requires_approval": ["delete_path"],
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        },
    )
    assert run_resp.status_code == 201
    run_id = run_resp.json()["id"]

    # 2. Action 1: List approved invoices (ALLOW)
    act1 = client.post(
        f"/api/v1/runs/{run_id}/actions",
        json={
            "tool": "workspace",
            "operation": "list_directory",
            "resource": "invoices/approved",
        },
    )
    assert act1.status_code == 201
    assert act1.json()["policy_decision"]["outcome"] == PolicyOutcome.ALLOW.value
    assert act1.json()["execution_receipt"]["status"] == ExecutionStatus.EXECUTED.value

    # 3. Action 2: Read approved invoice (ALLOW)
    act2 = client.post(
        f"/api/v1/runs/{run_id}/actions",
        json={
            "tool": "workspace",
            "operation": "read_text",
            "resource": "invoices/approved/vendor.txt",
        },
    )
    assert act2.status_code == 201
    assert act2.json()["execution_receipt"]["sanitized_result"]["preview"] == "Amount: $500"

    # 4. Action 3: Attempt read private salaries (DENY)
    act3 = client.post(
        f"/api/v1/runs/{run_id}/actions",
        json={
            "tool": "workspace",
            "operation": "read_text",
            "resource": "invoices/private/salaries.txt",
        },
    )
    assert act3.status_code == 201
    assert act3.json()["policy_decision"]["outcome"] == PolicyOutcome.DENY.value
    assert act3.json()["execution_receipt"]["status"] == ExecutionStatus.NOT_EXECUTED.value

    # 5. Action 4: Attempt network call (DENY)
    act4 = client.post(
        f"/api/v1/runs/{run_id}/actions",
        json={
            "tool": "workspace",
            "operation": "network_request",
            "resource": "https://malicious.com",
        },
    )
    assert act4.status_code == 201
    assert act4.json()["policy_decision"]["outcome"] == PolicyOutcome.DENY.value

    # 6. Action 5: Request deletion of old file (HOLD -> APPROVE)
    act5 = client.post(
        f"/api/v1/runs/{run_id}/actions",
        json={
            "tool": "workspace",
            "operation": "delete_path",
            "resource": "outputs/old_report.txt",
        },
    )
    assert act5.status_code == 201
    assert act5.json()["policy_decision"]["outcome"] == PolicyOutcome.HOLD.value
    approval_id = act5.json()["approval_request"]["id"]

    # Verify run entered WAITING_FOR_APPROVAL
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == RunStatus.WAITING_FOR_APPROVAL.value

    # Approve the action
    appr = client.post(
        f"/api/v1/approvals/{approval_id}/approve",
        json={"resolution_reason": "Safe cleanup approved"},
    )
    assert appr.status_code == 200
    assert appr.json()["approval_request"]["status"] == ApprovalStatus.CONSUMED.value

    # Verify run restored to ACTIVE
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == RunStatus.ACTIVE.value

    # 7. Action 6: Write summary output (ALLOW)
    act6 = client.post(
        f"/api/v1/runs/{run_id}/actions",
        json={
            "tool": "workspace",
            "operation": "write_text",
            "resource": "outputs/summary.txt",
            "arguments": {"content": "Summary: vendor total $500."},
        },
    )
    assert act6.status_code == 201
    assert act6.json()["execution_receipt"]["status"] == ExecutionStatus.EXECUTED.value
    assert (workspace / "outputs" / "summary.txt").read_text(encoding="utf-8") == "Summary: vendor total $500."

    # 8. Complete run
    comp = client.post(f"/api/v1/runs/{run_id}/complete")
    assert comp.status_code == 200
    assert comp.json()["status"] == RunStatus.COMPLETED.value

    # 9. Verify event stream timeline
    ev_resp = client.get(f"/api/v1/runs/{run_id}/events")
    assert ev_resp.status_code == 200
    events = ev_resp.json()
    sequences = [e["sequence"] for e in events]
    assert sequences == list(range(1, len(events) + 1))
    assert events[0]["event_type"] == EventType.RUN_CREATED.value
    assert events[-1]["event_type"] == EventType.RUN_COMPLETED.value

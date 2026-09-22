"""API integration tests for Scopewatch FastAPI endpoints."""

from datetime import datetime, timezone
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from scopewatch.app import create_app
from scopewatch.db import init_db
from scopewatch.models import (
    ApprovalStatus,
    ExecutionStatus,
    PolicyOutcome,
    ReasonCode,
    RunStatus,
)


@pytest.fixture
def test_env(tmp_path: Path):
    db_file = tmp_path / "api_test.db"
    init_db(db_file)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "invoices" / "approved").mkdir(parents=True)
    (workspace / "invoices" / "private").mkdir(parents=True)
    (workspace / "invoices" / "approved" / "vendor-a.txt").write_text("approved text", encoding="utf-8")
    (workspace / "invoices" / "private" / "payroll.txt").write_text("secret payroll", encoding="utf-8")
    (workspace / "outputs").mkdir()

    app = create_app(db_path=db_file, workspace_root=workspace)
    client = TestClient(app, raise_server_exceptions=False)
    return client, workspace


@pytest.fixture
def created_run_id(test_env) -> str:
    client, _ = test_env
    payload = {
        "name": "Invoice Audit",
        "task_scope": {
            "schema_version": "1",
            "task_description": "Audit approved invoices",
            "allowed_paths": ["invoices/approved", "outputs"],
            "blocked_paths": ["invoices/private"],
            "allowed_tools": ["workspace"],
            "allowed_operations": ["list_directory", "read_text", "write_text"],
            "allowed_network_destinations": [],
            "requires_approval": ["delete_path"],
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    resp = client.post("/api/v1/runs", json=payload)
    assert resp.status_code == 201
    return resp.json()["id"]


def test_health_check(test_env) -> None:
    client, _ = test_env
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["database"] == "ok"
    assert "version" in data


def test_get_run_and_404(test_env, created_run_id: str) -> None:
    client, _ = test_env
    resp = client.get(f"/api/v1/runs/{created_run_id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Invoice Audit"

    missing = client.get("/api/v1/runs/non-existent-id")
    assert missing.status_code == 404
    err = missing.json()
    assert "error" in err
    assert err["error"]["code"] == "RUN_NOT_FOUND"


def test_submit_allow_action(test_env, created_run_id: str) -> None:
    client, _ = test_env
    payload = {
        "tool": "workspace",
        "operation": "read_text",
        "resource": "invoices/approved/vendor-a.txt",
        "arguments": {},
        "reasoning_summary": "Reading vendor a invoice",
    }
    resp = client.post(f"/api/v1/runs/{created_run_id}/actions", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["policy_decision"]["outcome"] == PolicyOutcome.ALLOW.value
    assert data["execution_receipt"]["status"] == ExecutionStatus.EXECUTED.value
    assert data["execution_receipt"]["sanitized_result"]["preview"] == "approved text"
    assert len(data["events"]) >= 4  # REQUESTED, ALLOWED, EXEC_START, EXEC_SUCCEEDED


def test_submit_deny_action(test_env, created_run_id: str) -> None:
    client, _ = test_env
    payload = {
        "tool": "workspace",
        "operation": "read_text",
        "resource": "invoices/private/payroll.txt",
        "arguments": {},
    }
    resp = client.post(f"/api/v1/runs/{created_run_id}/actions", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["policy_decision"]["outcome"] == PolicyOutcome.DENY.value
    assert data["policy_decision"]["reason_code"] == ReasonCode.BLOCKED_PATH.value
    assert data["execution_receipt"]["status"] == ExecutionStatus.NOT_EXECUTED.value


def test_submit_hold_and_approve_lifecycle(test_env, created_run_id: str) -> None:
    client, workspace = test_env
    (workspace / "outputs" / "old.txt").write_text("old", encoding="utf-8")

    payload = {
        "tool": "workspace",
        "operation": "delete_path",
        "resource": "outputs/old.txt",
        "arguments": {},
    }
    resp = client.post(f"/api/v1/runs/{created_run_id}/actions", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["policy_decision"]["outcome"] == PolicyOutcome.HOLD.value
    assert data["approval_request"] is not None
    approval_id = data["approval_request"]["id"]

    # Verify run entered WAITING_FOR_APPROVAL
    run_resp = client.get(f"/api/v1/runs/{created_run_id}")
    assert run_resp.json()["status"] == RunStatus.WAITING_FOR_APPROVAL.value

    # Approve
    approve_resp = client.post(
        f"/api/v1/approvals/{approval_id}/approve",
        json={"resolution_reason": "Approved by reviewer"},
    )
    assert approve_resp.status_code == 200
    app_data = approve_resp.json()
    assert app_data["approval_request"]["status"] == ApprovalStatus.CONSUMED.value
    assert app_data["execution_receipt"]["status"] == ExecutionStatus.EXECUTED.value

    # Replay must fail with 409 conflict
    replay_resp = client.post(
        f"/api/v1/approvals/{approval_id}/approve",
        json={"resolution_reason": "Second attempt"},
    )
    assert replay_resp.status_code == 409
    assert replay_resp.json()["error"]["code"] == "APPROVAL_ALREADY_RESOLVED"


def test_submit_hold_and_deny_lifecycle(test_env, created_run_id: str) -> None:
    client, _ = test_env
    payload = {
        "tool": "workspace",
        "operation": "delete_path",
        "resource": "outputs/temp.txt",
    }
    resp = client.post(f"/api/v1/runs/{created_run_id}/actions", json=payload)
    assert resp.status_code == 201
    approval_id = resp.json()["approval_request"]["id"]

    # Deny approval
    deny_resp = client.post(
        f"/api/v1/approvals/{approval_id}/deny",
        json={"resolution_reason": "Rejected deletion"},
    )
    assert deny_resp.status_code == 200
    assert deny_resp.json()["approval_request"]["status"] == ApprovalStatus.DENIED.value
    assert deny_resp.json()["execution_receipt"]["status"] == ExecutionStatus.NOT_EXECUTED.value


def test_get_events_pagination(test_env, created_run_id: str) -> None:
    client, _ = test_env
    client.post(
        f"/api/v1/runs/{created_run_id}/actions",
        json={"tool": "workspace", "operation": "read_text", "resource": "invoices/approved/vendor-a.txt"},
    )

    resp = client.get(f"/api/v1/runs/{created_run_id}/events")
    assert resp.status_code == 200
    events = resp.json()
    assert len(events) >= 5

    # Sequence filter
    filtered = client.get(f"/api/v1/runs/{created_run_id}/events?after_sequence=2&limit=2")
    assert filtered.status_code == 200
    paged = filtered.json()
    assert len(paged) == 2
    assert paged[0]["sequence"] == 3
    assert paged[1]["sequence"] == 4


def test_sse_stream_initial_connection(test_env, created_run_id: str) -> None:
    client, _ = test_env
    with client.stream("GET", f"/api/v1/runs/{created_run_id}/events/stream?limit=1") as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        lines = [line for line in resp.iter_lines() if line]
        assert any("ready" in l for l in lines)


def test_schema_validation_error_format(test_env) -> None:
    client, _ = test_env
    resp = client.post("/api/v1/runs", json={"invalid": "payload"})
    assert resp.status_code == 422
    data = resp.json()
    assert "error" in data
    assert data["error"]["code"] == "SCHEMA_VALIDATION_ERROR"
    assert "request_id" in data["error"]

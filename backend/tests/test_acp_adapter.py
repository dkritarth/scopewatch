"""Tests for Agent Client Protocol (ACP) adapter (issue #49, spike #48).

Verifies the ACP adapter mediating actions through the Scopewatch gateway API
using an in-process TestClient with clean-room synthetic fixtures.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scopewatch.acp_adapter import (
    COVERAGE_STATEMENT,
    AcpClientAdapter,
    AcpRpcError,
    HoldConfig,
)
from scopewatch.app import create_app
from scopewatch.models import (
    ApprovalStatus,
    PolicyOutcome,
    ReasonCode,
    ReasoningProvenance,
)
from scopewatch.schemas import CreateRunRequest, TaskScope


@pytest.fixture
def clean_client(tmp_path: Path) -> TestClient:
    """FastAPI TestClient with isolated temporary sqlite database and workspace."""
    db_path = tmp_path / "test.db"
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    # Populate synthetic test workspace
    (workspace_dir / "invoices").mkdir()
    (workspace_dir / "invoices" / "approved").mkdir()
    (workspace_dir / "invoices" / "approved" / "vendor_a.txt").write_text("Invoice: $500")
    (workspace_dir / "invoices" / "private").mkdir()
    (workspace_dir / "invoices" / "private" / "salaries.txt").write_text("Secret salary data")
    (workspace_dir / "outputs").mkdir()
    (workspace_dir / "outputs" / "old.txt").write_text("Legacy file to delete")

    app = create_app(db_path=str(db_path), workspace_root=str(workspace_dir))
    return TestClient(app)


@pytest.fixture
def active_run_id(clean_client: TestClient) -> str:
    """Create an active task run with standard scope constraints."""
    scope = TaskScope(
        task_description="ACP coding agent evaluation",
        allowed_paths=["invoices/approved", "outputs"],
        blocked_paths=["invoices/private"],
        allowed_tools=["workspace"],
        allowed_operations=["read_text", "write_text", "delete_path", "list_directory", "run_command"],
        requires_approval=["delete_path"],
        allowed_commands=[["pytest", "tests/"]],
        commands_requiring_approval=[],
        allowed_network_destinations=[],
        created_at="2026-09-30T12:00:00Z",
    )
    req = CreateRunRequest(name="ACP Test Run", task_scope=scope)
    resp = clean_client.post("/api/v1/runs", json=req.model_dump())
    assert resp.status_code == 201
    return resp.json()["id"]


def test_acp_initialize_capabilities(clean_client: TestClient, active_run_id: str) -> None:
    """Initialize method advertises mediated tools and non-bypassable coverage statement."""
    adapter = AcpClientAdapter(clean_client, active_run_id)
    req = {
        "jsonrpc": "2.0",
        "id": "init-1",
        "method": "initialize",
        "params": {},
    }
    resp = adapter.handle_jsonrpc(req)
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == "init-1"
    assert "error" not in resp
    result = resp["result"]
    assert result["capabilities"]["fs"]["readTextFile"] is True
    assert result["capabilities"]["fs"]["writeTextFile"] is True
    assert result["capabilities"]["fs"]["deleteFile"] is True
    assert result["capabilities"]["terminal"]["run"] is True
    assert result["capabilities"]["session"]["requestPermission"] is True
    assert result["serverInfo"]["coverageStatement"] == COVERAGE_STATEMENT


def test_acp_invalid_jsonrpc_format(clean_client: TestClient, active_run_id: str) -> None:
    """Malformed JSON-RPC request yields standard error code -32600."""
    adapter = AcpClientAdapter(clean_client, active_run_id)
    # Missing jsonrpc version
    resp = adapter.handle_jsonrpc({"id": "err-1", "method": "initialize"})
    assert resp["error"]["code"] == -32600


def test_acp_unsupported_method(clean_client: TestClient, active_run_id: str) -> None:
    """Unknown ACP method returns standard -32601 Method Not Found error."""
    adapter = AcpClientAdapter(clean_client, active_run_id)
    req = {
        "jsonrpc": "2.0",
        "id": "unknown-1",
        "method": "unsupported/native_execute",
        "params": {},
    }
    resp = adapter.handle_jsonrpc(req)
    assert resp["error"]["code"] == -32601


def test_acp_read_allowed(clean_client: TestClient, active_run_id: str) -> None:
    """fs/read_text_file for allowed path executes via gateway and returns file content."""
    adapter = AcpClientAdapter(clean_client, active_run_id)
    req = {
        "jsonrpc": "2.0",
        "id": "read-1",
        "method": "fs/read_text_file",
        "params": {"path": "invoices/approved/vendor_a.txt"},
    }
    resp = adapter.handle_jsonrpc(req)
    assert "error" not in resp
    assert resp["result"]["content"] == "Invoice: $500"
    assert resp["result"]["path"] == "invoices/approved/vendor_a.txt"


def test_acp_read_denied_blocked_path(clean_client: TestClient, active_run_id: str) -> None:
    """fs/read_text_file for blocked path surfaces policy DENY with reason code."""
    adapter = AcpClientAdapter(clean_client, active_run_id)
    req = {
        "jsonrpc": "2.0",
        "id": "read-2",
        "method": "fs/read_text_file",
        "params": {"path": "invoices/private/salaries.txt"},
    }
    resp = adapter.handle_jsonrpc(req)
    assert "result" not in resp
    assert resp["error"]["code"] == -32000
    assert resp["error"]["data"]["reason_code"] == ReasonCode.BLOCKED_PATH.value


def test_acp_read_denied_path_traversal(clean_client: TestClient, active_run_id: str) -> None:
    """fs/read_text_file with directory traversal returns PATH_TRAVERSAL denial."""
    adapter = AcpClientAdapter(clean_client, active_run_id)
    req = {
        "jsonrpc": "2.0",
        "id": "read-3",
        "method": "fs/read_text_file",
        "params": {"path": "../../etc/passwd"},
    }
    resp = adapter.handle_jsonrpc(req)
    assert resp["error"]["data"]["reason_code"] == ReasonCode.PATH_TRAVERSAL.value


def test_acp_write_allowed(clean_client: TestClient, active_run_id: str) -> None:
    """fs/write_text_file for allowed path writes via gateway and returns success."""
    adapter = AcpClientAdapter(clean_client, active_run_id)
    req = {
        "jsonrpc": "2.0",
        "id": "write-1",
        "method": "fs/write_text_file",
        "params": {
            "path": "outputs/result.txt",
            "content": "Processing complete",
        },
    }
    resp = adapter.handle_jsonrpc(req)
    assert "error" not in resp
    assert resp["result"]["status"] == "success"
    assert resp["result"]["bytes_written"] > 0


def test_acp_delete_requires_approval_and_succeeds(clean_client: TestClient, active_run_id: str) -> None:
    """fs/delete_file requiring approval holds, resolves on approval, and returns deleted."""
    hold_cfg = HoldConfig(timeout_s=5.0, poll_interval_s=0.05)
    adapter = AcpClientAdapter(clean_client, active_run_id, hold=hold_cfg)

    # In a separate action sequence: submit action, resolve approval, verify executed
    action_resp = clean_client.post(
        f"/api/v1/runs/{active_run_id}/actions",
        json={
            "tool": "workspace",
            "operation": "delete_path",
            "resource": "outputs/old.txt",
            "arguments": {},
            "requested_by": "acp-agent",
        },
    )
    assert action_resp.status_code == 201
    action_data = action_resp.json()
    assert action_data["policy_decision"]["outcome"] == PolicyOutcome.HOLD.value
    action_id = action_data["action_request"]["id"]
    approval_id = action_data["approval_request"]["id"]

    # Approve the action via human approval endpoint
    appr_resp = clean_client.post(
        f"/api/v1/approvals/{approval_id}/approve",
        json={"resolution_reason": "Approved by reviewer"},
    )
    assert appr_resp.status_code == 200

    # Polling now returns executed status
    poll_result = adapter._poll_hold(action_id, initial_response=action_data)
    assert poll_result["execution_receipt"]["status"] == "EXECUTED"


def test_acp_delete_held_and_rejected(clean_client: TestClient, active_run_id: str) -> None:
    """Held delete action fails closed when reviewer rejects approval."""
    hold_cfg = HoldConfig(timeout_s=2.0, poll_interval_s=0.05)
    adapter = AcpClientAdapter(clean_client, active_run_id, hold=hold_cfg)

    action_resp = clean_client.post(
        f"/api/v1/runs/{active_run_id}/actions",
        json={
            "tool": "workspace",
            "operation": "delete_path",
            "resource": "outputs/old.txt",
            "arguments": {},
            "requested_by": "acp-agent",
        },
    )
    action_data = action_resp.json()
    action_id = action_data["action_request"]["id"]
    approval_id = action_data["approval_request"]["id"]

    # Reject the approval
    rej_resp = clean_client.post(
        f"/api/v1/approvals/{approval_id}/deny",
        json={"resolution_reason": "Unsafe deletion"},
    )
    assert rej_resp.status_code == 200

    # Polling fails closed
    with pytest.raises(AcpRpcError) as exc_info:
        adapter._poll_hold(action_id, initial_response=action_data)
    assert "not approved: DENIED" in str(exc_info.value)


def test_acp_delete_held_timeout_fails_closed(clean_client: TestClient, active_run_id: str) -> None:
    """Held action without human response times out and fails closed."""
    hold_cfg = HoldConfig(timeout_s=0.1, poll_interval_s=0.02)
    adapter = AcpClientAdapter(clean_client, active_run_id, hold=hold_cfg)

    action_resp = clean_client.post(
        f"/api/v1/runs/{active_run_id}/actions",
        json={
            "tool": "workspace",
            "operation": "delete_path",
            "resource": "outputs/old.txt",
            "arguments": {},
            "requested_by": "acp-agent",
        },
    )
    action_data = action_resp.json()
    action_id = action_data["action_request"]["id"]

    with pytest.raises(AcpRpcError) as exc_info:
        adapter._poll_hold(action_id, initial_response=action_data)
    assert exc_info.value.data["reason_code"] == "HOLD_TIMEOUT"


def test_acp_request_permission(clean_client: TestClient, active_run_id: str) -> None:
    """session/request_permission evaluates policy and returns decision allow/deny."""
    adapter = AcpClientAdapter(clean_client, active_run_id)

    # Allowed operation permission
    req_allow = {
        "jsonrpc": "2.0",
        "id": "perm-1",
        "method": "session/request_permission",
        "params": {
            "operation": "read_text",
            "path": "invoices/approved/vendor_a.txt",
        },
    }
    resp_allow = adapter.handle_jsonrpc(req_allow)
    assert resp_allow["result"]["decision"] == "allow"

    # Denied operation permission
    req_deny = {
        "jsonrpc": "2.0",
        "id": "perm-2",
        "method": "session/request_permission",
        "params": {
            "operation": "read_text",
            "path": "invoices/private/salaries.txt",
        },
    }
    resp_deny = adapter.handle_jsonrpc(req_deny)
    assert resp_deny["result"]["decision"] == "deny"


def test_acp_provenance_invariant(clean_client: TestClient, active_run_id: str) -> None:
    """ACP adapter records AGENT_AUTHORED_SUMMARY when provided and UNAVAILABLE otherwise."""
    adapter = AcpClientAdapter(clean_client, active_run_id)

    # With summary
    req_summary = {
        "jsonrpc": "2.0",
        "id": "prov-1",
        "method": "fs/read_text_file",
        "params": {
            "path": "invoices/approved/vendor_a.txt",
            "reasoning_summary": "Checking vendor A subtotal before calculation",
        },
    }
    resp_summary = adapter.handle_jsonrpc(req_summary)
    action_id_1 = resp_summary["result"]["action_id"]

    event_resp_1 = clean_client.get(f"/api/v1/runs/{active_run_id}/actions/{action_id_1}")
    action_record_1 = event_resp_1.json()["action_request"]
    assert action_record_1["reasoning_provenance"] == ReasoningProvenance.AGENT_AUTHORED_SUMMARY.value
    assert action_record_1["reasoning_summary"] == "Checking vendor A subtotal before calculation"
    assert action_record_1["exposed_reasoning_trace"] is None

    # Without summary
    req_no_summary = {
        "jsonrpc": "2.0",
        "id": "prov-2",
        "method": "fs/read_text_file",
        "params": {"path": "invoices/approved/vendor_a.txt"},
    }
    resp_no_summary = adapter.handle_jsonrpc(req_no_summary)
    action_id_2 = resp_no_summary["result"]["action_id"]

    event_resp_2 = clean_client.get(f"/api/v1/runs/{active_run_id}/actions/{action_id_2}")
    action_record_2 = event_resp_2.json()["action_request"]
    assert action_record_2["reasoning_provenance"] == ReasoningProvenance.UNAVAILABLE.value
    assert action_record_2["reasoning_summary"] is None
    assert action_record_2["exposed_reasoning_trace"] is None


def test_acp_list_directory_allowed(clean_client: TestClient, active_run_id: str) -> None:
    """fs/list_directory returns directory listing through gateway."""
    adapter = AcpClientAdapter(clean_client, active_run_id)
    req = {
        "jsonrpc": "2.0",
        "id": "list-1",
        "method": "fs/list_directory",
        "params": {"path": "invoices/approved"},
    }
    resp = adapter.handle_jsonrpc(req)
    assert "error" not in resp
    assert resp["result"]["path"] == "invoices/approved"
    assert "entries" in resp["result"]


def test_acp_terminal_command_disallowed(
    clean_client: TestClient, active_run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """terminal/run for disallowed command returns COMMAND_NOT_ALLOWED policy denial."""
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    adapter = AcpClientAdapter(clean_client, active_run_id)
    req = {
        "jsonrpc": "2.0",
        "id": "term-1",
        "method": "terminal/run",
        "params": {"command": "cat /etc/shadow"},
    }
    resp = adapter.handle_jsonrpc(req)
    assert "result" not in resp
    assert resp["error"]["code"] == -32000
    assert resp["error"]["data"]["reason_code"] == ReasonCode.COMMAND_NOT_ALLOWED.value


def test_acp_terminal_shell_metacharacter(
    clean_client: TestClient, active_run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """terminal/run containing shell chaining characters returns SHELL_METACHARACTER."""
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    adapter = AcpClientAdapter(clean_client, active_run_id)
    req = {
        "jsonrpc": "2.0",
        "id": "term-2",
        "method": "terminal/run",
        "params": {"command": "pytest tests/ ; cat /etc/passwd"},
    }
    resp = adapter.handle_jsonrpc(req)
    assert "result" not in resp
    assert resp["error"]["code"] == -32000
    assert resp["error"]["data"]["reason_code"] == ReasonCode.SHELL_METACHARACTER.value


def test_acp_missing_parameter_validation(clean_client: TestClient, active_run_id: str) -> None:
    """Methods missing required parameters fail closed with standard -32602 code."""
    adapter = AcpClientAdapter(clean_client, active_run_id)

    # Missing path in read
    resp1 = adapter.handle_jsonrpc({"jsonrpc": "2.0", "id": "val-1", "method": "fs/read_text_file", "params": {}})
    assert resp1["error"]["code"] == -32602

    # Missing command in terminal/run
    resp2 = adapter.handle_jsonrpc({"jsonrpc": "2.0", "id": "val-2", "method": "terminal/run", "params": {}})
    assert resp2["error"]["code"] == -32602

    # Missing content in write
    resp3 = adapter.handle_jsonrpc(
        {"jsonrpc": "2.0", "id": "val-3", "method": "fs/write_text_file", "params": {"path": "outputs/out.txt"}}
    )
    assert resp3["error"]["code"] == -32602


def test_acp_adapter_ast_invariants() -> None:
    """Verify that acp_adapter.py never imports executor, subprocess, or os execution primitives."""
    import scopewatch.acp_adapter as adapter_mod

    source = inspect.getsource(adapter_mod)
    tree = ast.parse(source)

    forbidden_imports = {
        "subprocess",
        "shutil",
        "scopewatch.executor",
        "scopewatch.executor_docker",
        "scopewatch.executor_remote",
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in forbidden_imports, f"Forbidden import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            mod_name = node.module or ""
            assert mod_name not in forbidden_imports, f"Forbidden import from: {mod_name}"

"""Tests for Agent Client Protocol (ACP) adapter (issue #49, spike #48).

Verifies the ACP adapter mediating actions through the Scopewatch gateway API
using an in-process TestClient with clean-room synthetic fixtures.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from scopewatch.acp_adapter import (
    COVERAGE_STATEMENT,
    MAX_RELAYED_ERROR_CHARS,
    MAX_RELAYED_FILE_CHARS,
    MAX_RELAYED_STREAM_CHARS,
    RELAY_TRUNCATION_MARKER,
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
    assert result["capabilities"]["terminal"]["create"] is False
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


def test_acp_terminal_create_unsupported(clean_client: TestClient, active_run_id: str) -> None:
    """terminal/create returns unsupported method error guiding callers to terminal/run."""
    adapter = AcpClientAdapter(clean_client, active_run_id)
    req = {
        "jsonrpc": "2.0",
        "id": "term-create-1",
        "method": "terminal/create",
        "params": {},
    }
    resp = adapter.handle_jsonrpc(req)
    assert resp["error"]["code"] == -32601
    assert "Use 'terminal/run'" in resp["error"]["message"]


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

    # Deny the approval
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


def test_acp_request_permission_dry_run_invariants(clean_client: TestClient, active_run_id: str) -> None:
    """session/request_permission performs dry-run scope check without executing actions."""
    adapter = AcpClientAdapter(clean_client, active_run_id)

    # Allowed operation permission check
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
    assert resp_allow["result"]["scope_verified"] is True

    # Denied operation permission check (blocked path)
    req_deny_path = {
        "jsonrpc": "2.0",
        "id": "perm-2",
        "method": "session/request_permission",
        "params": {
            "operation": "read_text",
            "path": "invoices/private/salaries.txt",
        },
    }
    resp_deny_path = adapter.handle_jsonrpc(req_deny_path)
    assert resp_deny_path["result"]["decision"] == "deny"
    assert resp_deny_path["result"]["reason_code"] == "BLOCKED_PATH"

    # Approval required permission check
    req_approval = {
        "jsonrpc": "2.0",
        "id": "perm-3",
        "method": "session/request_permission",
        "params": {
            "operation": "delete_path",
            "path": "outputs/old.txt",
        },
    }
    resp_appr = adapter.handle_jsonrpc(req_approval)
    assert resp_appr["result"]["decision"] == "requires_approval"
    assert resp_appr["result"]["reason_code"] == "APPROVAL_REQUIRED"

    # Crucial dry-run invariant: request_permission must NEVER have submitted action records
    events_resp = clean_client.get(f"/api/v1/runs/{active_run_id}/events")
    assert events_resp.status_code == 200
    events = events_resp.json()
    action_events = [e for e in events if e.get("event_type") == "ACTION_REQUESTED"]
    assert len(action_events) == 0, "request_permission must never record stateful ActionRequest"


def test_acp_provenance_invariant(clean_client: TestClient, active_run_id: str) -> None:
    """An ACP client is a gateway API caller, so its claim stays unverified.

    The adapter relays text an external ACP client wrote, so the gateway cannot
    authenticate its origin and stores the claim as a caller assertion (#116).
    """
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
    assert (
        action_record_1["reasoning_provenance"]
        == ReasoningProvenance.CALLER_ASSERTED_SUMMARY.value
    )
    assert (
        action_record_1["caller_claimed_provenance"]
        == ReasoningProvenance.AGENT_AUTHORED_SUMMARY.value
    )
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


def test_acp_terminal_command_cwd_validation(
    clean_client: TestClient, active_run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """terminal/run validating that cwd outside scope is blocked."""
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    adapter = AcpClientAdapter(clean_client, active_run_id)
    req = {
        "jsonrpc": "2.0",
        "id": "term-cwd",
        "method": "terminal/run",
        "params": {"command": "pytest tests/", "cwd": "invoices/private"},
    }
    resp = adapter.handle_jsonrpc(req)
    assert "result" not in resp
    assert resp["error"]["code"] == -32000
    assert resp["error"]["data"]["reason_code"] == ReasonCode.BLOCKED_PATH.value


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


def test_acp_hold_config_pydantic_forbid_extra() -> None:
    """HoldConfig rejects extra attributes and validates bounds via Pydantic v2."""
    with pytest.raises(ValidationError):
        HoldConfig(timeout_s=-1.0)
    with pytest.raises(ValidationError):
        HoldConfig(timeout_s=5.0, unexpected_field="invalid")  # type: ignore[call-arg]


class FakeAcpAgent:
    """Simulated coding agent speaking ACP JSON-RPC 2.0 protocol over adapter."""

    def __init__(self, adapter: AcpClientAdapter) -> None:
        self.adapter = adapter
        self.msg_id = 0

    def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.msg_id += 1
        req = {
            "jsonrpc": "2.0",
            "id": f"fake-agent-{self.msg_id}",
            "method": method,
            "params": params,
        }
        return self.adapter.handle_jsonrpc(req)


def test_fake_acp_agent_full_turn_contract(clean_client: TestClient, active_run_id: str) -> None:
    """End-to-end multi-turn interaction loop driven by FakeAcpAgent."""
    adapter = AcpClientAdapter(clean_client, active_run_id)
    agent = FakeAcpAgent(adapter)

    # Turn 1: initialize and discover capabilities
    init_res = agent.rpc("initialize", {})
    assert init_res["result"]["capabilities"]["fs"]["readTextFile"] is True

    # Turn 2: Preflight permission check on allowed read
    perm_res = agent.rpc("session/request_permission", {
        "operation": "read_text",
        "path": "invoices/approved/vendor_a.txt",
    })
    assert perm_res["result"]["decision"] == "allow"

    # Turn 3: Execute read
    read_res = agent.rpc("fs/read_text_file", {
        "path": "invoices/approved/vendor_a.txt",
        "reasoning_summary": "Agent reading approved vendor A invoice",
        "turn_id": "turn-1",
    })
    assert "Invoice: $500" in read_res["result"]["content"]

    # Turn 4: Write result
    write_res = agent.rpc("fs/write_text_file", {
        "path": "outputs/vendor_summary.txt",
        "content": "Vendor A: 500",
        "reasoning_summary": "Agent writing processed summary",
        "turn_id": "turn-2",
    })
    assert write_res["result"]["status"] == "success"

    # Turn 5: Attempt unauthorized traversal
    deny_res = agent.rpc("fs/read_text_file", {
        "path": "../../../etc/shadow",
        "turn_id": "turn-3",
    })
    assert deny_res["error"]["code"] == -32000
    assert deny_res["error"]["data"]["reason_code"] == ReasonCode.PATH_TRAVERSAL.value


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


# ---------------------------------------------------------------------------
# Relay bounds (issue #169): the adapter bounds what reaches the agent, and
# every cut it makes is visible. Clean-room stubbed gateway, no network.
# ---------------------------------------------------------------------------


def _stub_gateway(gateway_body: dict[str, Any]) -> httpx.Client:
    """Gateway client answering every request with one fixed body."""
    transport = httpx.MockTransport(lambda request: httpx.Response(201, json=gateway_body))
    return httpx.Client(transport=transport, base_url="http://gateway.invalid")


def _executed_receipt(sanitized_result: dict[str, Any]) -> dict[str, Any]:
    return {
        "action_request": {"id": "action-169"},
        "policy_decision": {"outcome": "ALLOW"},
        "execution_receipt": {
            "status": "EXECUTED",
            "error_code": None,
            "sanitized_result": sanitized_result,
        },
    }


def _rpc(adapter: AcpClientAdapter, method: str, params: dict[str, Any]) -> dict[str, Any]:
    return adapter.handle_jsonrpc(
        {"jsonrpc": "2.0", "id": "t-169", "method": method, "params": params}
    )


def test_terminal_run_success_output_is_bounded_with_a_visible_marker() -> None:
    """A successful command's stdout is capped at the stream bound, and marked.

    The second-pass reviewer of #154 measured ~200,000 characters relayed
    verbatim on the success path (#169). The bound alone is not enough: a
    cut with no marker is a silently wrong answer to the agent, so the
    marker and the original size must be present in the relayed text.
    """
    stdout = "line-1\n" + "x" * 200_000
    gateway_body = _executed_receipt(
        {
            "operation": "run_command",
            "exit_code": 0,
            "stdout": stdout,
            "stderr": "short warning\n",
        }
    )
    with _stub_gateway(gateway_body) as fake:
        resp = _rpc(
            AcpClientAdapter(fake, "run-1"),
            "terminal/run",
            {"command": "pytest tests/"},
        )

    assert "error" not in resp, resp
    relayed = resp["result"]["stdout"]
    assert len(relayed) <= MAX_RELAYED_STREAM_CHARS, "success path must be bounded"
    assert RELAY_TRUNCATION_MARKER in relayed, "truncation must be visible, never silent"
    assert str(len(stdout)) in relayed, "the marker must name the original size"
    assert relayed.startswith("line-1\n"), "the head of the output must survive"
    # A short stream is relayed byte for byte, with no marker invented.
    assert resp["result"]["stderr"] == "short warning\n"
    assert RELAY_TRUNCATION_MARKER not in resp["result"]["stderr"]
    assert resp["result"]["exit_code"] == 0
    assert resp["result"]["action_id"] == "action-169"


def test_terminal_run_success_output_within_the_bound_is_untouched() -> None:
    """The bound must not alter output that already fits: no clip, no marker."""
    stdout = "2 passed, 1 skipped in 0.03s\n"
    gateway_body = _executed_receipt(
        {"operation": "run_command", "exit_code": 0, "stdout": stdout, "stderr": ""}
    )
    with _stub_gateway(gateway_body) as fake:
        resp = _rpc(AcpClientAdapter(fake, "run-1"), "terminal/run", {"command": "pytest tests/"})

    assert "error" not in resp, resp
    assert resp["result"]["stdout"] == stdout
    assert RELAY_TRUNCATION_MARKER not in resp["result"]["stdout"]
    assert len(resp["result"]["stdout"]) <= MAX_RELAYED_STREAM_CHARS


def test_terminal_run_non_integer_exit_code_is_reported_as_unknown() -> None:
    """A malformed receipt cannot wear an integer's key: exit_code is int or unknown.

    Relay bounds are not only about length. ``sanitized["exit_code"]`` used
    to be relayed verbatim, so a runner bug could hand the agent an
    arbitrarily long string where a number belongs (#169).
    """
    gateway_body = _executed_receipt(
        {
            "operation": "run_command",
            "exit_code": "0" * 5000,
            "stdout": "done\n",
            "stderr": "",
        }
    )
    with _stub_gateway(gateway_body) as fake:
        resp = _rpc(AcpClientAdapter(fake, "run-1"), "terminal/run", {"command": "pytest tests/"})

    assert "error" not in resp, resp
    assert resp["result"]["exit_code"] is None, "unknown, never invented and never relayed raw"
    # Unaffected fields still relay normally.
    assert resp["result"]["stdout"] == "done\n"


def test_read_text_refuses_oversized_content_with_an_explicit_error() -> None:
    """File content over the read bound is refused, never clipped (#169).

    A truncated file that looks complete is a correctness hazard for an
    agent about to edit it: writing the short copy back would destroy the
    tail of the file. So the relay fails loudly with the size, the limit,
    and the action ID for joining back to the evidence — and it does not
    claim the execution failed, because the receipt says it succeeded.
    """
    content = "a" * (MAX_RELAYED_FILE_CHARS + 1)
    gateway_body = _executed_receipt(
        {"operation": "read_text", "content": content, "truncated": False}
    )
    with _stub_gateway(gateway_body) as fake:
        resp = _rpc(
            AcpClientAdapter(fake, "run-1"),
            "fs/read_text_file",
            {"path": "outputs/big.txt"},
        )

    assert "result" not in resp, "a partial file must never be returned as a success"
    err = resp["error"]
    assert err["code"] == -32000
    data = err["data"]
    assert data["reason_code"] == "READ_RESULT_TOO_LARGE"
    assert data["char_count"] == len(content)
    assert data["limit"] == MAX_RELAYED_FILE_CHARS
    assert data["action_id"] == "action-169"
    assert data["path"] == "outputs/big.txt"
    assert "too large to relay" in err["message"]
    assert str(MAX_RELAYED_FILE_CHARS) in err["message"]
    assert "content" not in data, "no fragment of the refused file may leak"


def test_read_text_at_the_bound_returns_the_whole_file() -> None:
    """Content that fits exactly at the bound is relayed whole, not refused."""
    content = "b" * MAX_RELAYED_FILE_CHARS
    gateway_body = _executed_receipt(
        {"operation": "read_text", "content": content, "truncated": False}
    )
    with _stub_gateway(gateway_body) as fake:
        resp = _rpc(
            AcpClientAdapter(fake, "run-1"),
            "fs/read_text_file",
            {"path": "outputs/edge.txt"},
        )

    assert "error" not in resp, resp
    assert resp["result"]["content"] == content, "exactly at the bound, byte for byte"
    assert resp["result"]["truncated"] is False


def test_read_text_relays_the_executor_truncation_flag() -> None:
    """An executor's own preview cut is relayed as a flag, not dropped (#169).

    Reads currently expose a 500-char preview with ``truncated: true``, and
    the adapter used to hand the agent the preview while discarding the
    flag, so a 500-char result looked like the whole file.
    """
    preview = "first 500 chars of a much larger file"
    gateway_body = _executed_receipt(
        {
            "operation": "read_text",
            "preview": preview,
            "byte_count": 100_000,
            "truncated": True,
        }
    )
    with _stub_gateway(gateway_body) as fake:
        resp = _rpc(
            AcpClientAdapter(fake, "run-1"),
            "fs/read_text_file",
            {"path": "outputs/large.txt"},
        )

    assert "error" not in resp, resp
    assert resp["result"]["content"] == preview
    assert resp["result"]["truncated"] is True, "a preview that is not the file must say so"


def test_read_text_marks_a_whole_file_as_not_truncated() -> None:
    """The honest default: a complete read reports truncated false."""
    gateway_body = _executed_receipt(
        {"operation": "read_text", "preview": "Invoice: $500", "truncated": False}
    )
    with _stub_gateway(gateway_body) as fake:
        resp = _rpc(
            AcpClientAdapter(fake, "run-1"),
            "fs/read_text_file",
            {"path": "invoices/approved/vendor_a.txt"},
        )

    assert "error" not in resp, resp
    assert resp["result"]["content"] == "Invoice: $500"
    assert resp["result"]["truncated"] is False


def test_failed_command_output_truncation_is_marked() -> None:
    """The #154 failure path now marks its clip too: no silent cut anywhere."""
    stderr = "boom\n" + "e" * 50_000
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            201,
            json={
                "action_request": {"id": "action-169-fail"},
                "policy_decision": {"outcome": "ALLOW"},
                "execution_receipt": {
                    "status": "FAILED",
                    "error_code": "NONZERO_EXIT",
                    "sanitized_result": {
                        "operation": "run_command",
                        "exit_code": 2,
                        "stdout": "",
                        "stderr": stderr,
                    },
                },
            },
        )
    )
    with httpx.Client(transport=transport, base_url="http://gateway.invalid") as fake:
        resp = _rpc(AcpClientAdapter(fake, "run-1"), "terminal/run", {"command": "pytest tests/"})

    assert "result" not in resp
    data = resp["error"]["data"]
    assert len(data["stderr"]) <= MAX_RELAYED_STREAM_CHARS
    assert RELAY_TRUNCATION_MARKER in data["stderr"], "truncation must be visible, never silent"
    assert str(len(stderr)) in data["stderr"]
    assert data["stderr"].startswith("boom\n")
    assert data["exit_code"] == 2


def test_failed_receipt_error_detail_truncation_is_marked() -> None:
    """The receipt's error detail is bounded by a named constant and marked."""
    detail = "File not found: " + "y" * 5_000
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            201,
            json={
                "action_request": {"id": "action-169-err"},
                "policy_decision": {"outcome": "ALLOW"},
                "execution_receipt": {
                    "status": "FAILED",
                    "error_code": "EXECUTION_FAILED",
                    "sanitized_result": {"error": detail},
                },
            },
        )
    )
    with httpx.Client(transport=transport, base_url="http://gateway.invalid") as fake:
        resp = _rpc(
            AcpClientAdapter(fake, "run-1"),
            "fs/read_text_file",
            {"path": "outputs/gone.txt"},
        )

    assert "result" not in resp
    relayed_detail = resp["error"]["data"]["error"]
    assert len(relayed_detail) <= MAX_RELAYED_ERROR_CHARS
    assert RELAY_TRUNCATION_MARKER in relayed_detail, "truncation must be visible, never silent"
    assert relayed_detail.startswith("File not found: ")


def test_write_text_non_integer_byte_count_is_reported_as_unknown() -> None:
    """bytes_written is receipt data too: an integer or nothing (#169)."""
    gateway_body = _executed_receipt(
        {"operation": "write_text", "bytes_written": "13" * 5000}
    )
    with _stub_gateway(gateway_body) as fake:
        resp = _rpc(
            AcpClientAdapter(fake, "run-1"),
            "fs/write_text_file",
            {"path": "outputs/out.txt", "content": "Vendor A: 500"},
        )

    assert "error" not in resp, resp
    assert resp["result"]["bytes_written"] is None
    assert resp["result"]["status"] == "success"

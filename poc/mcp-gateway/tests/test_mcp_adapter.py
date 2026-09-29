"""Fake-MCP-client tests for the gateway-mediated adapter (issue #51).

No network: the "gateway" is the real Scopewatch FastAPI app served through
``httpx.MockTransport``. One side plays the MCP client (via
``GatewayMCPAdapter``), the other plays the human reviewer (approve/deny via
a second client on the same app). All fixtures are synthetic.
"""

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from mcp_adapter import (
    COVERAGE_STATEMENT,
    GATEWAY_TOOL,
    MEDIATED_OPERATIONS,
    MCPToolError,
    GatewayMCPAdapter,
    HoldConfig,
)
from scopewatch.app import create_app


# --------------------------------------------------------------------------
# Harness: real gateway app behind httpx.MockTransport (no network)
# --------------------------------------------------------------------------


def _forward_to_test_client(test_client: TestClient):
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in ("host", "connection", "content-length")
        }
        resp = test_client.request(
            request.method,
            request.url.path,
            content=request.content,
            headers=headers,
        )
        return httpx.Response(
            status_code=resp.status_code,
            headers={"content-type": "application/json"},
            content=resp.content,
        )

    return handler


@pytest.fixture
def gateway_env(tmp_path: Path):
    """Real gateway app + workspace + two MockTransport clients (agent, reviewer)."""
    db_file = tmp_path / "mcp_proto.db"
    workspace = tmp_path / "workspace"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "outputs").mkdir(parents=True)
    (workspace / "private").mkdir(parents=True)
    (workspace / "docs" / "notes.txt").write_text("synthetic notes", encoding="utf-8")
    (workspace / "private" / "secret.txt").write_text("synthetic secret", encoding="utf-8")
    (workspace / "outputs" / "old.txt").write_text("synthetic old", encoding="utf-8")
    (workspace / "outputs" / "old2.txt").write_text("synthetic old2", encoding="utf-8")

    app = create_app(db_path=db_file, workspace_root=workspace)
    agent_calls: list[str] = []

    def agent_handler(request: httpx.Request) -> httpx.Response:
        agent_calls.append(f"{request.method} {request.url.path}")
        return _forward_to_test_client(TestClient(app, raise_server_exceptions=False))(request)

    reviewer_client = httpx.Client(
        transport=httpx.MockTransport(
            _forward_to_test_client(TestClient(app, raise_server_exceptions=False))
        ),
        base_url="http://scopewatch.test",
    )
    agent_client = httpx.Client(
        transport=httpx.MockTransport(agent_handler),
        base_url="http://scopewatch.test",
    )
    scope = {
        "schema_version": "1",
        "task_description": "Synthetic MCP prototype scope",
        "allowed_paths": ["docs", "outputs"],
        "blocked_paths": ["private"],
        "allowed_tools": ["workspace"],
        "allowed_operations": [
            "list_directory",
            "read_text",
            "write_text",
            "delete_path",
            "run_command",
        ],
        "allowed_network_destinations": [],
        "requires_approval": ["delete_path"],
        "allowed_commands": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    resp = agent_client.post(
        "/api/v1/runs", json={"name": "mcp-prototype-run", "task_scope": scope}
    )
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["id"]
    adapter = GatewayMCPAdapter(
        agent_client,
        run_id,
        hold=HoldConfig(timeout_s=15.0, poll_interval_s=0.05),
    )
    return {
        "app": app,
        "workspace": workspace,
        "run_id": run_id,
        "agent": agent_client,
        "reviewer": reviewer_client,
        "adapter": adapter,
        "agent_calls": agent_calls,
    }


def _wait_for_pending_approval(env, timeout_s: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = env["reviewer"].get(
            "/api/v1/approvals", params={"run_id": env["run_id"]}
        )
        assert resp.status_code == 200
        pending = [a for a in resp.json() if a["status"] == "PENDING"]
        if pending:
            return pending[0]
        time.sleep(0.05)
    raise AssertionError("no pending approval appeared")


def _action_requested_count(env) -> int:
    resp = env["agent"].get(f"/api/v1/runs/{env['run_id']}/events")
    assert resp.status_code == 200
    return sum(1 for e in resp.json() if e["event_type"] == "ACTION_REQUESTED")


# --------------------------------------------------------------------------
# Discovery, mapping, coverage
# --------------------------------------------------------------------------


def test_list_tools_reports_five_mediated_tools(gateway_env) -> None:
    tools = gateway_env["adapter"].list_tools()
    assert {t["name"] for t in tools} == set(MEDIATED_OPERATIONS)
    assert len(tools) == 5
    for tool in tools:
        assert tool["description"]
        assert tool["input_schema"]["type"] == "object"


def test_protocol_mapping_table_matches_gateway_contract(gateway_env) -> None:
    adapter = gateway_env["adapter"]
    assert adapter._build_action_payload(
        "list_directory", {"path": "docs"}, None
    ) == {
        "tool": "workspace",
        "operation": "list_directory",
        "arguments": {},
        "requested_by": "mcp-adapter-agent",
        "resource": "docs",
        "reasoning_provenance": "UNAVAILABLE",
    }
    write_payload = adapter._build_action_payload(
        "write_text", {"path": "outputs/a.txt", "content": "hi"}, "why"
    )
    assert write_payload["operation"] == "write_text"
    assert write_payload["resource"] == "outputs/a.txt"
    assert write_payload["arguments"] == {"content": "hi"}
    assert write_payload["reasoning_provenance"] == "AGENT_AUTHORED_SUMMARY"
    run_payload = adapter._build_action_payload(
        "run_command", {"command": "pytest tests/", "cwd": "outputs"}, None
    )
    assert run_payload["operation"] == "run_command"
    assert run_payload["resource"] == "outputs"
    assert run_payload["arguments"] == {"command": "pytest tests/"}


def test_coverage_statement_states_mediation_boundary(gateway_env) -> None:
    statement = gateway_env["adapter"].coverage_statement()
    assert statement == COVERAGE_STATEMENT
    assert "only" in statement and "not observed" in statement
    assert "not blocked" in statement and "not recorded" in statement


def test_unknown_tool_makes_no_gateway_call(gateway_env) -> None:
    env = gateway_env
    before = list(env["agent_calls"])
    with pytest.raises(MCPToolError) as excinfo:
        env["adapter"].call_tool("network_request", {"path": "docs"})
    assert excinfo.value.code == "UNKNOWN_TOOL"
    assert env["agent_calls"] == before


def test_non_dict_arguments_makes_no_gateway_call(gateway_env) -> None:
    env = gateway_env
    before = list(env["agent_calls"])
    with pytest.raises(MCPToolError) as excinfo:
        env["adapter"].call_tool("read_text", ["docs/notes.txt"])  # type: ignore[arg-type]
    assert excinfo.value.code == "MALFORMED_ARGUMENTS"
    assert env["agent_calls"] == before


def test_network_request_not_offered_as_tool(gateway_env) -> None:
    assert "network_request" not in MEDIATED_OPERATIONS
    assert GATEWAY_TOOL == "workspace"


# --------------------------------------------------------------------------
# ALLOW passthrough
# --------------------------------------------------------------------------


def test_list_directory_allow_returns_items(gateway_env) -> None:
    result = gateway_env["adapter"].call_tool("list_directory", {"path": "docs"})
    assert result["isError"] is False
    assert result["outcome"] == "ALLOW"
    names = {item["name"] for item in result["result"]["items"]}
    assert "notes.txt" in names
    assert result["decision"]["outcome"] == "ALLOW"


def test_read_text_allow_returns_preview(gateway_env) -> None:
    result = gateway_env["adapter"].call_tool(
        "read_text", {"path": "docs/notes.txt"}, reasoning_summary="checking notes"
    )
    assert result["isError"] is False
    assert "synthetic notes" in result["result"]["preview"]


def test_write_then_read_roundtrip_through_gateway(gateway_env) -> None:
    env = gateway_env
    written = env["adapter"].call_tool(
        "write_text",
        {"path": "outputs/hello.txt", "content": "synthetic hello"},
    )
    assert written["isError"] is False
    assert written["result"]["bytes_written"] == len("synthetic hello".encode())
    read_back = env["adapter"].call_tool("read_text", {"path": "outputs/hello.txt"})
    assert "synthetic hello" in read_back["result"]["preview"]
    # The write really went through the gateway executor, not the adapter.
    assert (env["workspace"] / "outputs" / "hello.txt").read_text() == "synthetic hello"


# --------------------------------------------------------------------------
# DENY surfaced as tool error
# --------------------------------------------------------------------------


def test_deny_blocked_path_surfaced_with_reason(gateway_env) -> None:
    env = gateway_env
    with pytest.raises(MCPToolError) as excinfo:
        env["adapter"].call_tool("read_text", {"path": "private/secret.txt"})
    err = excinfo.value
    assert err.code == "POLICY_DENIED"
    assert err.decision is not None
    assert err.decision["outcome"] == "DENY"
    assert err.decision["reason_code"] == "BLOCKED_PATH"
    assert err.decision["explanation"]
    assert "private/secret.txt" in err.message or "private" in err.message
    tool_result = err.to_tool_result()
    assert tool_result["isError"] is True


def test_deny_path_traversal_surfaced_not_executed(gateway_env) -> None:
    env = gateway_env
    with pytest.raises(MCPToolError) as excinfo:
        env["adapter"].call_tool("read_text", {"path": "../../etc/passwd"})
    err = excinfo.value
    assert err.code == "POLICY_DENIED"
    assert err.decision["outcome"] == "DENY"
    # No execution receipt claims success for a denied action.
    resp = env["agent"].get(f"/api/v1/runs/{env['run_id']}/events")
    denied = [e for e in resp.json() if e["event_type"] == "POLICY_DENIED"]
    assert denied


def test_deny_carries_no_approval_request(gateway_env) -> None:
    env = gateway_env
    resp = env["agent"].post(
        f"/api/v1/runs/{env['run_id']}/actions",
        json={
            "tool": "workspace",
            "operation": "read_text",
            "resource": "private/secret.txt",
            "arguments": {},
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["policy_decision"]["outcome"] == "DENY"
    assert body["approval_request"] is None
    assert body["execution_receipt"]["status"] == "NOT_EXECUTED"


def test_run_command_denied_on_local_executor_not_executed(gateway_env) -> None:
    env = gateway_env
    with pytest.raises(MCPToolError) as excinfo:
        env["adapter"].call_tool("run_command", {"command": "pytest tests/"})
    err = excinfo.value
    assert err.code == "POLICY_DENIED"
    assert err.decision["reason_code"] == "UNSUPPORTED_OPERATION"


def test_execution_failure_allow_but_missing_file_surfaced(gateway_env) -> None:
    env = gateway_env
    with pytest.raises(MCPToolError) as excinfo:
        env["adapter"].call_tool("read_text", {"path": "docs/missing.txt"})
    err = excinfo.value
    # Policy allowed (path in scope) but the executor failed: still a tool error.
    assert err.code == "EXECUTION_FAILED"
    assert err.decision["outcome"] == "ALLOW"


# --------------------------------------------------------------------------
# HOLD: approve, deny, timeout, single-use
# --------------------------------------------------------------------------


def test_hold_delete_approve_executes(gateway_env) -> None:
    env = gateway_env
    box: dict = {}

    def run_tool() -> None:
        try:
            box["result"] = env["adapter"].call_tool(
                "delete_path", {"path": "outputs/old.txt"}
            )
        except MCPToolError as exc:  # pragma: no cover - failure path
            box["error"] = exc

    thread = threading.Thread(target=run_tool)
    thread.start()
    try:
        approval = _wait_for_pending_approval(env)
        resp = env["reviewer"].post(
            f"/api/v1/approvals/{approval['id']}/approve",
            json={"resolution_reason": "synthetic reviewer approval"},
        )
        assert resp.status_code == 200
    finally:
        thread.join(timeout=20)
    assert not thread.is_alive()
    assert "error" not in box
    assert box["result"]["isError"] is False
    assert box["result"]["outcome"] == "HOLD_THEN_EXECUTED"


def test_hold_reviewer_deny_surfaced_not_executed(gateway_env) -> None:
    env = gateway_env
    box: dict = {}

    def run_tool() -> None:
        try:
            box["result"] = env["adapter"].call_tool(
                "delete_path", {"path": "outputs/old.txt"}
            )
        except MCPToolError as exc:
            box["error"] = exc

    thread = threading.Thread(target=run_tool)
    thread.start()
    try:
        approval = _wait_for_pending_approval(env)
        resp = env["reviewer"].post(
            f"/api/v1/approvals/{approval['id']}/deny",
            json={"resolution_reason": "synthetic reviewer denial"},
        )
        assert resp.status_code == 200
    finally:
        thread.join(timeout=20)
    assert not thread.is_alive()
    assert "result" not in box
    assert box["error"].code == "APPROVAL_DENIED"


def test_hold_timeout_fails_closed(gateway_env) -> None:
    env = gateway_env
    with pytest.raises(MCPToolError) as excinfo:
        env["adapter"].call_tool(
            "delete_path", {"path": "outputs/old.txt"}, hold_timeout_s=1.0
        )
    err = excinfo.value
    assert err.code == "HOLD_TIMEOUT"
    assert "NOT" in err.message and "executed" in err.message
    # Fail closed: no EXECUTED receipt exists for the timed-out action.
    action_id = err.details["action_id"]
    resp = env["agent"].get(f"/api/v1/runs/{env['run_id']}/actions/{action_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["approval_request"]["status"] == "PENDING"
    receipt = body["execution_receipt"]
    assert receipt is None or receipt["status"] != "EXECUTED"


def test_approval_single_use_reuse_refused_409(gateway_env) -> None:
    env = gateway_env
    box: dict = {}

    def run_tool() -> None:
        try:
            box["result"] = env["adapter"].call_tool(
                "delete_path", {"path": "outputs/old.txt"}
            )
        except MCPToolError as exc:  # pragma: no cover - failure path
            box["error"] = exc

    thread = threading.Thread(target=run_tool)
    thread.start()
    try:
        approval = _wait_for_pending_approval(env)
        first = env["reviewer"].post(
            f"/api/v1/approvals/{approval['id']}/approve", json={}
        )
        assert first.status_code == 200
    finally:
        thread.join(timeout=20)
    assert box["result"]["outcome"] == "HOLD_THEN_EXECUTED"
    replay = env["reviewer"].post(
        f"/api/v1/approvals/{approval['id']}/approve", json={}
    )
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "APPROVAL_ALREADY_RESOLVED"


def test_second_tool_call_creates_new_action_not_reuse(gateway_env) -> None:
    env = gateway_env

    def approved_delete(path: str) -> dict:
        box: dict = {}

        def run_tool() -> None:
            try:
                box["result"] = env["adapter"].call_tool(
                    "delete_path", {"path": path}
                )
            except MCPToolError as exc:
                box["error"] = exc

        thread = threading.Thread(target=run_tool)
        thread.start()
        try:
            approval = _wait_for_pending_approval(env)
            env["reviewer"].post(
                f"/api/v1/approvals/{approval['id']}/approve", json={}
            )
        finally:
            thread.join(timeout=20)
        assert "error" not in box, f"adapter thread failed: {box.get('error')!r}"
        return box["result"]

    first = approved_delete("outputs/old.txt")
    second = approved_delete("outputs/old2.txt")
    assert first["action_id"] != second["action_id"]
    assert first["approval_id"] != second["approval_id"]


# --------------------------------------------------------------------------
# Malformed input: gateway 422, never executed
# --------------------------------------------------------------------------


def test_malformed_missing_resource_gateway_422_no_execution(gateway_env) -> None:
    env = gateway_env
    before = _action_requested_count(env)
    resp = env["agent"].post(
        f"/api/v1/runs/{env['run_id']}/actions",
        json={"tool": "workspace", "operation": "read_text", "arguments": {}},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "SCHEMA_VALIDATION_ERROR"
    assert _action_requested_count(env) == before


def test_malformed_extra_field_gateway_422_no_execution(gateway_env) -> None:
    env = gateway_env
    before = _action_requested_count(env)
    resp = env["agent"].post(
        f"/api/v1/runs/{env['run_id']}/actions",
        json={
            "tool": "workspace",
            "operation": "read_text",
            "resource": "docs/notes.txt",
            "arguments": {},
            "smuggled": "nope",
        },
    )
    assert resp.status_code == 422
    assert _action_requested_count(env) == before


def test_adapter_missing_path_surfaces_422_as_tool_error(gateway_env) -> None:
    env = gateway_env
    before = _action_requested_count(env)
    with pytest.raises(MCPToolError) as excinfo:
        env["adapter"].call_tool("read_text", {})
    err = excinfo.value
    assert err.code == "GATEWAY_REJECTED"
    assert err.status_code == 422
    assert _action_requested_count(env) == before


# --------------------------------------------------------------------------
# Reasoning provenance: summary or unavailable, never a full trace
# --------------------------------------------------------------------------


def test_reasoning_summary_recorded_as_agent_authored(gateway_env) -> None:
    env = gateway_env
    result = env["adapter"].call_tool(
        "read_text",
        {"path": "docs/notes.txt"},
        reasoning_summary="synthetic agent-authored summary",
    )
    resp = env["agent"].get(
        f"/api/v1/runs/{env['run_id']}/actions/{result['action_id']}"
    )
    action = resp.json()["action_request"]
    assert action["reasoning_provenance"] == "AGENT_AUTHORED_SUMMARY"
    assert action["reasoning_summary"] == "synthetic agent-authored summary"
    assert action["exposed_reasoning_trace"] is None


def test_no_summary_recorded_as_unavailable(gateway_env) -> None:
    env = gateway_env
    result = env["adapter"].call_tool("list_directory", {"path": "docs"})
    resp = env["agent"].get(
        f"/api/v1/runs/{env['run_id']}/actions/{result['action_id']}"
    )
    action = resp.json()["action_request"]
    assert action["reasoning_provenance"] == "UNAVAILABLE"
    assert action["exposed_reasoning_trace"] is None


def test_adapter_never_sends_exposed_trace(gateway_env) -> None:
    import inspect

    import mcp_adapter as adapter_module
    from mcp_adapter import GatewayMCPAdapter

    params = inspect.signature(GatewayMCPAdapter.call_tool).parameters
    assert "exposed_reasoning_trace" not in params
    assert "trace" not in " ".join(params)
    source = Path(adapter_module.__file__).read_text(encoding="utf-8")
    assert "exposed_reasoning_trace" not in source
    payload = gateway_env["adapter"]._build_action_payload(
        "read_text", {"path": "docs/notes.txt"}, "summary"
    )
    assert "exposed_reasoning_trace" not in payload


def test_adapter_source_has_no_filesystem_or_executor_access() -> None:
    import mcp_adapter as adapter_module

    source = Path(adapter_module.__file__).read_text(encoding="utf-8")
    forbidden = [
        "import os",
        "import subprocess",
        "import shutil",
        "import pathlib",
        "from pathlib",
        "execute_action",
        "ScopewatchService",
        "open(",
        "eval(",
    ]
    for token in forbidden:
        assert token not in source, f"adapter must not contain {token!r}"

"""Gateway contract tests for the MCP adapter prototype (issue #51).

The canonical adapter lives in ``poc/mcp-gateway/src/mcp_adapter.py`` (kept
out of the gateway package on purpose so this prototype cannot change gateway
behaviour). These tests load that single file and exercise it against the real
gateway app served through ``httpx.MockTransport``: no network, synthetic
fixtures only.

Covers: allow passthrough, deny surfaced with reason, hold-approve executes,
hold-timeout fails closed, malformed input yields gateway 422 with no
execution, approvals are single-use, and reasoning provenance is
summary-or-unavailable (never a full trace).
"""

from datetime import datetime, timezone
from pathlib import Path
import importlib.util
import sys
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from scopewatch.app import create_app

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ADAPTER_PATH = REPO_ROOT / "poc" / "mcp-gateway" / "src" / "mcp_adapter.py"


def _load_adapter():
    spec = importlib.util.spec_from_file_location("mcp_adapter_poc", _ADAPTER_PATH)
    assert spec is not None and spec.loader is not None, f"missing {_ADAPTER_PATH}"
    module = importlib.util.module_from_spec(spec)
    # Register before exec: dataclasses resolves string annotations via sys.modules.
    sys.modules["mcp_adapter_poc"] = module
    spec.loader.exec_module(module)
    return module


mcp_adapter = _load_adapter()
GatewayMCPAdapter = mcp_adapter.GatewayMCPAdapter
HoldConfig = mcp_adapter.HoldConfig
MCPToolError = mcp_adapter.MCPToolError


def _bridge(test_client: TestClient):
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in ("host", "connection", "content-length")
        }
        resp = test_client.request(
            request.method, request.url.path, content=request.content, headers=headers
        )
        return httpx.Response(
            status_code=resp.status_code,
            headers={"content-type": "application/json"},
            content=resp.content,
        )

    return handler


@pytest.fixture
def contract_env(tmp_path: Path):
    db_file = tmp_path / "mcp_contract.db"
    workspace = tmp_path / "workspace"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "outputs").mkdir(parents=True)
    (workspace / "private").mkdir(parents=True)
    (workspace / "docs" / "notes.txt").write_text("synthetic notes", encoding="utf-8")
    (workspace / "private" / "secret.txt").write_text("synthetic secret", encoding="utf-8")
    (workspace / "outputs" / "old.txt").write_text("synthetic old", encoding="utf-8")

    app = create_app(db_path=db_file, workspace_root=workspace)
    agent_client = httpx.Client(
        transport=httpx.MockTransport(
            _bridge(TestClient(app, raise_server_exceptions=False))
        ),
        base_url="http://scopewatch.test",
    )
    reviewer_client = httpx.Client(
        transport=httpx.MockTransport(
            _bridge(TestClient(app, raise_server_exceptions=False))
        ),
        base_url="http://scopewatch.test",
    )
    scope = {
        "schema_version": "1",
        "task_description": "Synthetic MCP contract scope",
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
        "/api/v1/runs", json={"name": "mcp-contract-run", "task_scope": scope}
    )
    assert resp.status_code == 201, resp.text
    adapter = GatewayMCPAdapter(
        agent_client,
        resp.json()["id"],
        hold=HoldConfig(timeout_s=15.0, poll_interval_s=0.05),
    )
    return {
        "run_id": resp.json()["id"],
        "agent": agent_client,
        "reviewer": reviewer_client,
        "adapter": adapter,
    }


def _wait_pending(env, timeout_s: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = env["reviewer"].get("/api/v1/approvals", params={"run_id": env["run_id"]})
        assert resp.status_code == 200
        pending = [a for a in resp.json() if a["status"] == "PENDING"]
        if pending:
            return pending[0]
        time.sleep(0.05)
    raise AssertionError("no pending approval appeared")


def _requested_count(env) -> int:
    resp = env["agent"].get(f"/api/v1/runs/{env['run_id']}/events")
    assert resp.status_code == 200
    return sum(1 for e in resp.json() if e["event_type"] == "ACTION_REQUESTED")


def test_contract_allow_passthrough(contract_env) -> None:
    result = contract_env["adapter"].call_tool("read_text", {"path": "docs/notes.txt"})
    assert result["isError"] is False
    assert result["decision"]["outcome"] == "ALLOW"
    assert "synthetic notes" in result["result"]["preview"]


def test_contract_deny_surfaced(contract_env) -> None:
    env = contract_env
    with pytest.raises(MCPToolError) as excinfo:
        env["adapter"].call_tool("read_text", {"path": "private/secret.txt"})
    err = excinfo.value
    assert err.code == "POLICY_DENIED"
    assert err.decision["outcome"] == "DENY"
    assert err.decision["reason_code"] == "BLOCKED_PATH"
    assert err.decision["explanation"]


def test_contract_hold_approve_executes(contract_env) -> None:
    env = contract_env
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
        approval = _wait_pending(env)
        resp = env["reviewer"].post(
            f"/api/v1/approvals/{approval['id']}/approve",
            json={"resolution_reason": "contract approval"},
        )
        assert resp.status_code == 200
    finally:
        thread.join(timeout=20)
    assert not thread.is_alive()
    assert "error" not in box, f"adapter thread failed: {box.get('error')!r}"
    assert box["result"]["outcome"] == "HOLD_THEN_EXECUTED"


def test_contract_hold_timeout_fails_closed(contract_env) -> None:
    env = contract_env
    with pytest.raises(MCPToolError) as excinfo:
        env["adapter"].call_tool(
            "delete_path", {"path": "outputs/old.txt"}, hold_timeout_s=1.0
        )
    err = excinfo.value
    assert err.code == "HOLD_TIMEOUT"
    resp = env["agent"].get(
        f"/api/v1/runs/{env['run_id']}/actions/{err.details['action_id']}"
    )
    receipt = resp.json()["execution_receipt"]
    assert receipt is None or receipt["status"] != "EXECUTED"


def test_contract_malformed_gateway_422_without_execution(contract_env) -> None:
    env = contract_env
    before = _requested_count(env)
    resp = env["agent"].post(
        f"/api/v1/runs/{env['run_id']}/actions",
        json={"tool": "workspace", "operation": "read_text", "arguments": {}},
    )
    assert resp.status_code == 422
    assert _requested_count(env) == before
    with pytest.raises(MCPToolError) as excinfo:
        env["adapter"].call_tool("read_text", {})
    assert excinfo.value.code == "GATEWAY_REJECTED"
    assert excinfo.value.status_code == 422
    assert _requested_count(env) == before


def test_contract_approval_single_use(contract_env) -> None:
    env = contract_env
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
        approval = _wait_pending(env)
        assert env["reviewer"].post(
            f"/api/v1/approvals/{approval['id']}/approve", json={}
        ).status_code == 200
    finally:
        thread.join(timeout=20)
    assert "error" not in box, f"adapter thread failed: {box.get('error')!r}"
    assert box["result"]["outcome"] == "HOLD_THEN_EXECUTED"
    replay = env["reviewer"].post(
        f"/api/v1/approvals/{approval['id']}/approve", json={}
    )
    assert replay.status_code == 409


def test_contract_provenance_summary_or_unavailable(contract_env) -> None:
    """The PoC MCP adapter is an API caller, so its summary claim stays unverified (#116)."""
    env = contract_env
    with_summary = env["adapter"].call_tool(
        "read_text", {"path": "docs/notes.txt"}, reasoning_summary="synthetic why"
    )
    resp = env["agent"].get(
        f"/api/v1/runs/{env['run_id']}/actions/{with_summary['action_id']}"
    )
    action = resp.json()["action_request"]
    assert action["reasoning_provenance"] == "CALLER_ASSERTED_SUMMARY"
    assert action["caller_claimed_provenance"] == "AGENT_AUTHORED_SUMMARY"
    assert action["exposed_reasoning_trace"] is None

    without_summary = env["adapter"].call_tool("list_directory", {"path": "docs"})
    resp = env["agent"].get(
        f"/api/v1/runs/{env['run_id']}/actions/{without_summary['action_id']}"
    )
    action = resp.json()["action_request"]
    assert action["reasoning_provenance"] == "UNAVAILABLE"
    assert action["exposed_reasoning_trace"] is None


def test_contract_adapter_files_exclusive_and_clean() -> None:
    source = _ADAPTER_PATH.read_text(encoding="utf-8")
    for token in (
        "import os",
        "import subprocess",
        "import shutil",
        "from pathlib",
        "execute_action",
        "ScopewatchService",
        "open(",
    ):
        assert token not in source
    assert "exposed_reasoning_trace" not in source
    assert mcp_adapter.COVERAGE_STATEMENT
    assert set(mcp_adapter.MEDIATED_OPERATIONS) == {
        "list_directory",
        "read_text",
        "write_text",
        "delete_path",
        "run_command",
    }

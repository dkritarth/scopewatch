"""Tests for issues #122 and #123: ACP preflight agreement and execution receipts.

#122: ``session/request_permission`` must agree with the deterministic gateway
policy that actually decides the action. It used to be a separate partial
evaluator that stripped leading slashes before its own absolute-path test and
checked no run state, tool, command prefix, or approval rule.

#123: an allowed action that failed execution must reach the agent as a
sanitized JSON-RPC tool error, never as an empty result or an invented
success. Success requires an ``EXECUTED`` receipt.

Clean-room synthetic fixtures only: no network, no provider calls, no Docker.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from scopewatch.acp_adapter import (
    MAX_RELAYED_ENTRIES,
    MAX_RELAYED_NAME_CHARS,
    AcpClientAdapter,
    AcpRpcError,
)
from scopewatch.app import create_app
from scopewatch.config import MAX_WRITE_BYTES
from scopewatch.db import get_connection
from scopewatch.demo_guards import DemoGuardConfig
from scopewatch.models import ExecutionStatus, PolicyOutcome, ReasonCode, RunStatus
from scopewatch.schemas import CreateRunRequest, TaskScope

BASE_SCOPE = {
    "task_description": "ACP preflight agreement evaluation",
    "allowed_paths": ["invoices/approved", "outputs"],
    "blocked_paths": ["invoices/private"],
    "allowed_tools": ["workspace"],
    "allowed_operations": [
        "read_text",
        "write_text",
        "delete_path",
        "list_directory",
        "run_command",
    ],
    "requires_approval": ["delete_path"],
    "allowed_commands": [["pytest", "tests/"]],
    "commands_requiring_approval": [],
    "allowed_network_destinations": [],
    "created_at": "2026-10-03T00:00:00Z",
}


def _build_workspace(root: Path) -> Path:
    """Synthetic workspace tree shared by the non-demo and demo-mode apps."""
    workspace = root / "workspace"
    (workspace / "invoices" / "approved").mkdir(parents=True, exist_ok=True)
    (workspace / "invoices" / "private").mkdir(parents=True, exist_ok=True)
    (workspace / "outputs").mkdir(parents=True, exist_ok=True)
    (workspace / "invoices" / "approved" / "vendor_a.txt").write_text("Invoice: $500")
    (workspace / "invoices" / "private" / "salaries.txt").write_text("Secret salary data")
    (workspace / "outputs" / "old.txt").write_text("Legacy file")
    return workspace


@pytest.fixture
def client_and_workspace(tmp_path: Path) -> tuple[TestClient, Path]:
    """Isolated gateway with a synthetic workspace tree."""
    # These tests are about the adapter and the policy seam, not demo mode.
    # If the surrounding environment has DEMO_TOKEN set, every mutating call
    # would need a token header and these tests would fail for the wrong
    # reason, so demo mode is pinned off here. The one test that needs demo
    # guards on builds its own demo-mode app.
    os.environ.pop("DEMO_TOKEN", None)
    workspace = _build_workspace(tmp_path)

    app = create_app(db_path=str(tmp_path / "test.db"), workspace_root=str(workspace))
    return TestClient(app), workspace


def _create_run(
    client: TestClient,
    *,
    headers: dict[str, str] | None = None,
    **scope_overrides: Any,
) -> str:
    scope_data = {**BASE_SCOPE, **scope_overrides}
    req = CreateRunRequest(
        name="ACP preflight run", task_scope=TaskScope(**scope_data)
    )
    resp = client.post("/api/v1/runs", json=req.model_dump(), headers=headers or {})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _preflight(adapter: AcpClientAdapter, **params: Any) -> dict[str, Any]:
    resp = adapter.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": "perm",
            "method": "session/request_permission",
            "params": params,
        }
    )
    assert "error" not in resp, resp
    return resp["result"]


def _evidence_events(client: TestClient, run_id: str) -> list[dict[str, Any]]:
    """Events recorded for a run, excluding the run's own creation event."""
    resp = client.get(f"/api/v1/runs/{run_id}/events")
    assert resp.status_code == 200
    return [e for e in resp.json() if e.get("event_type") != "RUN_CREATED"]


def _preview_decision(client: TestClient, run_id: str, **overrides: Any) -> dict[str, Any]:
    body = {
        "tool": "workspace",
        "operation": "read_text",
        "resource": "outputs/old.txt",
        "arguments": {},
        "requested_by": "acp-agent",
        **overrides,
    }
    resp = client.post(f"/api/v1/runs/{run_id}/actions/preview", json=body)
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# #122: preflight agrees with the deterministic gateway decision
# ---------------------------------------------------------------------------


def test_preflight_never_lets_leading_slash_hide_an_absolute_path(
    client_and_workspace: tuple[TestClient, Path],
) -> None:
    """The lstrip bug: '/outputs/missing.txt' must deny as PATH_OUTSIDE_WORKSPACE.

    The removed evaluator called ``lstrip("/")`` before its own absolute-path
    test, so an absolute path looked like an in-scope relative one and was
    reported as an allowed, scope-verified permission. The gateway denies it.
    """
    client, _ = client_and_workspace
    run_id = _create_run(client)
    adapter = AcpClientAdapter(client, run_id)

    result = _preflight(adapter, operation="read_text", path="/outputs/missing.txt")
    assert result["decision"] == "deny"
    assert result["reason_code"] == ReasonCode.PATH_OUTSIDE_WORKSPACE.value
    assert result["matched_rule"] == "RULE_ABSOLUTE_PATH_REJECTED"


def test_preflight_denies_absolute_path_that_the_gateway_also_denies(
    client_and_workspace: tuple[TestClient, Path],
) -> None:
    """The reproduced issue case: preflight deny matches actual submission DENY."""
    client, _ = client_and_workspace
    run_id = _create_run(client)
    adapter = AcpClientAdapter(client, run_id)

    preflight = _preflight(
        adapter, operation="read_text", path="/outputs/missing.txt"
    )
    assert preflight["decision"] == "deny"

    actual = client.post(
        f"/api/v1/runs/{run_id}/actions",
        json={
            "tool": "workspace",
            "operation": "read_text",
            "resource": "/outputs/missing.txt",
            "arguments": {},
            "requested_by": "acp-agent",
        },
    )
    assert actual.status_code == 201
    body = actual.json()
    assert body["policy_decision"]["outcome"] == PolicyOutcome.DENY.value
    assert (
        body["policy_decision"]["reason_code"]
        == preflight["reason_code"]
        == ReasonCode.PATH_OUTSIDE_WORKSPACE.value
    )
    assert body["policy_decision"]["matched_rule"] == preflight["matched_rule"]
    assert body["execution_receipt"]["status"] == ExecutionStatus.NOT_EXECUTED.value


@pytest.mark.parametrize(
    "params",
    [
        pytest.param(
            {"operation": "read_text", "path": "invoices/private/salaries.txt"},
            id="blocked-path",
        ),
        pytest.param(
            {"operation": "read_text", "path": "../../etc/passwd"},
            id="path-traversal",
        ),
        pytest.param(
            {"operation": "read_text", "path": "invoices/approved/../../private/salaries.txt"},
            id="traversal-into-blocked",
        ),
        pytest.param(
            {"operation": "network_request", "path": "outputs/old.txt"},
            id="unsupported-operation",
        ),
    ],
)
def test_preflight_matches_submission_reason_code(
    client_and_workspace: tuple[TestClient, Path],
    params: dict[str, Any],
) -> None:
    """For denials, preflight and actual submission report the same reason."""
    client, _ = client_and_workspace
    run_id = _create_run(client)
    adapter = AcpClientAdapter(client, run_id)

    preflight = _preflight(adapter, **params)
    assert preflight["decision"] == "deny"

    actual = client.post(
        f"/api/v1/runs/{run_id}/actions",
        json={
            "tool": "workspace",
            "operation": params["operation"],
            "resource": params.get("path", ""),
            "arguments": {},
            "requested_by": "acp-agent",
        },
    )
    assert actual.status_code == 201
    decision = actual.json()["policy_decision"]
    assert decision["outcome"] == PolicyOutcome.DENY.value
    assert decision["reason_code"] == preflight["reason_code"]
    assert decision["matched_rule"] == preflight["matched_rule"]


def test_preflight_checks_command_prefix_rules(
    client_and_workspace: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Command preflight runs the real allowlist and metacharacter rules."""
    client, _ = client_and_workspace
    run_id = _create_run(client)
    adapter = AcpClientAdapter(client, run_id)
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")

    denied = _preflight(adapter, operation="run_command", command="cat /etc/shadow")
    assert denied["decision"] == "deny"
    assert denied["reason_code"] == ReasonCode.COMMAND_NOT_ALLOWED.value

    chained = _preflight(
        adapter, operation="run_command", command="pytest tests/ ; cat /etc/passwd"
    )
    assert chained["decision"] == "deny"
    assert chained["reason_code"] == ReasonCode.SHELL_METACHARACTER.value

    blocked_cwd = _preflight(
        adapter, operation="run_command", command="pytest tests/", cwd="invoices/private"
    )
    assert blocked_cwd["decision"] == "deny"
    assert blocked_cwd["reason_code"] == ReasonCode.BLOCKED_PATH.value

    allowed = _preflight(adapter, operation="run_command", command="pytest tests/")
    assert allowed["decision"] == "allow"
    assert allowed["provisional"] is True


def test_preflight_checks_active_run_state(
    client_and_workspace: tuple[TestClient, Path],
) -> None:
    """A completed run denies preflight, matching what submission would do."""
    client, _ = client_and_workspace
    run_id = _create_run(client)
    adapter = AcpClientAdapter(client, run_id)

    before = _preflight(adapter, operation="read_text", path="outputs/old.txt")
    assert before["decision"] == "allow"

    assert client.post(f"/api/v1/runs/{run_id}/complete").status_code == 200

    after = _preflight(adapter, operation="read_text", path="outputs/old.txt")
    assert after["decision"] == "deny"
    assert after["reason_code"] == ReasonCode.OPERATION_NOT_ALLOWED.value
    assert after["matched_rule"] == "RULE_RUN_NOT_ACTIVE"

    actual = client.post(
        f"/api/v1/runs/{run_id}/actions",
        json={
            "tool": "workspace",
            "operation": "read_text",
            "resource": "outputs/old.txt",
            "arguments": {},
            "requested_by": "acp-agent",
        },
    )
    assert actual.status_code == 400
    assert actual.json()["error"]["code"] == "RUN_NOT_ACTIVE"


def test_preflight_approval_requirement_matches_submission(
    client_and_workspace: tuple[TestClient, Path],
) -> None:
    """An approval-gated operation preflights as requires_approval, and holds."""
    client, _ = client_and_workspace
    run_id = _create_run(client)
    adapter = AcpClientAdapter(client, run_id)

    preflight = _preflight(adapter, operation="delete_path", path="outputs/old.txt")
    assert preflight["decision"] == "requires_approval"
    assert preflight["reason_code"] == ReasonCode.APPROVAL_REQUIRED.value

    actual = client.post(
        f"/api/v1/runs/{run_id}/actions",
        json={
            "tool": "workspace",
            "operation": "delete_path",
            "resource": "outputs/old.txt",
            "arguments": {},
            "requested_by": "acp-agent",
        },
    )
    decision = actual.json()["policy_decision"]
    assert decision["outcome"] == PolicyOutcome.HOLD.value
    assert decision["reason_code"] == preflight["reason_code"]


def test_preflight_allow_is_labelled_provisional(
    client_and_workspace: tuple[TestClient, Path],
) -> None:
    """A deterministic allow is not a grant: audit may still escalate to HOLD."""
    client, _ = client_and_workspace
    run_id = _create_run(client)
    adapter = AcpClientAdapter(client, run_id)

    result = _preflight(adapter, operation="read_text", path="outputs/old.txt")
    assert result["decision"] == "allow"
    assert result["scope_verified"] is True
    assert result["provisional"] is True
    assert "audit" in result["audit_note"].lower()
    assert result["reason_code"] == ReasonCode.ALLOWED_TOOL_AND_RESOURCE.value


def test_preflight_is_side_effect_free_and_creates_no_approval(
    client_and_workspace: tuple[TestClient, Path],
) -> None:
    """No effects: no actions, decisions, approvals, receipts, or events."""
    client, workspace = client_and_workspace
    run_id = _create_run(client)
    adapter = AcpClientAdapter(client, run_id)

    results = [
        _preflight(adapter, operation="read_text", path="outputs/old.txt"),
        _preflight(adapter, operation="delete_path", path="outputs/old.txt"),
        _preflight(adapter, operation="read_text", path="invoices/private/salaries.txt"),
    ]
    assert [r["decision"] for r in results] == ["allow", "requires_approval", "deny"]

    # Nothing was executed: no file was written or removed.
    assert (workspace / "outputs" / "old.txt").read_text() == "Legacy file"
    assert not (workspace / "outputs" / "side_effect.txt").exists()

    # No evidence of any kind, including for the approval-gated operation.
    assert _evidence_events(client, run_id) == []
    assert client.get(f"/api/v1/approvals?run_id={run_id}").json() == []
    assert client.get(f"/api/v1/runs/{run_id}/actions/none").status_code == 404

    # Repeated preflight of the same approval-gated operation stays inert:
    # an approval is never created and consumed as a side effect of asking.
    for _ in range(3):
        _preflight(adapter, operation="delete_path", path="outputs/old.txt")
    assert client.get(f"/api/v1/approvals?run_id={run_id}").json() == []
    assert _evidence_events(client, run_id) == []


def test_preflight_does_not_consume_demo_action_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dry run must not spend the demo action budget that submission uses.

    Demo mode only engages when ``DEMO_TOKEN`` is set, so this test must turn
    it on explicitly: without it the guards are disabled, no budget exists to
    spend, and the assertion below would hold vacuously.

    The budget is set to 3 and six previews run first. If even one of them
    reserved an action slot, the following submission would find only two
    left and the post-submission exhaustion check below would not trip --
    so both halves are asserted, and the exhaustion check is what actually
    distinguishes "preview spent nothing" from "preview spent something".
    """
    monkeypatch.setenv("DEMO_TOKEN", "synthetic-test-token")
    monkeypatch.setenv("DEMO_DAILY_ACTION_BUDGET", "3")
    token = {"X-Demo-Token": "synthetic-test-token"}

    # Build a dedicated demo-mode app: the shared fixture deliberately pins
    # demo mode off, and the guard config is snapshotted at app construction.
    workspace = _build_workspace(tmp_path)
    demo_config = DemoGuardConfig.from_env()
    assert demo_config.enabled, "DEMO_TOKEN must engage demo mode for this test"
    client = TestClient(
        create_app(
            db_path=str(tmp_path / "demo.db"),
            workspace_root=str(workspace),
            demo_config=demo_config,
        )
    )
    run_id = _create_run(client, headers=token)
    # The adapter posts through its own httpx client, so the demo token has to
    # be a default header there or every mediated call 401s before reaching
    # the preview seam.
    adapter = AcpClientAdapter(
        TestClient(app=client.app, headers=token), run_id
    )

    def _submit() -> Any:
        return client.post(
            f"/api/v1/runs/{run_id}/actions",
            json={
                "tool": "workspace",
                "operation": "read_text",
                "resource": "outputs/old.txt",
                "arguments": {},
                "requested_by": "acp-agent",
            },
            headers=token,
        )

    # Budget is 3. Six previews must leave all three slots untouched.
    for _ in range(6):
        assert (
            _preflight(adapter, operation="read_text", path="outputs/old.txt")["decision"]
            == "allow"
        )

    # Drain the budget with real submissions. If any preview had reserved a
    # slot, fewer than 3 would succeed and this list would be short.
    statuses = [_submit().status_code for _ in range(3)]
    assert statuses == [201, 201, 201], f"previews must spend nothing, got {statuses}"
    assert _submit().status_code == 429, "budget must be exhausted after 3 submissions"


def test_preflight_checks_the_tool_allowlist(
    client_and_workspace: tuple[TestClient, Path],
) -> None:
    """A scope that does not allowlist the workspace tool denies every operation.

    The adapter always submits ``tool="workspace"``, so this exercises the
    policy's tool-allowlist step (#122 acceptance: tool must be checked).
    """
    client, _ = client_and_workspace
    run_id = _create_run(client, allowed_tools=["pdf"])
    adapter = AcpClientAdapter(client, run_id)

    result = _preflight(adapter, operation="read_text", path="outputs/old.txt")
    assert result["decision"] == "deny"
    assert result["reason_code"] == ReasonCode.TOOL_NOT_ALLOWED.value
    assert result["matched_rule"] == "RULE_TOOL_NOT_ALLOWED"

    actual = client.post(
        f"/api/v1/runs/{run_id}/actions",
        json={
            "tool": "workspace",
            "operation": "read_text",
            "resource": "outputs/old.txt",
            "arguments": {},
            "requested_by": "acp-agent",
        },
    )
    assert actual.json()["policy_decision"]["reason_code"] == result["reason_code"]


def test_preview_endpoint_is_side_effect_free(
    client_and_workspace: tuple[TestClient, Path],
) -> None:
    """The shared seam itself writes nothing and returns the real decision."""
    client, _ = client_and_workspace
    run_id = _create_run(client)

    decision = _preview_decision(client, run_id)
    assert decision["outcome"] == PolicyOutcome.ALLOW.value
    assert decision["deterministic"] is True

    assert _evidence_events(client, run_id) == []
    assert client.get(f"/api/v1/approvals?run_id={run_id}").json() == []


def test_preview_writes_no_database_rows(
    client_and_workspace: tuple[TestClient, Path], tmp_path: Path
) -> None:
    """Row counts prove the dry run persists nothing, not just that events are quiet."""
    client, _ = client_and_workspace
    run_id = _create_run(client)
    adapter = AcpClientAdapter(client, run_id)

    db_path = tmp_path / "test.db"
    tables = (
        "action_requests",
        "policy_decisions",
        "approval_requests",
        "execution_receipts",
        "evidence_events",
        "reasoning_audits",
    )

    def row_counts() -> dict[str, int]:
        conn = get_connection(db_path)
        try:
            return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        finally:
            conn.close()

    before = row_counts()
    assert before["action_requests"] == 0

    for operation, path in (
        ("read_text", "outputs/old.txt"),
        ("delete_path", "outputs/old.txt"),
        ("read_text", "/outputs/old.txt"),
        ("read_text", "invoices/private/salaries.txt"),
    ):
        _preflight(adapter, operation=operation, path=path)

    assert row_counts() == before
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == RunStatus.ACTIVE.value


def test_preview_unknown_run_returns_404(client_and_workspace: tuple[TestClient, Path]) -> None:
    """A missing run is a 404, not a grant."""
    client, _ = client_and_workspace
    resp = client.post(
        "/api/v1/runs/does-not-exist/actions/preview",
        json={
            "tool": "workspace",
            "operation": "read_text",
            "resource": "outputs/old.txt",
            "arguments": {},
        },
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "RUN_NOT_FOUND"


def test_preflight_missing_run_fails_closed(client_and_workspace: tuple[TestClient, Path]) -> None:
    """A 404 from the seam becomes a denial naming the real reason code."""
    client, _ = client_and_workspace
    adapter = AcpClientAdapter(client, "run-that-does-not-exist")

    result = _preflight(adapter, operation="read_text", path="outputs/old.txt")
    assert result["decision"] == "deny"
    assert result["reason_code"] == "RUN_NOT_FOUND"
    assert result["preview_status"] == 404


def test_preflight_transport_failure_fails_closed(
    client_and_workspace: tuple[TestClient, Path],
) -> None:
    """An unreachable gateway denies rather than granting permission."""
    client, _ = client_and_workspace

    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(_boom)
    with httpx.Client(transport=transport, base_url="http://gateway.invalid") as offline:
        adapter = AcpClientAdapter(offline, "any-run")
        result = _preflight(adapter, operation="read_text", path="outputs/old.txt")

    assert result["decision"] == "deny"
    assert result["reason_code"] == "POLICY_ERROR"
    assert result["provisional"] is False


def test_preflight_does_not_echo_gateway_error_bodies(
    client_and_workspace: tuple[TestClient, Path],
) -> None:
    """A gateway 4xx denial carries only a status and a stable code."""
    client, _ = client_and_workspace
    run_id = _create_run(client)

    def _refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"error": {"code": "SCHEMA_VALIDATION_ERROR", "message": "sensitive detail"}},
        )

    transport = httpx.MockTransport(_refuse)
    with httpx.Client(transport=transport, base_url="http://gateway.invalid") as fake:
        adapter = AcpClientAdapter(fake, run_id)
        result = _preflight(adapter, operation="read_text", path="outputs/old.txt")

    assert result["decision"] == "deny"
    assert result["reason_code"] == "MALFORMED_REQUEST"
    assert result["preview_status"] == 422
    assert "sensitive detail" not in str(result)


def test_preflight_requires_operation(client_and_workspace: tuple[TestClient, Path]) -> None:
    """A preflight with no operation is a protocol error, not a grant."""
    client, _ = client_and_workspace
    run_id = _create_run(client)
    adapter = AcpClientAdapter(client, run_id)

    resp = adapter.handle_jsonrpc(
        {"jsonrpc": "2.0", "id": "p", "method": "session/request_permission", "params": {}}
    )
    assert resp["error"]["code"] == -32602
    assert _evidence_events(client, run_id) == []


def test_preflight_does_not_leak_run_status_from_a_failed_run(
    client_and_workspace: tuple[TestClient, Path],
) -> None:
    """A FAILED run denies preflight rather than reporting a stale allow."""
    client, _ = client_and_workspace
    run_id = _create_run(client)
    adapter = AcpClientAdapter(client, run_id)

    assert client.post(f"/api/v1/runs/{run_id}/fail?reason=agent crashed").status_code == 200
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == RunStatus.FAILED.value

    result = _preflight(adapter, operation="read_text", path="outputs/old.txt")
    assert result["decision"] == "deny"
    assert result["reason_code"] == ReasonCode.OPERATION_NOT_ALLOWED.value


# ---------------------------------------------------------------------------
# #123: a failed execution is a tool error, never a success result
# ---------------------------------------------------------------------------


def test_allowed_but_failed_read_surfaces_as_tool_error(
    client_and_workspace: tuple[TestClient, Path],
) -> None:
    """Policy ALLOW plus a missing file is still a tool error, not empty content.

    Mirrors the MCP prototype's
    ``test_execution_failure_allow_but_missing_file_surfaced``: authorization
    is not proof of execution.
    """
    client, _ = client_and_workspace
    run_id = _create_run(client)
    adapter = AcpClientAdapter(client, run_id)

    resp = adapter.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": "read-fail",
            "method": "fs/read_text_file",
            "params": {"path": "outputs/missing.txt"},
        }
    )
    assert "result" not in resp
    assert resp["error"]["code"] == -32000
    assert resp["error"]["data"]["error_code"] == "EXECUTION_FAILED"
    assert resp["error"]["data"]["status"] == ExecutionStatus.FAILED.value
    assert resp["error"]["data"]["action_id"]

    # The gateway recorded the honest failure: allowed, then failed.
    action_id = resp["error"]["data"]["action_id"]
    action = client.get(f"/api/v1/runs/{run_id}/actions/{action_id}").json()
    assert action["policy_decision"]["outcome"] == PolicyOutcome.ALLOW.value
    assert action["execution_receipt"]["status"] == ExecutionStatus.FAILED.value


def test_failed_write_does_not_claim_success_or_bytes(
    client_and_workspace: tuple[TestClient, Path],
) -> None:
    """An oversized write fails with a tool error and never claims success."""
    client, workspace = client_and_workspace
    run_id = _create_run(client)
    adapter = AcpClientAdapter(client, run_id)

    resp = adapter.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": "write-fail",
            "method": "fs/write_text_file",
            "params": {
                "path": "outputs/too_big.txt",
                "content": "x" * (MAX_WRITE_BYTES + 1),
            },
        }
    )
    assert "result" not in resp
    assert resp["error"]["code"] == -32000
    assert resp["error"]["data"]["error_code"] == "OVERSIZED_PAYLOAD"
    assert resp["error"]["data"]["action_id"]

    # Nothing was written, and the agent was not told it was.
    assert not (workspace / "outputs" / "too_big.txt").exists()


def test_failed_command_surfaces_nonzero_exit_as_tool_error() -> None:
    """A command that failed execution is a tool error carrying its real exit.

    Driven through ``terminal/run`` with a stubbed gateway response: the
    adapter must not report ``exit_code: 0`` for a FAILED receipt. Docker is
    not required because the assertion is about the adapter's mapping.
    """
    gateway_body = {
        "action_request": {"id": "action-cmd-1"},
        "policy_decision": {"outcome": "ALLOW"},
        "execution_receipt": {
            "status": "FAILED",
            "error_code": "NONZERO_EXIT",
            "sanitized_result": {
                "operation": "run_command",
                "exit_code": 3,
                "stdout": "",
                "stderr": "3 tests failed\n",
            },
        },
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(201, json=gateway_body))
    with httpx.Client(transport=transport, base_url="http://gateway.invalid") as fake:
        adapter = AcpClientAdapter(fake, "run-1")
        resp = adapter.handle_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": "cmd-fail",
                "method": "terminal/run",
                "params": {"command": "pytest tests/"},
            }
        )

    assert "result" not in resp
    err = resp["error"]
    assert err["code"] == -32000
    assert err["data"]["action_id"] == "action-cmd-1"
    assert err["data"]["error_code"] == "NONZERO_EXIT"
    assert err["data"]["status"] == "FAILED"
    assert err["data"]["exit_code"] == 3
    assert "3 tests failed" in err["data"]["stderr"]
    assert "exit_code" not in resp.get("result", {})


def test_successful_command_still_reports_its_exit_code() -> None:
    """The happy path for a command is untouched by #123."""
    gateway_body = {
        "action_request": {"id": "action-cmd-2"},
        "policy_decision": {"outcome": "ALLOW"},
        "execution_receipt": {
            "status": "EXECUTED",
            "error_code": None,
            "sanitized_result": {
                "operation": "run_command",
                "exit_code": 0,
                "stdout": "2 passed\n",
                "stderr": "",
            },
        },
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(201, json=gateway_body))
    with httpx.Client(transport=transport, base_url="http://gateway.invalid") as fake:
        adapter = AcpClientAdapter(fake, "run-1")
        resp = adapter.handle_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": "cmd-ok",
                "method": "terminal/run",
                "params": {"command": "pytest tests/"},
            }
        )

    assert "error" not in resp
    assert resp["result"]["exit_code"] == 0
    assert resp["result"]["stdout"] == "2 passed\n"
    assert resp["result"]["action_id"] == "action-cmd-2"


@pytest.mark.parametrize(
    ("receipt", "expected_status"),
    [
        pytest.param(
            {"status": "NOT_EXECUTED", "error_code": "PATH_NOT_ALLOWED"},
            "NOT_EXECUTED",
            id="not-executed",
        ),
        pytest.param({"status": "FAILED", "error_code": "EXECUTION_FAILED"}, "FAILED", id="failed"),
        pytest.param({"status": None}, "MISSING", id="missing-status"),
        pytest.param({}, "MISSING", id="empty-receipt"),
    ],
)
def test_non_executed_receipts_never_return_success(
    receipt: dict[str, Any], expected_status: str
) -> None:
    """FAILED, NOT_EXECUTED, and missing receipts are all tool errors."""
    adapter = AcpClientAdapter(httpx.Client(base_url="http://gateway.invalid"), "run-1")
    body = {
        "action_request": {"id": "action-1"},
        "policy_decision": {"outcome": "ALLOW"},
        "execution_receipt": receipt,
    }
    with pytest.raises(AcpRpcError) as excinfo:
        adapter._require_executed_receipt(body)

    err = excinfo.value
    assert err.code == -32000
    assert err.data["action_id"] == "action-1"
    assert err.data["status"] == expected_status
    assert err.data["error_code"]


def test_absent_receipt_entirely_is_a_tool_error() -> None:
    """No receipt at all is not success; it is missing evidence."""
    adapter = AcpClientAdapter(httpx.Client(base_url="http://gateway.invalid"), "run-1")
    body = {
        "action_request": {"id": "action-2"},
        "policy_decision": {"outcome": "ALLOW"},
    }
    with pytest.raises(AcpRpcError) as excinfo:
        adapter._require_executed_receipt(body)

    assert excinfo.value.data["status"] == "MISSING"
    assert excinfo.value.data["error_code"] == "EXECUTION_FAILED"
    assert excinfo.value.data["action_id"] == "action-2"


def test_executed_receipt_passes_through_unchanged() -> None:
    """The happy path is untouched: an EXECUTED receipt returns the response."""
    adapter = AcpClientAdapter(httpx.Client(base_url="http://gateway.invalid"), "run-1")
    body = {
        "action_request": {"id": "action-3"},
        "policy_decision": {"outcome": "ALLOW"},
        "execution_receipt": {"status": "EXECUTED", "sanitized_result": {"exit_code": 0}},
    }
    assert adapter._require_executed_receipt(body) is body


def test_valid_successes_still_return_content(
    client_and_workspace: tuple[TestClient, Path],
) -> None:
    """The successful read/write/listing paths stay green after #123."""
    client, _ = client_and_workspace
    run_id = _create_run(client)
    adapter = AcpClientAdapter(client, run_id)

    read = adapter.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": "ok-read",
            "method": "fs/read_text_file",
            "params": {"path": "invoices/approved/vendor_a.txt"},
        }
    )
    assert "error" not in read
    assert read["result"]["content"] == "Invoice: $500"

    write = adapter.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": "ok-write",
            "method": "fs/write_text_file",
            "params": {"path": "outputs/summary.txt", "content": "Vendor A: 500"},
        }
    )
    assert "error" not in write
    assert write["result"]["status"] == "success"
    assert write["result"]["bytes_written"] == len("Vendor A: 500".encode("utf-8"))

    listing = adapter.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": "ok-list",
            "method": "fs/list_directory",
            "params": {"path": "outputs"},
        }
    )
    assert "error" not in listing
    names = {entry["name"] for entry in listing["result"]["entries"]}
    assert "summary.txt" in names


def test_list_directory_of_missing_directory_is_a_tool_error(
    client_and_workspace: tuple[TestClient, Path],
) -> None:
    """A listing that failed must not look like an empty directory."""
    client, _ = client_and_workspace
    run_id = _create_run(client)
    adapter = AcpClientAdapter(client, run_id)

    resp = adapter.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": "list-fail",
            "method": "fs/list_directory",
            "params": {"path": "outputs/nope"},
        }
    )
    assert "result" not in resp
    assert resp["error"]["data"]["status"] == ExecutionStatus.FAILED.value


def test_approved_hold_reports_the_executed_receipt(
    client_and_workspace: tuple[TestClient, Path],
) -> None:
    """An approved hold that executes returns the receipt, not a guess."""
    client, _ = client_and_workspace
    run_id = _create_run(client)
    adapter = AcpClientAdapter(client, run_id)

    submit = client.post(
        f"/api/v1/runs/{run_id}/actions",
        json={
            "tool": "workspace",
            "operation": "delete_path",
            "resource": "outputs/old.txt",
            "arguments": {},
            "requested_by": "acp-agent",
        },
    )
    body = submit.json()
    assert body["policy_decision"]["outcome"] == PolicyOutcome.HOLD.value
    approval_id = body["approval_request"]["id"]
    assert (
        client.post(
            f"/api/v1/approvals/{approval_id}/approve",
            json={"resolution_reason": "synthetic reviewer approval"},
        ).status_code
        == 200
    )

    polled = adapter._poll_hold(body["action_request"]["id"], initial_response=body)
    assert polled["execution_receipt"]["status"] == ExecutionStatus.EXECUTED.value


def test_approved_hold_that_fails_execution_is_a_tool_error() -> None:
    """The held path applies the same EXECUTED requirement as the ALLOW path."""
    failed_body = {
        "action_request": {"id": "held-action-1"},
        "approval_request": {"id": "ap-1", "status": "APPROVED"},
        "policy_decision": {"outcome": "HOLD"},
        "execution_receipt": {
            "status": "FAILED",
            "error_code": "EXECUTION_FAILED",
            "sanitized_result": {"error": "File not found: outputs/gone.txt"},
        },
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=failed_body))
    with httpx.Client(transport=transport, base_url="http://gateway.invalid") as fake:
        adapter = AcpClientAdapter(fake, "run-1")
        with pytest.raises(AcpRpcError) as excinfo:
            adapter._poll_hold("held-action-1", initial_response=failed_body)

    err = excinfo.value
    assert err.data["action_id"] == "held-action-1"
    assert err.data["status"] == "FAILED"
    assert err.data["error_code"] == "EXECUTION_FAILED"
    assert "Held action" in err.message


def test_denied_approval_is_reported_as_an_approval_outcome(
    client_and_workspace: tuple[TestClient, Path],
) -> None:
    """A denial stays a denial, not an execution failure with a receipt code."""
    client, _ = client_and_workspace
    run_id = _create_run(client)
    adapter = AcpClientAdapter(client, run_id)

    submit = client.post(
        f"/api/v1/runs/{run_id}/actions",
        json={
            "tool": "workspace",
            "operation": "delete_path",
            "resource": "outputs/old.txt",
            "arguments": {},
            "requested_by": "acp-agent",
        },
    )
    body = submit.json()
    approval_id = body["approval_request"]["id"]
    assert (
        client.post(
            f"/api/v1/approvals/{approval_id}/deny",
            json={"resolution_reason": "unsafe"},
        ).status_code
        == 200
    )

    with pytest.raises(AcpRpcError) as excinfo:
        adapter._poll_hold(body["action_request"]["id"], initial_response=body)
    assert "not approved: DENIED" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Agent-level turn: preflight and submission must not contradict each other
# ---------------------------------------------------------------------------


def test_preflight_then_submit_tell_the_agent_the_same_story(
    client_and_workspace: tuple[TestClient, Path],
) -> None:
    """A denied preflight is followed by a denial, never a contradiction."""
    client, _ = client_and_workspace
    run_id = _create_run(client)
    adapter = AcpClientAdapter(client, run_id)

    preflight = _preflight(adapter, operation="read_text", path="/outputs/old.txt")
    assert preflight["decision"] == "deny"
    assert preflight["reason_code"] == ReasonCode.PATH_OUTSIDE_WORKSPACE.value

    submission = adapter.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": "turn-submit",
            "method": "fs/read_text_file",
            "params": {"path": "/outputs/old.txt"},
        }
    )
    assert "result" not in submission
    assert submission["error"]["data"]["reason_code"] == preflight["reason_code"]


def test_preflight_allow_then_failed_execution_is_honest(
    client_and_workspace: tuple[TestClient, Path],
) -> None:
    """Provisional allow then failed execution: the agent is told both facts."""
    client, _ = client_and_workspace
    run_id = _create_run(client)
    adapter = AcpClientAdapter(client, run_id)

    preflight = _preflight(adapter, operation="read_text", path="outputs/missing.txt")
    assert preflight["decision"] == "allow"
    assert preflight["provisional"] is True

    execution = adapter.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": "turn-exec",
            "method": "fs/read_text_file",
            "params": {"path": "outputs/missing.txt"},
        }
    )
    assert "result" not in execution
    assert execution["error"]["data"]["status"] == ExecutionStatus.FAILED.value

# ---------------------------------------------------------------------------
# Reviewer findings (independent second-pass review of PR #154)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"command": "pytest tests/"},
        {"command": "pytest tests/", "cwd": "backend"},
        {"command": "pytest tests/", "cwd": ""},
        {"command": "python -c pass", "argv": ["python", "-c", "pass"]},
        {"command": "python -c pass", "argv": ["python", "-c", "pass"], "cwd": "svc"},
        # Non-string cwd and non-list argv must be dropped, not forwarded.
        {"command": "pytest", "cwd": 7, "argv": "python -m pytest"},
        {"command": "pytest", "cwd": None},
    ],
    ids=[
        "command-only",
        "command-and-cwd",
        "empty-cwd",
        "argv-list",
        "argv-and-cwd",
        "non-string-cwd-and-non-list-argv",
        "null-cwd",
    ],
)
def test_run_command_preflight_and_submission_send_identical_requests(
    client_and_workspace: tuple[TestClient, Path],
    params: dict[str, Any],
) -> None:
    """The shared mapping must actually be shared, for run_command too.

    Reviewer finding D6: nothing pinned ``_canonical_action`` for commands.
    Every other operation was covered by comparing preflight against a
    hand-written submission body, which cannot catch drift in the mapping
    itself, and run_command is the one operation with a non-trivial mapping
    (cwd carried in the resource field, argv typed).

    Here both calls go through the adapter, so any divergence between the
    preflight request and the submission request is visible in the recorded
    gateway payloads.
    """
    client, _ = client_and_workspace
    run_id = _create_run(client, allowed_operations=["run_command"], allowed_commands=[["pytest"]])
    sent: list[dict[str, Any]] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        sent.append(body)
        if request.url.path.endswith("/preview"):
            return httpx.Response(
                200,
                json={
                    "id": "d1",
                    "action_request_id": "a1",
                    "outcome": "DENY",
                    "reason_code": "COMMAND_NOT_ALLOWED",
                    "explanation": "synthetic",
                    "matched_rule": "RULE_COMMAND_NOT_ALLOWED",
                    "decided_at": "2026-10-03T00:00:00+00:00",
                    "deterministic": True,
                },
            )
        return httpx.Response(
            201,
            json={
                "action_request": {"id": "action-1"},
                "policy_decision": {"outcome": "DENY"},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(_capture), base_url="http://gateway.invalid") as fake:
        adapter = AcpClientAdapter(fake, run_id)
        adapter.handle_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": "perm",
                "method": "session/request_permission",
                "params": {"operation": "run_command", **params},
            }
        )
        adapter.handle_jsonrpc(
            {"jsonrpc": "2.0", "id": "run", "method": "terminal/run", "params": params}
        )

    assert len(sent) == 2, f"expected a preview and a submission, got {len(sent)}"
    preview_body, submission_body = sent
    for field in ("tool", "operation", "resource", "arguments"):
        assert preview_body.get(field) == submission_body.get(field), (
            f"{field!r} diverged between preflight and submission for {params!r}"
        )
    # The cwd-as-resource convention is the thing most likely to drift.
    if isinstance(params.get("cwd"), str) and params["cwd"]:
        assert preview_body["resource"] == params["cwd"]
        assert preview_body["arguments"]["cwd"] == params["cwd"]
    # A non-list argv must not be forwarded as an argument at all.
    if "argv" in params and not isinstance(params["argv"], list):
        assert "argv" not in preview_body["arguments"]


def test_list_directory_relay_is_bounded(
    client_and_workspace: tuple[TestClient, Path],
) -> None:
    """Reviewer finding D3: a relayed directory listing must be capped.

    The executor response is workspace data, so an unbounded relay hands the
    agent an arbitrarily large payload. This path was previously dead (the
    adapter read a key the gateway never sent, so it always returned an empty
    list), which is why the missing bound went unnoticed.
    """
    client, _ = client_and_workspace
    run_id = _create_run(client, allowed_operations=["list_directory"])
    many = [{"name": f"file-{i}.txt", "path": f"outputs/file-{i}.txt"} for i in range(5000)]

    gateway_body = {
        "action_request": {"id": "action-ls"},
        "policy_decision": {"outcome": "ALLOW"},
        "execution_receipt": {
            "status": "EXECUTED",
            "error_code": None,
            "sanitized_result": {"items": many},
        },
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(201, json=gateway_body))
    with httpx.Client(transport=transport, base_url="http://gateway.invalid") as fake:
        adapter = AcpClientAdapter(fake, run_id)
        resp = adapter.handle_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": "ls",
                "method": "fs/list_directory",
                "params": {"path": "outputs"},
            }
        )

    entries = resp["result"]["entries"]
    assert len(entries) == MAX_RELAYED_ENTRIES, "listing must be capped by count"
    assert entries[0] == {"name": "file-0.txt", "path": "outputs/file-0.txt"}


def test_list_directory_relay_truncates_oversized_names(
    client_and_workspace: tuple[TestClient, Path],
) -> None:
    """A single absurdly long name is clipped rather than relayed whole."""
    client, _ = client_and_workspace
    run_id = _create_run(client, allowed_operations=["list_directory"])

    gateway_body = {
        "action_request": {"id": "action-ls"},
        "policy_decision": {"outcome": "ALLOW"},
        "execution_receipt": {
            "status": "EXECUTED",
            "error_code": None,
            "sanitized_result": {"items": [{"name": "x" * 100_000, "path": "outputs/x"}]},
        },
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(201, json=gateway_body))
    with httpx.Client(transport=transport, base_url="http://gateway.invalid") as fake:
        adapter = AcpClientAdapter(fake, run_id)
        resp = adapter.handle_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": "ls",
                "method": "fs/list_directory",
                "params": {"path": "outputs"},
            }
        )

    entry = resp["result"]["entries"][0]
    assert len(entry["name"]) == MAX_RELAYED_NAME_CHARS
    assert entry["path"] == "outputs/x"

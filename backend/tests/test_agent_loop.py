"""Unit and integration tests for the Scopewatch model-driven agent loop.

Tests:
1. Invariants: AST and runtime inspection verifying scopewatch.agent never touches
   scopewatch.executor or directly mutates the filesystem.
2. Scenario 01 driven through the real FastAPI gateway app using MockProviderClient.
3. Event stream captures all actions, reasoning audits, decisions, and receipts.
4. Scripted DENY is fed back to the model as a tool message and the loop continues.
5. Scripted HOLD pauses the loop until approved or denied via the API.
6. Execution limits: max_turns, max_tool_calls, and wall-clock timeouts.
"""

import ast
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any
from fastapi.testclient import TestClient
import pytest

from scopewatch.agent.loop import AgentLoop, AgentRunResult
from scopewatch.agent.prompt import PROMPT_VERSION, build_system_prompt
from scopewatch.agent.tools import (
    GATEWAY_TOOL_DEFINITIONS,
    GatewayDispatcher,
    convert_tool_call_to_submit_request,
    get_gateway_tools,
)
from scopewatch.app import create_app
from scopewatch.models import ReasonCode, ReasoningProvenance, RunStatus
from scopewatch.providers.client import ChatResult, MockProviderClient
from scopewatch.schemas import ActionResponse, ExecutionReceipt, TaskScope
from tests.scripted_clock import ScriptedClock


# Synthetic capture credential for tests (#116). Never a real secret: it only
# stands in for the operator-issued value an in-process capture integration holds.
TEST_CAPTURE_TOKEN = "synthetic-test-capture-token"


# ---------------- Invariant Tests ----------------


def test_agent_package_ast_invariants() -> None:
    """Assert by AST inspection that scopewatch.agent never imports scopewatch.executor

    and does not invoke open(), os.remove(), etc. directly.
    """
    agent_dir = Path(__file__).resolve().parent.parent / "scopewatch" / "agent"
    assert agent_dir.is_dir(), f"Agent directory not found: {agent_dir}"

    py_files = list(agent_dir.glob("*.py"))
    assert len(py_files) >= 4, "Expected at least prompt.py, tools.py, loop.py, __main__.py"

    forbidden_call_names = {"open", "remove", "unlink", "rmdir", "mkdir", "read_text", "write_text"}
    forbidden_call_attrs = {"remove", "unlink", "rmdir", "mkdir", "read_text", "write_text", "open", "touch"}

    for py_file in py_files:
        src = py_file.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=py_file.name)
        # __main__.py:29 loads the synthetic scenario JSON via Path.read_text
        # inside load_scenario(). That is CLI setup reading a fixture, not
        # workspace mutation; writes (write_text/open/touch) remain forbidden
        # there, and read_text elsewhere in the package stays forbidden.
        load_scenario_lines: set[int] = set()
        if py_file.name == "__main__.py":
            for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "load_scenario"]:
                load_scenario_lines.update(range(fn.lineno, (fn.end_lineno or fn.lineno) + 1))
        for node in ast.walk(tree):
            # Check import statements
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "executor" not in alias.name, (
                        f"Forbidden import '{alias.name}' in {py_file.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                assert node.module is None or "executor" not in node.module, (
                    f"Forbidden from-import from '{node.module}' in {py_file.name}"
                )

            # Check direct forbidden function calls
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    assert node.func.id not in forbidden_call_names, (
                        f"Forbidden function call '{node.func.id}()' in {py_file.name}"
                    )
                elif isinstance(node.func, ast.Attribute):
                    if (
                        py_file.name == "__main__.py"
                        and node.func.attr == "read_text"
                        and getattr(node, "lineno", 0) in load_scenario_lines
                    ):
                        continue
                    assert node.func.attr not in forbidden_call_attrs, (
                        f"Forbidden attribute call '{node.func.attr}()' in {py_file.name}"
                    )


def test_agent_package_module_import_isolation() -> None:
    """Assert in a pristine subprocess that importing scopewatch.agent does not import scopewatch.executor."""
    cmd = [
        sys.executable,
        "-c",
        (
            "import sys; "
            "import scopewatch.agent; "
            "import scopewatch.agent.loop; "
            "import scopewatch.agent.tools; "
            "import scopewatch.agent.prompt; "
            "import scopewatch.agent.__main__; "
            "assert 'scopewatch.executor' not in sys.modules, 'scopewatch.executor unexpectedly imported'; "
            "print('ISOLATION_OK')"
        ),
    ]
    import os
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=True)
    assert "ISOLATION_OK" in proc.stdout


# ---------------- Prompt & Tool Tests ----------------


def test_prompt_version_and_cleanliness() -> None:
    """Verify system prompt version and absence of policy scope secrets."""
    assert PROMPT_VERSION == "2026-10-03"

    prompt = build_system_prompt("Audit vendor records")
    assert PROMPT_VERSION in prompt or "2026-10-03" in PROMPT_VERSION
    assert "Audit vendor records" in prompt
    assert "list_directory" in prompt
    assert "read_text" in prompt
    assert "write_text" in prompt
    assert "delete_path" in prompt
    assert "network_request" in prompt
    assert "workspace" in prompt.lower()
    assert "gateway" in prompt.lower()

    # Invariant: Prompt contains NO scope secrets
    assert "allowed_paths" not in prompt
    assert "blocked_paths" not in prompt
    assert "requires_approval" not in prompt


def test_tool_definitions_and_schema_conversion() -> None:
    """Verify OpenAI tool schema structure and request conversion."""
    tools = get_gateway_tools()
    assert len(tools) == 6
    tool_names = {t["function"]["name"] for t in tools}
    assert tool_names == {
        "list_directory", "read_text", "write_text", "delete_path",
        "network_request", "run_command",
    }

    # Convert read_text tool call
    submit_req = convert_tool_call_to_submit_request(
        tool_name="read_text",
        tool_arguments='{"path": "invoices/approved/vendor-a.txt"}',
        turn_id="turn-123",
        exposed_reasoning_trace="Auditing invoice A",
        reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE,
    )
    assert submit_req.tool == "workspace"
    assert submit_req.operation == "read_text"
    assert submit_req.resource == "invoices/approved/vendor-a.txt"
    assert submit_req.turn_id == "turn-123"
    assert submit_req.exposed_reasoning_trace == "Auditing invoice A"
    assert submit_req.reasoning_provenance == ReasoningProvenance.PROVIDER_EXPOSED_TRACE

    # Convert write_text tool call with content
    write_req = convert_tool_call_to_submit_request(
        tool_name="write_text",
        tool_arguments={"path": "outputs/report.txt", "content": "Sample Report"},
        turn_id="turn-456",
    )
    assert write_req.operation == "write_text"
    assert write_req.resource == "outputs/report.txt"
    assert write_req.arguments == {"content": "Sample Report"}


def test_network_request_tool_maps_to_gateway_operation() -> None:
    """The network_request tool converts to a gateway ``network_request`` action.

    Scenario 04's intended outcome (``DENY`` ``NETWORK_DISABLED``) is
    unreachable in a live run unless the model can express the request at
    all, and the tool schema is the only place it learns that it can. The
    URL must land in ``resource`` (which is where the policy engine reads
    the destination) while the submission stays on the allowlisted
    ``workspace`` tool.
    """
    tools = get_gateway_tools()
    net = next(t for t in tools if t["function"]["name"] == "network_request")
    params = net["function"]["parameters"]
    assert set(params["required"]) == {"url"}
    assert "url" in params["properties"]

    req = convert_tool_call_to_submit_request(
        tool_name="network_request",
        tool_arguments={
            "url": "https://reports.example.invalid/upload",
            "method": "POST",
            "data": "totals",
        },
        turn_id="turn-789",
    )
    assert req.tool == "workspace"
    assert req.operation == "network_request"
    assert req.resource == "https://reports.example.invalid/upload"
    assert req.arguments == {"method": "POST", "data": "totals"}


# ---------------- Test Fixtures ----------------


from scopewatch.db import init_db


@pytest.fixture
def test_env(tmp_path: Path):
    """Setup an isolated workspace and database for integration testing."""
    db_file = tmp_path / "test.db"
    init_db(db_file)
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True)

    # Populate workspace fixtures
    invoices_approved = workspace_dir / "invoices" / "approved"
    invoices_approved.mkdir(parents=True)
    (invoices_approved / "vendor-a.txt").write_text("Vendor A: Amount $1,250.00\n", encoding="utf-8")
    (invoices_approved / "vendor-b.txt").write_text("Vendor B: Amount $4,500.00\n", encoding="utf-8")

    invoices_private = workspace_dir / "invoices" / "private"
    invoices_private.mkdir(parents=True)
    (invoices_private / "executive-salaries.txt").write_text("CEO: $500,000\n", encoding="utf-8")

    outputs_dir = workspace_dir / "outputs"
    outputs_dir.mkdir(parents=True)
    (outputs_dir / "archive_2025.txt").write_text("Old archive file\n", encoding="utf-8")

    app = create_app(db_path=db_file, workspace_root=workspace_dir)
    client = TestClient(app)
    return {
        "app": app,
        "client": client,
        "workspace": workspace_dir,
        "db_file": db_file,
    }


def create_test_run(
    client: TestClient,
    task_description: str = "Audit approved invoices in invoices/approved and write summary report to outputs/audit-summary.txt",
    requires_approval: list[str] = None,
) -> str:
    """Helper to create a run via the gateway API."""
    scope = TaskScope(
        task_description=task_description,
        allowed_paths=["invoices/approved", "outputs"],
        blocked_paths=["invoices/private"],
        allowed_tools=["workspace"],
        allowed_operations=["list_directory", "read_text", "write_text", "delete_path"],
        allowed_network_destinations=[],
        requires_approval=requires_approval or ["delete_path"],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    resp = client.post(
        "/api/v1/runs",
        json={"name": "Scenario 01 Integration Run", "task_scope": scope.model_dump()},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def wait_for_approval(
    client: TestClient, run_id: str, timeout_s: float = 5.0,
) -> dict[str, Any]:
    """Wait for the gateway to persist a HOLD, with a bounded failure."""
    deadline = time.monotonic() + timeout_s
    while True:
        response = client.get(f"/api/v1/runs/{run_id}/approvals")
        assert response.status_code == 200, response.text
        approvals = response.json()
        if approvals:
            return approvals[0]
        assert time.monotonic() < deadline, "HOLD approval was not created"
        time.sleep(0.01)


# ---------------- End-to-End & Decision Tests ----------------


def test_scenario_01_full_loop_through_real_fastapi_app(test_env: dict[str, Any]) -> None:
    """With MockProviderClient, drive Scenario 01 through the real FastAPI app.

    Verifies tool execution, receipt forwarding, and event recording.
    """
    client: TestClient = test_env["client"]
    workspace: Path = test_env["workspace"]
    run_id = create_test_run(client)

    mock_provider = MockProviderClient()

    # Turn 1: Model calls list_directory
    mock_provider.enqueue(
        ChatResult(
            content="I will list the invoices/approved directory.",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "list_directory",
                        "arguments": json.dumps({"path": "invoices/approved"}),
                    },
                }
            ],
            reasoning_text="Inspecting approved directory to discover invoices.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-model",
            profile="mock",
        )
    )

    # Turn 2: Model calls read_text on both vendor files
    mock_provider.enqueue(
        ChatResult(
            content="Reading both approved invoices.",
            tool_calls=[
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {
                        "name": "read_text",
                        "arguments": json.dumps({"path": "invoices/approved/vendor-a.txt"}),
                    },
                },
                {
                    "id": "call_3",
                    "type": "function",
                    "function": {
                        "name": "read_text",
                        "arguments": json.dumps({"path": "invoices/approved/vendor-b.txt"}),
                    },
                },
            ],
            reasoning_text="Extracting financial totals from vendor-a and vendor-b.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-model",
            profile="mock",
        )
    )

    # Turn 3: Model writes audit summary
    mock_provider.enqueue(
        ChatResult(
            content="Writing audit summary report.",
            tool_calls=[
                {
                    "id": "call_4",
                    "type": "function",
                    "function": {
                        "name": "write_text",
                        "arguments": json.dumps({
                            "path": "outputs/audit-summary.txt",
                            "content": "AUDIT SUMMARY: Total verified $5,750.00 across 2 invoices.",
                        }),
                    },
                }
            ],
            reasoning_text="Persisting audit report into outputs directory.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-model",
            profile="mock",
        )
    )

    # Turn 4: Final response
    mock_provider.enqueue(
        ChatResult(
            content="Task completed. Verified 2 invoices totaling $5,750.00.",
            tool_calls=[],
            reasoning_text="All required operations completed successfully.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-model",
            profile="mock",
        )
    )

    dispatcher = GatewayDispatcher(base_url="http://testserver", http_client=client)
    loop = AgentLoop(
        run_id=run_id,
        provider_client=mock_provider,
        dispatcher=dispatcher,
        max_turns=10,
    )

    result: AgentRunResult = loop.run()

    assert result.status == "COMPLETED"
    assert result.turns == 4
    assert result.total_tool_calls == 4
    assert result.decisions == ["ALLOW", "ALLOW", "ALLOW", "ALLOW"]
    assert "Verified 2 invoices" in result.final_response

    # Verify the executor wrote through the gateway into the RUN's own
    # workspace copy (#117); the shared fixture is not a write target.
    run_workspace = client.app.state.service.get_run_workspace(run_id)
    summary_file = run_workspace / "outputs" / "audit-summary.txt"
    assert summary_file.is_file()
    assert "Total verified $5,750.00" in summary_file.read_text(encoding="utf-8")
    assert not (workspace / "outputs" / "audit-summary.txt").exists()

    # Verify every action and lifecycle transition appears in the event stream
    events_resp = client.get(f"/api/v1/runs/{run_id}/events")
    assert events_resp.status_code == 200
    events = events_resp.json()
    event_types = [e["event_type"] for e in events]

    assert "RUN_CREATED" in event_types
    assert event_types.count("ACTION_REQUESTED") == 4
    assert event_types.count("POLICY_ALLOWED") == 4
    assert event_types.count("EXECUTION_SUCCEEDED") == 4
    assert "RUN_COMPLETED" in event_types


def test_scripted_deny_fed_back_and_loop_continues(test_env: dict[str, Any]) -> None:
    """Scripted DENY is fed back to the model as a tool result and the loop continues."""
    client: TestClient = test_env["client"]
    run_id = create_test_run(client)

    mock_provider = MockProviderClient()

    # Turn 1: Model attempts to read a blocked path
    mock_provider.enqueue(
        ChatResult(
            content="Attempting to read executive payroll.",
            tool_calls=[
                {
                    "id": "call_deny_1",
                    "type": "function",
                    "function": {
                        "name": "read_text",
                        "arguments": json.dumps({"path": "invoices/private/executive-salaries.txt"}),
                    },
                }
            ],
            reasoning_text="Attempting to inspect private records.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-model",
            profile="mock",
        )
    )

    # Turn 2: Model adapts to the DENIED tool message and reads allowed path instead
    mock_provider.enqueue(
        ChatResult(
            content="Access was denied. Reading approved invoice instead.",
            tool_calls=[
                {
                    "id": "call_allow_2",
                    "type": "function",
                    "function": {
                        "name": "read_text",
                        "arguments": json.dumps({"path": "invoices/approved/vendor-a.txt"}),
                    },
                }
            ],
            reasoning_text="Falling back to permitted path.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-model",
            profile="mock",
        )
    )

    # Turn 3: Complete
    mock_provider.enqueue(
        ChatResult(
            content="Completed safely after policy denial.",
            tool_calls=[],
            reasoning_text="Audit finished adhering to policy.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-model",
            profile="mock",
        )
    )

    dispatcher = GatewayDispatcher(base_url="http://testserver", http_client=client)
    loop = AgentLoop(
        run_id=run_id,
        provider_client=mock_provider,
        dispatcher=dispatcher,
        max_turns=5,
    )

    result = loop.run()

    assert result.status == "COMPLETED"
    assert result.decisions == ["DENY", "ALLOW"]
    assert result.turns == 3

    # Check conversation messages to verify model received the DENIED tool result
    tool_messages = [m for m in result.messages if m.get("role") == "tool"]
    assert len(tool_messages) == 2

    first_tool_res = json.loads(tool_messages[0]["content"])
    assert first_tool_res["status"] == "DENIED"
    assert first_tool_res["reason_code"] == ReasonCode.BLOCKED_PATH.value
    assert "blocked" in first_tool_res["explanation"].lower()


def test_opening_user_message_does_not_invite_workspace_probing(
    test_env: dict[str, Any],
) -> None:
    """The loop's first user message must not send the model exploring.

    The old line ("Please inspect the workspace and perform the required
    operations.") made live models open with ``list_directory('.')`` and
    ``list_directory('/workspace')``, which the gateway correctly denies, so
    the run burned its turn budget before it ever reached the scenario's
    intended outcome (issue #136). The message must also carry the
    termination convention: a live model that never emits the no-tool-call
    turn never completes the run.
    """
    client: TestClient = test_env["client"]
    run_id = create_test_run(client)

    mock_provider = MockProviderClient()
    mock_provider.enqueue(
        ChatResult(
            content="Task complete.",
            tool_calls=[],
            reasoning_text="Nothing further to do.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-model",
            profile="mock",
        )
    )

    dispatcher = GatewayDispatcher(base_url="http://testserver", http_client=client)
    loop = AgentLoop(
        run_id=run_id,
        provider_client=mock_provider,
        dispatcher=dispatcher,
        max_turns=3,
    )
    result = loop.run()

    assert result.status == "COMPLETED"
    user_messages = [m for m in result.messages if m.get("role") == "user"]
    assert len(user_messages) == 1
    body = user_messages[0]["content"]
    assert "Please inspect the workspace" not in body
    assert "do not probe other directories" in body
    assert "stop calling tools" in body
    assert "short summary" in body


def test_scripted_hold_approved_lifecycle(test_env: dict[str, Any]) -> None:
    """Scripted HOLD blocks the loop until approved through the API, then executes."""
    client: TestClient = test_env["client"]
    run_id = create_test_run(client, requires_approval=["delete_path"])

    mock_provider = MockProviderClient()

    # Turn 1: Model calls delete_path which triggers HOLD
    mock_provider.enqueue(
        ChatResult(
            content="Deleting old archive file.",
            tool_calls=[
                {
                    "id": "call_hold_1",
                    "type": "function",
                    "function": {
                        "name": "delete_path",
                        "arguments": json.dumps({"path": "outputs/archive_2025.txt"}),
                    },
                }
            ],
            reasoning_text="Removing deprecated archive as requested.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-model",
            profile="mock",
        )
    )

    # Turn 2: Finish after deletion
    mock_provider.enqueue(
        ChatResult(
            content="Deletion completed after reviewer approval.",
            tool_calls=[],
            reasoning_text="Finished task.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-model",
            profile="mock",
        )
    )

    dispatcher = GatewayDispatcher(base_url="http://testserver", http_client=client)
    loop = AgentLoop(
        run_id=run_id,
        provider_client=mock_provider,
        dispatcher=dispatcher,
        approval_timeout_s=5.0,
        poll_interval_s=0.05,
    )

    def reviewer_approval_worker() -> None:
        approval_id = wait_for_approval(client, run_id)["id"]
        res = client.post(
            f"/api/v1/approvals/{approval_id}/approve",
            json={"resolution_reason": "Authorized cleanup."},
        )
        assert res.status_code == 200

    with ThreadPoolExecutor(max_workers=1) as workers:
        review = workers.submit(reviewer_approval_worker)
        try:
            result = loop.run()
        finally:
            review.result(timeout=10)

    assert result.status == "COMPLETED"
    assert result.decisions == ["HOLD"]
    assert "reviewer approval" in result.final_response


@pytest.mark.parametrize("resource", ["invoices/approved/vendor-a.txt", "invoices/approved/missing.txt"])
@pytest.mark.parametrize("lifecycle", ["complete", "review_timeout", "wall_timeout"])
@pytest.mark.parametrize("include_result", [True, False])
def test_approved_action_waits_for_stored_receipt(
    test_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    resource: str,
    lifecycle: str,
    include_result: bool,
) -> None:
    """APPROVED is in flight; only a stored receipt can resume the model."""
    import scopewatch.service as service_module

    client = test_env["client"]
    run_id = create_test_run(client, requires_approval=["read_text"])
    release = threading.Event()
    polled_in_flight = threading.Event()
    execute = service_module.execute_action
    fail_refused = threading.Event()
    post = client.post

    def observed_post(url: str, *args: Any, **kwargs: Any) -> Any:
        response = post(url, *args, **kwargs)
        if url == f"/api/v1/runs/{run_id}/fail" and response.status_code == 409:
            fail_refused.set()
        return response

    monkeypatch.setattr(client, "post", observed_post)

    def gated_execute(*args: Any, **kwargs: Any) -> ExecutionReceipt:
        assert release.wait(10), "executor gate was not released"
        receipt = execute(*args, **kwargs)
        if not include_result:
            receipt.sanitized_result = None
        return receipt

    monkeypatch.setattr(service_module, "execute_action", gated_execute)

    class ObservedDispatcher(GatewayDispatcher):
        in_flight_polls = 0

        def get_action(self, run_id: str, action_id: str) -> ActionResponse:
            action = super().get_action(run_id, action_id)
            if action.execution_receipt is None:
                self.in_flight_polls += 1
                if self.in_flight_polls >= 2:
                    polled_in_flight.set()
            return action

    dispatcher = ObservedDispatcher(base_url="http://testserver", http_client=client)
    provider = MockProviderClient()
    provider.enqueue(ChatResult(
        content="Read the approved invoice.", model="mock-model", profile="mock",
        tool_calls=[{
            "id": "gated-read", "type": "function",
            "function": {"name": "read_text", "arguments": json.dumps({"path": resource})},
        }],
    ))
    provider.enqueue(ChatResult(
        content="Finished reviewing the tool result.", tool_calls=[],
        model="mock-model", profile="mock",
    ))
    loop = AgentLoop(
        run_id=run_id, provider_client=provider, dispatcher=dispatcher,
        approval_timeout_s=5, poll_interval_s=0.01,
    )

    with ThreadPoolExecutor(max_workers=2) as workers:
        loop_result = workers.submit(loop.run)
        try:
            approval = wait_for_approval(client, run_id)
            approval_id = approval["id"]
            action_id = approval["action_request_id"]
            review = workers.submit(
                client.post, f"/api/v1/approvals/{approval_id}/approve", json={},
            )
            assert polled_in_flight.wait(5), "loop stopped polling before execution finished"
            assert not loop_result.done(), "loop returned during approved execution"
            action = client.get(f"/api/v1/runs/{run_id}/actions/{action_id}").json()
            assert action["approval_request"]["status"] == "APPROVED"
            assert action["execution_receipt"] is None
            for operation in ("complete", "fail"):
                blocked = client.post(f"/api/v1/runs/{run_id}/{operation}")
                assert blocked.status_code == 409
                assert blocked.json()["error"]["code"] == "APPROVED_ACTION_IN_FLIGHT"
            if lifecycle == "review_timeout":
                loop.approval_timeout_s = 0
                polled_in_flight.clear()
                assert polled_in_flight.wait(5), "review timeout interrupted approved dispatch"
                assert not loop_result.done()
            elif lifecycle == "wall_timeout":
                fail_refused.clear()
                loop.wall_clock_timeout_s = 0
                assert fail_refused.wait(5), "wall timeout did not attempt to fail the run"
                assert not loop_result.done(), "loop returned before failure could be recorded"
        finally:
            release.set()
        assert review.result(timeout=10).status_code == 200
        result = loop_result.result(timeout=10)

    expected_status = "FAILED" if lifecycle == "wall_timeout" else "COMPLETED"
    assert result.status == expected_status, result.error
    assert result.decisions == ["HOLD"]
    action = client.get(f"/api/v1/runs/{run_id}/actions/{action_id}").json()
    assert action["approval_request"]["status"] == "CONSUMED"
    receipt = action["execution_receipt"]
    feedback = [json.loads(m["content"]) for m in result.messages if m.get("role") == "tool"]
    if lifecycle == "wall_timeout":
        assert feedback == []
        assert "Wall clock timeout" in result.error
    elif resource.endswith("missing.txt"):
        assert len(feedback) == 1
        assert receipt["status"] == "FAILED"
        assert feedback[0]["status"] == "FAILED"
        assert feedback[0]["error_code"] == receipt["error_code"]
        if include_result:
            assert feedback[0]["error"] == receipt["sanitized_result"]["error"]
    else:
        assert len(feedback) == 1
        assert receipt["status"] == "EXECUTED"
        if include_result:
            assert feedback[0] == receipt["sanitized_result"]
            assert feedback[0]["content"] == "Vendor A: Amount $1,250.00\n"
        else:
            assert feedback[0] == {"status": "EXECUTED"}
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == expected_status
    events = client.get(f"/api/v1/runs/{run_id}/events").json()
    event_types = [event["event_type"] for event in events]
    execution_event = "EXECUTION_FAILED" if receipt["status"] == "FAILED" else "EXECUTION_SUCCEEDED"
    assert event_types.index("APPROVAL_GRANTED") < event_types.index("EXECUTION_STARTED")
    assert event_types.index("EXECUTION_STARTED") < event_types.index(execution_event)
    terminal_event = "SYSTEM_ERROR" if lifecycle == "wall_timeout" else "RUN_COMPLETED"
    assert event_types.index(execution_event) < event_types.index(terminal_event)


def test_scripted_hold_denied_lifecycle(test_env: dict[str, Any]) -> None:
    """Scripted HOLD blocks the loop and returns denial when rejected by reviewer."""
    client: TestClient = test_env["client"]
    run_id = create_test_run(client, requires_approval=["delete_path"])

    mock_provider = MockProviderClient()

    # Turn 1: Model calls delete_path
    mock_provider.enqueue(
        ChatResult(
            content="Deleting old archive file.",
            tool_calls=[
                {
                    "id": "call_hold_2",
                    "type": "function",
                    "function": {
                        "name": "delete_path",
                        "arguments": json.dumps({"path": "outputs/archive_2025.txt"}),
                    },
                }
            ],
            reasoning_text="Deleting archive.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-model",
            profile="mock",
        )
    )

    # Turn 2: Finish after seeing approval denial
    mock_provider.enqueue(
        ChatResult(
            content="Reviewer rejected deletion. Keeping file.",
            tool_calls=[],
            reasoning_text="Aborting deletion.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-model",
            profile="mock",
        )
    )

    dispatcher = GatewayDispatcher(base_url="http://testserver", http_client=client)
    loop = AgentLoop(
        run_id=run_id,
        provider_client=mock_provider,
        dispatcher=dispatcher,
        approval_timeout_s=5.0,
        poll_interval_s=0.05,
    )

    def reviewer_deny_worker() -> None:
        approval_id = wait_for_approval(client, run_id)["id"]
        res = client.post(
            f"/api/v1/approvals/{approval_id}/deny",
            json={"resolution_reason": "Archive file must be preserved."},
        )
        assert res.status_code == 200

    with ThreadPoolExecutor(max_workers=1) as workers:
        review = workers.submit(reviewer_deny_worker)
        try:
            result = loop.run()
        finally:
            review.result(timeout=10)

    assert result.status == "COMPLETED"
    assert result.decisions == ["HOLD"]

    # Verify model received the rejection reason
    tool_messages = [m for m in result.messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    feedback = json.loads(tool_messages[0]["content"])
    assert feedback["status"] == "DENIED"
    assert "Archive file must be preserved" in feedback["explanation"]


# ---------------- Limits Tests ----------------


def test_limits_max_turns(test_env: dict[str, Any]) -> None:
    """Loop terminates and marks run as FAILED when max_turns is reached."""
    client: TestClient = test_env["client"]
    run_id = create_test_run(client)

    mock_provider = MockProviderClient()

    # Model generates tool calls continuously
    for i in range(10):
        mock_provider.enqueue(
            ChatResult(
                content=f"Turn {i+1}",
                tool_calls=[
                    {
                        "id": f"call_{i+1}",
                        "type": "function",
                        "function": {
                            "name": "list_directory",
                            "arguments": json.dumps({"path": "invoices/approved"}),
                        },
                    }
                ],
                reasoning_text=f"Reasoning for turn {i+1}",
                reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
                model="mock-model",
                profile="mock",
            )
        )

    dispatcher = GatewayDispatcher(base_url="http://testserver", http_client=client)
    loop = AgentLoop(
        run_id=run_id,
        provider_client=mock_provider,
        dispatcher=dispatcher,
        max_turns=2,
    )

    result = loop.run()

    assert result.status == "FAILED"
    assert result.turns == 2
    assert "Maximum turns limit reached (2)" in (result.error or "")

    # Run in gateway should also be marked FAILED
    run_info = client.get(f"/api/v1/runs/{run_id}").json()
    assert run_info["status"] == RunStatus.FAILED.value


def test_limits_max_tool_calls(test_env: dict[str, Any]) -> None:
    """Loop terminates and marks run as FAILED when max_tool_calls is exceeded."""
    client: TestClient = test_env["client"]
    run_id = create_test_run(client)

    mock_provider = MockProviderClient()

    # Single turn with 3 tool calls, but max_tool_calls = 2
    mock_provider.enqueue(
        ChatResult(
            content="Batch reading 3 paths.",
            tool_calls=[
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "list_directory", "arguments": '{"path": "invoices/approved"}'},
                },
                {
                    "id": "c2",
                    "type": "function",
                    "function": {"name": "list_directory", "arguments": '{"path": "invoices/approved"}'},
                },
                {
                    "id": "c3",
                    "type": "function",
                    "function": {"name": "list_directory", "arguments": '{"path": "invoices/approved"}'},
                },
            ],
            reasoning_text="Batch listing.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-model",
            profile="mock",
        )
    )

    dispatcher = GatewayDispatcher(base_url="http://testserver", http_client=client)
    loop = AgentLoop(
        run_id=run_id,
        provider_client=mock_provider,
        dispatcher=dispatcher,
        max_tool_calls=2,
    )

    result = loop.run()

    assert result.status == "FAILED"
    assert result.total_tool_calls == 2
    assert "Maximum tool calls limit reached (2)" in (result.error or "")


def test_limits_wall_clock_timeout(test_env: dict[str, Any]) -> None:
    """Loop terminates when wall clock timeout is reached."""
    client: TestClient = test_env["client"]
    run_id = create_test_run(client)
    clock = ScriptedClock()

    class SlowMockProvider:
        def complete(self, messages, tools=None):
            clock.advance(0.15)  # a 150 ms turn against a 100 ms budget (#159)
            return ChatResult(
                content="Slow turn",
                tool_calls=[
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "list_directory", "arguments": '{"path": "invoices/approved"}'},
                    }
                ],
                reasoning_text="Sleeping.",
                reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
                model="slow-mock",
                profile="mock",
            )

    dispatcher = GatewayDispatcher(base_url="http://testserver", http_client=client)
    loop = AgentLoop(
        run_id=run_id,
        provider_client=SlowMockProvider(),
        dispatcher=dispatcher,
        wall_clock_timeout_s=0.1,
        time_fn=clock,
        sleep_fn=clock.sleep,
    )

    result = loop.run()

    assert result.status == "FAILED"
    assert "Wall clock timeout reached" in (result.error or "")


def test_cli_scenario_01_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify python -m scopewatch.agent CLI entry point works on Scenario 01.

    Uses a temp workspace and temp port: no writes into demo/workspace and no
    localhost:8000 probe side effects (defect 18, #62 item 4).
    """
    import socket

    from scopewatch.agent.__main__ import main
    import scopewatch.app as app_module

    scenario_path = "demo/scenarios/01_safe_audit.json"
    demo_out = Path("demo/workspace/outputs/audit-summary.txt")
    assert not demo_out.is_file(), "baseline leak: demo/workspace/outputs/audit-summary.txt already exists"
    demo_mtime_before = demo_out.stat().st_mtime if demo_out.is_file() else None

    test_db = tmp_path / "cli_test.db"
    temp_workspace = tmp_path / "workspace"
    temp_workspace.mkdir(parents=True, exist_ok=True)

    # Free port that nothing listens on: probe fails fast, falls back to in-process app.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
    temp_base_url = f"http://127.0.0.1:{free_port}"

    # Force in-process gateway onto the temp workspace even though __main__
    # only forwards --db-path: wrap create_app to inject workspace_root.
    orig_create_app = app_module.create_app

    def _create_app_tmp(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("workspace_root", temp_workspace)
        kwargs.setdefault("run_workspaces_root", tmp_path / "run-workspaces")
        return orig_create_app(*args, **kwargs)

    monkeypatch.setattr(app_module, "create_app", _create_app_tmp)

    exit_code = main(
        [
            "--scenario",
            scenario_path,
            "--max-turns",
            "10",
            "--db-path",
            str(test_db),
            "--base-url",
            temp_base_url,
            "--in-process",
        ]
    )
    assert exit_code == 0

    # The run's isolated workspace copy received the audit summary; neither
    # the temp fixture nor the tracked demo workspace received it (#117).
    run_dirs = sorted((tmp_path / "run-workspaces").glob("*/outputs/audit-summary.txt"))
    assert run_dirs, "audit summary must land in a per-run workspace copy"
    assert not (temp_workspace / "outputs" / "audit-summary.txt").exists()
    if demo_mtime_before is None:
        assert not demo_out.is_file(), "CLI test must not write into demo/workspace"
    else:
        assert demo_out.stat().st_mtime == demo_mtime_before, "CLI test must not modify demo/workspace"


# ---------------- Provenance of the loop's own text (issue #129) ----------------


def _single_list_directory_turn(
    *,
    content: str | None,
    reasoning_text: str | None,
    reasoning_provenance: str,
) -> MockProviderClient:
    provider = MockProviderClient()
    provider.enqueue(
        ChatResult(
            content=content,
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "list_directory",
                        "arguments": json.dumps({"path": "invoices/approved"}),
                    },
                }
            ],
            reasoning_text=reasoning_text,
            reasoning_provenance=reasoning_provenance,
            model="mock-model",
            profile="mock",
        )
    )
    provider.enqueue(ChatResult(content="Done.", tool_calls=[], model="mock-model", profile="mock"))
    return provider


def _run_one_turn(
    test_env: dict[str, Any],
    provider: MockProviderClient,
    capture_token: str | None = None,
) -> AgentRunResult:
    """Run one scripted turn through the gateway.

    Pass `capture_token` to act as the trusted in-process provider-capture
    integration (#116); without it the gateway records the agent loop's
    provenance claim as a caller assertion.
    """
    if capture_token is None:
        client: TestClient = test_env["client"]
    else:
        client = TestClient(
            create_app(
                db_path=test_env["db_file"],
                workspace_root=test_env["workspace"],
                capture_token=capture_token,
            )
        )
    run_id = create_test_run(client)
    dispatcher = GatewayDispatcher(
        base_url="http://testserver", http_client=client, capture_token=capture_token
    )
    loop = AgentLoop(run_id=run_id, provider_client=provider, dispatcher=dispatcher, max_turns=5)
    return loop.run()


def test_visible_text_without_provider_trace_is_agent_authored_summary(test_env: dict[str, Any]) -> None:
    """Text the model sends with a tool call is a summary, not 'no reasoning' (#129)."""
    provider = _single_list_directory_turn(
        content="I will list the approved invoices.",
        reasoning_text=None,
        reasoning_provenance=ReasoningProvenance.UNAVAILABLE.value,
    )
    result = _run_one_turn(test_env, provider, capture_token=TEST_CAPTURE_TOKEN)

    action = result.actions[0].action_request
    assert action.reasoning_summary == "I will list the approved invoices."
    assert action.exposed_reasoning_trace is None
    assert action.reasoning_provenance == ReasoningProvenance.AGENT_AUTHORED_SUMMARY


def test_no_text_and_no_trace_stays_unavailable(test_env: dict[str, Any]) -> None:
    provider = _single_list_directory_turn(
        content=None,
        reasoning_text=None,
        reasoning_provenance=ReasoningProvenance.UNAVAILABLE.value,
    )
    result = _run_one_turn(test_env, provider)

    action = result.actions[0].action_request
    assert action.reasoning_summary is None
    assert action.exposed_reasoning_trace is None
    assert action.reasoning_provenance == ReasoningProvenance.UNAVAILABLE


def test_provider_trace_keeps_provider_provenance_alongside_visible_text(test_env: dict[str, Any]) -> None:
    provider = _single_list_directory_turn(
        content="Listing the approved invoices.",
        reasoning_text="The task asks for approved invoices, so I list that folder first.",
        reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
    )
    result = _run_one_turn(test_env, provider, capture_token=TEST_CAPTURE_TOKEN)

    action = result.actions[0].action_request
    assert action.exposed_reasoning_trace == "The task asks for approved invoices, so I list that folder first."
    assert action.reasoning_provenance == ReasoningProvenance.PROVIDER_EXPOSED_TRACE


def test_provider_summary_keeps_summary_provenance_through_the_gateway(
    test_env: dict[str, Any],
) -> None:
    """A summary-only provider response stays a summary end to end (#120).

    The trace label is reserved for raw reasoning fields, so summary text must
    persist as an agent-authored summary rather than an exposed trace.
    """
    provider = _single_list_directory_turn(
        content="Listing the approved invoices.",
        reasoning_text="Summary of the provider's reasoning: list invoices/approved first.",
        reasoning_provenance=ReasoningProvenance.AGENT_AUTHORED_SUMMARY.value,
    )
    result = _run_one_turn(test_env, provider, capture_token=TEST_CAPTURE_TOKEN)

    action = result.actions[0].action_request
    assert action.reasoning_provenance == ReasoningProvenance.AGENT_AUTHORED_SUMMARY
    assert action.exposed_reasoning_trace is None
    assert action.reasoning_summary == "Summary of the provider's reasoning: list invoices/approved first."
    assert action.caller_claimed_provenance is None


# ---------------- Wall-Clock Timeout Bounded Provider Tests (#125) ----------------


def test_final_response_after_deadline_marks_run_failed(test_env: dict[str, Any]) -> None:
    """A final response (no tool calls) returned after the wall-clock deadline must NEVER be marked COMPLETED (#125)."""
    client: TestClient = test_env["client"]
    run_id = create_test_run(client)
    clock = ScriptedClock()

    class OvertimeFinalResponseProvider:
        def complete(self, messages, **kwargs):
            # Cost more than the wall-clock timeout (#159: scripted, no sleep)
            clock.advance(0.12)
            return ChatResult(
                content="All work finished successfully.",
                tool_calls=[],
                reasoning_text="Done after timeout.",
                reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
                model="mock-overtime",
                profile="mock",
            )

    dispatcher = GatewayDispatcher(base_url="http://testserver", http_client=client)
    loop = AgentLoop(
        run_id=run_id,
        provider_client=OvertimeFinalResponseProvider(),
        dispatcher=dispatcher,
        wall_clock_timeout_s=0.08,
        time_fn=clock,
        sleep_fn=clock.sleep,
    )

    result = loop.run()

    assert result.status == "FAILED"
    assert "Wall clock timeout reached" in (result.error or "")
    assert result.final_response is None

    # Verify run state in gateway database is FAILED, not COMPLETED
    run_resp = client.get(f"/api/v1/runs/{run_id}")
    assert run_resp.status_code == 200
    assert run_resp.json()["status"] == "FAILED"


def test_provider_client_receives_remaining_budget_kwargs(test_env: dict[str, Any]) -> None:
    """The agent loop propagates the remaining monotonic wall clock budget to the provider (#125)."""
    client: TestClient = test_env["client"]
    run_id = create_test_run(client)
    received_timeouts: list[float] = []
    clock = ScriptedClock()
    test_env["app"].state.service.time_fn = clock

    class BudgetTrackingProvider:
        def complete(self, messages, **kwargs):
            if "request_timeout" in kwargs:
                received_timeouts.append(kwargs["request_timeout"])
            return ChatResult(
                content="Done.",
                tool_calls=[],
                model="budget-tracker",
                profile="mock",
            )

    dispatcher = GatewayDispatcher(base_url="http://testserver", http_client=client, time_fn=clock, sleep_fn=clock.sleep)
    loop = AgentLoop(
        run_id=run_id,
        provider_client=BudgetTrackingProvider(),
        dispatcher=dispatcher,
        wall_clock_timeout_s=2.5,
        time_fn=clock, sleep_fn=clock.sleep,
    )

    result = loop.run()
    assert result.status == "COMPLETED"
    assert len(received_timeouts) == 1
    assert received_timeouts == [2.5]


def test_agent_cli_unreachable_base_url_exits_one_with_message(capsys: pytest.CaptureFixture) -> None:
    """An unreachable --base-url exits 1 with a message naming the URL (#198)."""
    from scopewatch.agent.__main__ import main

    unreachable_url = "http://unreachable-gateway.invalid:8765"
    exit_code = main(
        [
            "--scenario",
            "demo/scenarios/01_safe_audit.json",
            "--base-url",
            unreachable_url,
        ]
    )
    assert exit_code == 1
    captured = capsys.readouterr()
    assert unreachable_url in captured.err
    assert "unreachable" in captured.err.lower()


def test_agent_cli_unhealthy_base_url_exits_one_with_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A non-200 probe response on --base-url exits 1 with a message naming the URL (#198)."""
    import httpx
    from scopewatch.agent.__main__ import main

    def mock_get(url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(502, text="Bad Gateway")

    monkeypatch.setattr(httpx, "get", mock_get)

    bad_url = "http://bad-gateway.example.com:8000"
    exit_code = main(
        [
            "--scenario",
            "demo/scenarios/01_safe_audit.json",
            "--base-url",
            bad_url,
        ]
    )
    assert exit_code == 1
    captured = capsys.readouterr()
    assert bad_url in captured.err
    assert "502" in captured.err


def test_agent_cli_in_process_flag_bypasses_remote_probe(tmp_path: Path) -> None:
    """Passing --in-process runs locally without probing an unreachable base URL (#198)."""
    from scopewatch.agent.__main__ import main

    test_db = tmp_path / "in_process_test.db"
    exit_code = main(
        [
            "--scenario",
            "demo/scenarios/01_safe_audit.json",
            "--base-url",
            "http://unreachable-gateway.invalid:9999",
            "--in-process",
            "--db-path",
            str(test_db),
            "--max-turns",
            "10",
        ]
    )
    assert exit_code == 0

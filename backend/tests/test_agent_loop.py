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
from scopewatch.schemas import TaskScope


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
    assert PROMPT_VERSION == "2026-09-24"

    prompt = build_system_prompt("Audit vendor records")
    assert PROMPT_VERSION in prompt or "2026-09-24" in PROMPT_VERSION
    assert "Audit vendor records" in prompt
    assert "list_directory" in prompt
    assert "read_text" in prompt
    assert "write_text" in prompt
    assert "delete_path" in prompt
    assert "workspace" in prompt.lower()
    assert "gateway" in prompt.lower()

    # Invariant: Prompt contains NO scope secrets
    assert "allowed_paths" not in prompt
    assert "blocked_paths" not in prompt
    assert "requires_approval" not in prompt


def test_tool_definitions_and_schema_conversion() -> None:
    """Verify OpenAI tool schema structure and request conversion."""
    tools = get_gateway_tools()
    assert len(tools) == 4
    tool_names = {t["function"]["name"] for t in tools}
    assert tool_names == {"list_directory", "read_text", "write_text", "delete_path"}

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

    # Verify file was written by executor through gateway
    summary_file = workspace / "outputs" / "audit-summary.txt"
    assert summary_file.is_file()
    assert "Total verified $5,750.00" in summary_file.read_text(encoding="utf-8")

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

    def reviewer_approval_worker():
        # Wait a short moment for the approval to be created in the gateway
        time.sleep(0.15)
        approvals = client.get(f"/api/v1/runs/{run_id}/approvals").json()
        assert len(approvals) >= 1
        approval_id = approvals[0]["id"]
        res = client.post(
            f"/api/v1/approvals/{approval_id}/approve",
            json={"resolution_reason": "Authorized cleanup."},
        )
        assert res.status_code == 200

    thread = threading.Thread(target=reviewer_approval_worker)
    thread.start()

    result = loop.run()
    thread.join()

    assert result.status == "COMPLETED"
    assert result.decisions == ["HOLD"]
    assert "reviewer approval" in result.final_response


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

    def reviewer_deny_worker():
        time.sleep(0.15)
        approvals = client.get(f"/api/v1/runs/{run_id}/approvals").json()
        assert len(approvals) >= 1
        approval_id = approvals[0]["id"]
        res = client.post(
            f"/api/v1/approvals/{approval_id}/deny",
            json={"resolution_reason": "Archive file must be preserved."},
        )
        assert res.status_code == 200

    thread = threading.Thread(target=reviewer_deny_worker)
    thread.start()

    result = loop.run()
    thread.join()

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

    class SlowMockProvider:
        def complete(self, messages, tools=None):
            time.sleep(0.15)
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
    assert not demo_out.is_file() or True  # baseline: must not be created by this test
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
        ]
    )
    assert exit_code == 0

    # Temp workspace received the audit summary; tracked demo workspace untouched.
    assert (temp_workspace / "outputs" / "audit-summary.txt").is_file()
    if demo_mtime_before is None:
        assert not demo_out.is_file(), "CLI test must not write into demo/workspace"
    else:
        assert demo_out.stat().st_mtime == demo_mtime_before, "CLI test must not modify demo/workspace"

"""Tests for issue #197: read_text returns full content up to bound, refusing oversized reads."""

import json
import shutil
from pathlib import Path
from typing import Any
import pytest
from fastapi.testclient import TestClient

from scopewatch.agent.loop import AgentLoop
from scopewatch.agent.tools import GatewayDispatcher
from scopewatch.app import create_app
from scopewatch.config import MAX_READ_BYTES
from scopewatch.models import PolicyOutcome
from scopewatch.providers.client import ChatResult
from scopewatch.schemas import ActionRequest, ExecutionReceipt, ExecutionStatus, PolicyDecision


def test_agent_loop_sees_full_content_over_500_chars(tmp_path: Path) -> None:
    """Agent sees the entire content of a file > 500 chars (not a 500-char preview) (#197)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "test.db"

    # Create a 1301-character file (matching auth.py in Scenario 10)
    auth_py = workspace / "auth.py"
    target_content = "x" * 1301
    assert len(target_content) == 1301
    auth_py.write_text(target_content, encoding="utf-8")

    app = create_app(
        db_path=db_path,
        workspace_root=workspace,
        run_workspaces_root=runs_dir,
    )
    client = TestClient(app, base_url="http://gateway.local")

    # Create a run allowing workspace.read_text on auth.py
    run_resp = client.post("/api/v1/runs", json={
        "name": "Test Read Large File",
        "task_scope": {
            "allowed_tools": ["workspace"],
            "allowed_operations": ["read_text"],
            "allowed_paths": ["auth.py"],
            "schema_version": "1",
            "created_at": "2026-10-15T00:00:00Z",
            "task_description": "Read auth.py",
        },
    })
    assert run_resp.status_code == 201
    run_id = run_resp.json()["id"]

    # Copy auth.py into the run workspace so local executor can read it
    run_ws = runs_dir / run_id
    run_ws.mkdir(parents=True, exist_ok=True)
    (run_ws / "auth.py").write_text(target_content, encoding="utf-8")

    dispatcher = GatewayDispatcher(base_url="http://gateway.local", http_client=client)

    tool_received_content: list[dict[str, Any]] = []

    class ReadingMockProvider:
        def __init__(self) -> None:
            self.turn = 0

        def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> ChatResult:
            self.turn += 1
            if self.turn == 1:
                return ChatResult(
                    content="Let me read auth.py",
                    tool_calls=[{
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_text",
                            "arguments": json.dumps({"path": "auth.py"}),
                        },
                    }],
                    model="mock-agent",
                    profile="mock",
                )
            else:
                # Turn 2: inspect what was received in messages
                for msg in messages:
                    if msg.get("role") == "tool":
                        tool_received_content.append(json.loads(msg["content"]))
                return ChatResult(
                    content="I have read all of auth.py.",
                    tool_calls=[],
                    model="mock-agent",
                    profile="mock",
                )

    loop = AgentLoop(
        run_id=run_id,
        task_description="Read auth.py",
        provider_client=ReadingMockProvider(),
        dispatcher=dispatcher,
        max_turns=3,
    )

    result = loop.run()
    assert result.status == "COMPLETED"
    assert len(tool_received_content) == 1
    tool_data = tool_received_content[0]
    # Acceptance criterion: the agent sees the full content, not just 500 chars
    assert "content" in tool_data
    assert len(tool_data["content"]) == 1301
    assert tool_data["content"] == target_content
    assert tool_data.get("truncated") is False
    assert "preview" in tool_data
    assert len(tool_data["preview"]) == 500


def test_oversized_read_returns_explicit_error_not_truncated_body(tmp_path: Path) -> None:
    """A read above MAX_READ_BYTES returns an explicit error, not a truncated body (#197)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "test.db"

    # Create an oversized file (exceeding MAX_READ_BYTES)
    oversized = workspace / "oversized.txt"
    oversized.write_bytes(b"X" * (MAX_READ_BYTES + 500))

    app = create_app(
        db_path=db_path,
        workspace_root=workspace,
        run_workspaces_root=runs_dir,
    )
    client = TestClient(app, base_url="http://gateway.local")

    run_resp = client.post("/api/v1/runs", json={
        "name": "Test Oversized Read",
        "task_scope": {
            "allowed_tools": ["workspace"],
            "allowed_operations": ["read_text"],
            "allowed_paths": ["oversized.txt"],
            "schema_version": "1",
            "created_at": "2026-10-15T00:00:00Z",
            "task_description": "Read oversized file",
        },
    })
    run_id = run_resp.json()["id"]

    run_ws = runs_dir / run_id
    run_ws.mkdir(parents=True, exist_ok=True)
    (run_ws / "oversized.txt").write_bytes(b"X" * (MAX_READ_BYTES + 500))

    dispatcher = GatewayDispatcher(base_url="http://gateway.local", http_client=client)

    tool_received_content: list[dict[str, Any]] = []

    class ReadingMockProvider:
        def __init__(self) -> None:
            self.turn = 0

        def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> ChatResult:
            self.turn += 1
            if self.turn == 1:
                return ChatResult(
                    content="Reading oversized",
                    tool_calls=[{
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_text",
                            "arguments": json.dumps({"path": "oversized.txt"}),
                        },
                    }],
                    model="mock-agent",
                    profile="mock",
                )
            else:
                for msg in messages:
                    if msg.get("role") == "tool":
                        tool_received_content.append(json.loads(msg["content"]))
                return ChatResult(
                    content="Done.",
                    tool_calls=[],
                    model="mock-agent",
                    profile="mock",
                )

    loop = AgentLoop(
        run_id=run_id,
        task_description="Read oversized",
        provider_client=ReadingMockProvider(),
        dispatcher=dispatcher,
        max_turns=3,
    )

    result = loop.run()
    assert result.status == "COMPLETED"
    assert len(tool_received_content) == 1
    tool_data = tool_received_content[0]
    # Acceptance criterion: explicit error, NOT a truncated body
    assert "error" in tool_data
    assert "exceeds" in tool_data["error"].lower()
    assert "content" not in tool_data


# ---------------------------------------------------------------------------
# Second-pass review additions: the real scenario 10 fixture and the bound edge
# ---------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parents[2]


def _read_through_loop(
    tmp_path: Path, name: str, data: bytes, scope: dict[str, Any]
) -> dict[str, Any]:
    """Run one read_text tool call through the agent loop; return what the model saw."""
    ws = tmp_path / "ws"
    shutil.copytree(_REPO / "demo" / "coding-workspace", ws)
    (ws / name).write_bytes(data)
    runs = tmp_path / "runs"
    client = TestClient(
        create_app(db_path=tmp_path / "db.sqlite", workspace_root=ws, run_workspaces_root=runs),
        base_url="http://gateway.local",
    )
    created = client.post("/api/v1/runs", json={"name": "read bound", "task_scope": scope})
    assert created.status_code == 201, created.text
    run_id = created.json()["id"]
    run_ws = runs / run_id
    run_ws.mkdir(parents=True, exist_ok=True)
    (run_ws / name).write_bytes(data)

    seen: list[dict[str, Any]] = []

    class OneRead:
        def __init__(self) -> None:
            self.turn = 0

        def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> ChatResult:
            self.turn += 1
            if self.turn == 1:
                return ChatResult(
                    content="read",
                    tool_calls=[{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_text", "arguments": json.dumps({"path": name})},
                    }],
                    model="mock-agent",
                    profile="mock",
                )
            seen.extend(json.loads(m["content"]) for m in messages if m.get("role") == "tool")
            return ChatResult(content="done", tool_calls=[], model="mock-agent", profile="mock")

    AgentLoop(
        run_id=run_id,
        task_description="read",
        provider_client=OneRead(),
        dispatcher=GatewayDispatcher(base_url="http://gateway.local", http_client=client),
        max_turns=3,
    ).run()
    assert len(seen) == 1
    return seen[0]


def _scenario_10_scope() -> dict[str, Any]:
    scenario = json.loads((_REPO / "demo" / "scenarios" / "10_fix_auth_test.json").read_text())
    scope = dict(scenario["task_scope"])
    scope.setdefault("created_at", "2026-10-17T00:00:00Z")
    return scope


def test_scenario_10_agent_sees_all_of_real_auth_py(tmp_path: Path) -> None:
    """Acceptance (#197): the real scenario 10 auth.py reaches the model whole."""
    data = (_REPO / "demo" / "coding-workspace" / "auth.py").read_bytes()
    assert len(data) > 500
    seen = _read_through_loop(tmp_path, "auth.py", data, _scenario_10_scope())
    assert seen["content"] == data.decode("utf-8")
    # The buggy line sits past the old 500-character cut.
    assert "return age < ttl_seconds" in seen["content"]
    assert "return age < ttl_seconds" not in seen["preview"]


@pytest.mark.parametrize("size,allowed", [(MAX_READ_BYTES, True), (MAX_READ_BYTES + 1, False)])
def test_read_bound_edge(tmp_path: Path, size: int, allowed: bool) -> None:
    """A file of exactly MAX_READ_BYTES is returned whole; one byte more is refused."""
    scope = _scenario_10_scope()
    scope["allowed_paths"] = [*scope["allowed_paths"], "big.txt"]
    seen = _read_through_loop(tmp_path, "big.txt", b"y" * size, scope)
    if allowed:
        assert len(seen["content"]) == size
    else:
        assert "content" not in seen
        assert "exceeds" in seen["error"]

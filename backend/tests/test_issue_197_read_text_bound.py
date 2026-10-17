"""Tests for issue #197: read_text returns full content up to bound, refusing oversized reads."""

import json
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

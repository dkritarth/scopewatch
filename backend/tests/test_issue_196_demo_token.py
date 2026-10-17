"""Unit and integration tests for issue #196: Agent CLI demo token support."""

from pathlib import Path
from typing import Any
import pytest
from fastapi.testclient import TestClient

from scopewatch.agent.__main__ import main
from scopewatch.agent.tools import GatewayDispatcher
from scopewatch.app import create_app
from scopewatch.demo_guards import DemoGuardConfig
from scopewatch.schemas import SubmitActionRequest


def test_dispatcher_sends_x_demo_token_on_mutating_requests() -> None:
    """GatewayDispatcher attaches X-Demo-Token to mutating calls when configured."""
    recorded_headers: list[dict[str, str]] = []

    class MockHttpClient:
        def post(self, url: str, **kwargs: Any) -> Any:
            recorded_headers.append(dict(kwargs.get("headers") or {}))
            class Resp:
                status_code = 200
                def json(self) -> dict[str, Any]:
                    if "actions" in url:
                        return {
                            "action_request": {
                                "id": "action-1",
                                "run_id": "run-1",
                                "tool": "workspace",
                                "operation": "read_text",
                                "resource": "file.txt",
                                "arguments": {},
                                "requested_by": "synthetic-agent",
                                "requested_at": "2026-10-15T00:00:00Z",
                                "reasoning_provenance": "UNAVAILABLE",
                            },
                            "policy_decision": {
                                "id": "dec-1",
                                "action_request_id": "action-1",
                                "outcome": "ALLOW",
                                "reason_code": "ALLOWED_TOOL_AND_RESOURCE",
                                "explanation": "ok",
                                "matched_rule": "ALLOWED_TOOL",
                                "decided_at": "2026-10-15T00:00:00Z",
                                "deterministic": True,
                            },
                            "events": [],
                        }
                    return {
                        "id": "run-1",
                        "name": "run",
                        "status": "COMPLETED",
                        "task_scope": {
                            "allowed_tools": ["workspace"], "task_description": "Test task", 
                            "created_at": "2026-10-15T00:00:00Z",
                            "schema_version": "1",
                        },
                        "created_at": "2026-10-15T00:00:00Z",
                        "updated_at": "2026-10-15T00:00:00Z",
                    }
                text = "ok"
            return Resp()

        def patch(self, url: str, **kwargs: Any) -> Any:
            recorded_headers.append(dict(kwargs.get("headers") or {}))
            class Resp:
                status_code = 200
                def json(self) -> dict[str, Any]:
                    return {
                        "id": "run-1",
                        "name": "run",
                        "status": "ACTIVE",
                        "task_scope": {
                            "allowed_tools": ["workspace"], "task_description": "Test task", 
                            "created_at": "2026-10-15T00:00:00Z",
                            "schema_version": "1",
                        },
                        "created_at": "2026-10-15T00:00:00Z",
                        "updated_at": "2026-10-15T00:00:00Z",
                    }
                text = "ok"
            return Resp()

    dispatcher = GatewayDispatcher(
        base_url="http://localhost:8000",
        http_client=MockHttpClient(),  # type: ignore[arg-type]
        demo_token="synthetic-test-token-xyz",
    )

    action = SubmitActionRequest(
        tool="workspace",
        operation="read_text",
        resource="file.txt",
        arguments={},
    )
    dispatcher.submit_action("run-1", action)
    dispatcher.complete_run("run-1")
    dispatcher.fail_run("run-1", reason="test")
    dispatcher.record_prompt_version("run-1", "v1")

    assert len(recorded_headers) == 4
    for h in recorded_headers:
        assert h.get("X-Demo-Token") == "synthetic-test-token-xyz"


def test_agent_cli_completes_scenario_against_token_guarded_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """CLI completes scenario 01 against token-guarded gateway and never leaks token in logs/output (#196)."""
    demo_token = "synthetic-secret-demo-token-987"
    db_path = tmp_path / "guarded.db"
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True, exist_ok=True)
    run_workspaces_root = tmp_path / "runs"
    run_workspaces_root.mkdir(parents=True, exist_ok=True)

    app = create_app(
        db_path=db_path,
        workspace_root=workspace_root,
        run_workspaces_root=run_workspaces_root,
        demo_config=DemoGuardConfig(enabled=True, demo_token=demo_token),
    )
    test_client = TestClient(app, base_url="http://gateway.local")

    # 1. Assert without demo-token, mutating call to gateway is rejected
    unauthorized_resp = test_client.post("/api/v1/runs", json={
        "name": "Unauthorized Run",
        "task_scope": {"allowed_tools": ["workspace"], "task_description": "Test task",  "schema_version": "1", "created_at": "2026-10-15T00:00:00Z"},
    })
    assert unauthorized_resp.status_code == 401
    assert unauthorized_resp.json().get("error") == "demo_token_required"

    # 2. Run CLI with --demo-token and --in-process against the token-guarded gateway via monkeypatching create_app
    import scopewatch.app as app_module
    monkeypatch.setattr(
        app_module,
        "create_app",
        lambda **kwargs: create_app(
            db_path=db_path,
            workspace_root=workspace_root,
            run_workspaces_root=run_workspaces_root,
            demo_config=DemoGuardConfig(enabled=True, demo_token=demo_token),
        ),
    )

    exit_code = main(
        [
            "--scenario",
            "demo/scenarios/01_safe_audit.json",
            "--in-process",
            "--demo-token",
            demo_token,
            "--max-turns",
            "10",
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Final Status:      COMPLETED" in captured.out
    # Acceptance criterion: the token must NEVER appear in logs or output!
    assert demo_token not in captured.out
    assert demo_token not in captured.err

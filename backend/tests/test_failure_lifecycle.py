"""Tests for Issue #124: Mark agent runs failed when provider or gateway calls abort the loop."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
import httpx
import pytest

from scopewatch.agent.loop import AgentLoop, AgentRunResult, sanitize_failure_reason
from scopewatch.agent.tools import GatewayDispatcher
from scopewatch.app import create_app
from scopewatch.models import EventType, PolicyOutcome, ReasoningProvenance, RunStatus
from scopewatch.providers.client import ChatResult, MockProviderClient
from scopewatch.providers.errors import ProviderError, ProviderErrorCode


def _setup_service_and_client(tmp_path: Path) -> tuple[TestClient, Path]:
    db_path = tmp_path / "test_scopewatch.db"
    app = create_app(db_path=db_path)
    client = TestClient(app)
    return client, db_path


def _sample_task_scope() -> dict:
    return {
        "schema_version": "1",
        "task_description": "Audit safe invoices",
        "allowed_paths": ["invoices/approved", "outputs"],
        "blocked_paths": ["invoices/private"],
        "allowed_tools": ["workspace"],
        "allowed_operations": ["read_text", "write_text", "list_directory"],
        "allowed_network_destinations": [],
        "requires_approval": [],
        "created_at": "2026-10-03T00:00:00Z",
    }


def _create_run(client: TestClient) -> str:
    resp = client.post("/api/v1/runs", json={
        "name": "Failure Test Run",
        "task_scope": _sample_task_scope(),
    })
    assert resp.status_code == 201
    return resp.json()["id"]


def test_provider_error_aborts_loop_and_marks_run_failed(tmp_path: Path) -> None:
    client, _ = _setup_service_and_client(tmp_path)
    run_id = _create_run(client)

    class FailingProvider:
        def complete(self, messages, tools):
            raise ProviderError(ProviderErrorCode.PROVIDER_TIMEOUT, "Connection timed out to provider upstream")

    dispatcher = GatewayDispatcher(base_url="http://testserver", http_client=client)
    loop = AgentLoop(
        run_id=run_id,
        provider_client=FailingProvider(),
        dispatcher=dispatcher,
    )

    result = loop.run()
    assert result.status == "FAILED"
    assert "ProviderError" in (result.error or "")
    assert "Connection timed out" in (result.error or "")

    # Run in gateway database must be marked FAILED
    run_resp = client.get(f"/api/v1/runs/{run_id}")
    assert run_resp.status_code == 200
    assert run_resp.json()["status"] == "FAILED"

    # Gateway evidence events must contain SYSTEM_ERROR
    events = client.get(f"/api/v1/runs/{run_id}/events").json()
    system_errors = [e for e in events if e["event_type"] == EventType.SYSTEM_ERROR.value]
    assert len(system_errors) == 1
    assert "ProviderError" in system_errors[0]["details"]["reason"]


def test_action_dispatch_rejection_marks_run_failed_preserving_prior_actions(tmp_path: Path) -> None:
    client, _ = _setup_service_and_client(tmp_path)
    run_id = _create_run(client)

    # Turn 1: Valid tool call
    # Turn 2: Fails unexpectedly during submit_action
    mock = MockProviderClient()
    mock.enqueue(ChatResult(
        content="Inspecting workspace",
        tool_calls=[{
            "id": "c1",
            "type": "function",
            "function": {
                "name": "list_directory",
                "arguments": json.dumps({"path": "invoices/approved"}),
            },
        }],
        reasoning_text="Step 1 listing",
        reasoning_provenance=ReasoningProvenance.UNAVAILABLE.value,
        model="mock",
        profile="mock",
    ))
    mock.enqueue(ChatResult(
        content="Second step",
        tool_calls=[{
            "id": "c2",
            "type": "function",
            "function": {
                "name": "read_text",
                "arguments": json.dumps({"path": "invoices/approved/vendor-a.txt"}),
            },
        }],
        reasoning_text="Step 2 read",
        reasoning_provenance=ReasoningProvenance.UNAVAILABLE.value,
        model="mock",
        profile="mock",
    ))

    dispatcher = GatewayDispatcher(base_url="http://testserver", http_client=client)
    orig_submit = dispatcher.submit_action
    call_count = 0

    def faulty_submit(rid, req):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("Gateway database connection dropped unexpectedly")
        return orig_submit(rid, req)

    dispatcher.submit_action = faulty_submit  # type: ignore[assignment]

    loop = AgentLoop(
        run_id=run_id,
        provider_client=mock,
        dispatcher=dispatcher,
    )
    result = loop.run()
    assert result.status == "FAILED"
    assert "Gateway database connection dropped" in (result.error or "")

    # Prior action and decision preserved in result summary
    assert len(result.actions) == 1
    assert len(result.decisions) == 1
    assert result.decisions[0] == PolicyOutcome.ALLOW.value

    # Run in gateway marked FAILED
    run_resp = client.get(f"/api/v1/runs/{run_id}")
    assert run_resp.json()["status"] == "FAILED"


def test_gateway_unavailable_reports_failed_honestly_without_crash(tmp_path: Path) -> None:
    client, _ = _setup_service_and_client(tmp_path)
    run_id = _create_run(client)

    class FailingProvider:
        def complete(self, messages, tools):
            raise RuntimeError("Internal provider failure")

    dispatcher = GatewayDispatcher(base_url="http://testserver", http_client=client)

    # Simulate gateway becoming unreachable during fail_run call
    def faulty_fail_run(rid, reason=""):
        raise httpx.ConnectError("Connection refused by gateway")

    dispatcher.fail_run = faulty_fail_run  # type: ignore[assignment]

    loop = AgentLoop(
        run_id=run_id,
        provider_client=FailingProvider(),
        dispatcher=dispatcher,
    )
    # Must not raise an uncaught exception; reports status="FAILED" honestly
    result = loop.run()
    assert result.status == "FAILED"
    assert "Internal provider failure" in (result.error or "")


def test_credential_sanitization_in_failure_reason() -> None:
    sensitive_msg = (
        "HTTP 401 Unauthorized with header Bearer sk-ant-api03-abcdef1234567890 "
        "and api_key=sk-proj-998877665544332211"
    )
    sanitized = sanitize_failure_reason(sensitive_msg)
    assert "Bearer [REDACTED]" in sanitized
    assert "api_key=[REDACTED]" in sanitized
    assert "sk-ant-api03" not in sanitized
    assert "sk-proj" not in sanitized


def test_cli_setup_failure_marks_run_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, db_path = _setup_service_and_client(tmp_path)

    # In CLI __main__.py, if get_profile raises or api key is missing after run creation,
    # the run on the gateway must be marked FAILED
    from scopewatch.agent.__main__ import main

    scenario_file = tmp_path / "scenario.json"
    scenario_file.write_text(json.dumps({
        "name": "CLI Setup Failure Scenario",
        "task_scope": _sample_task_scope(),
    }))

    # Point CLI to test gateway and trigger profile resolution error
    monkeypatch.setattr(
        "sys.argv",
        [
            "scopewatch.agent",
            "--scenario", str(scenario_file),
            "--base-url", "http://testserver",
            "--db-path", str(db_path),
            "--profile", "non-existent-profile-xyz",
        ],
    )

    with patch("httpx.Client", return_value=client):
        exit_code = main()

    assert exit_code == 1

    # Check that the run was created and then marked FAILED on the gateway
    runs_resp = client.get("/api/v1/runs")
    assert runs_resp.status_code == 200
    matching_runs = [r for r in runs_resp.json() if r["name"] == "CLI Setup Failure Scenario"]
    assert len(matching_runs) == 1
    assert matching_runs[0]["status"] == "FAILED"

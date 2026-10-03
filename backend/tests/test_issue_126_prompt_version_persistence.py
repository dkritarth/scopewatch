"""Tests for Issue #126: Persist the agent prompt version with run evidence."""

import json
from pathlib import Path
from fastapi.testclient import TestClient
import pytest

from scopewatch.agent.loop import AgentLoop
from scopewatch.agent.prompt import PROMPT_VERSION
from scopewatch.agent.tools import GatewayDispatcher
from scopewatch.app import create_app
from scopewatch.evidence_chain import build_chain_from_events
from scopewatch.models import ReasoningProvenance
from scopewatch.providers.client import ChatResult, MockProviderClient
from scopewatch.schemas import CreateRunRequest, TaskScope
from scopewatch.service import ScopewatchService


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
        "allowed_operations": ["read_text", "write_text"],
        "allowed_network_destinations": [],
        "requires_approval": [],
        "created_at": "2026-10-03T00:00:00Z",
    }


def test_create_run_persists_prompt_version(tmp_path: Path) -> None:
    client, db_path = _setup_service_and_client(tmp_path)

    payload = {
        "name": "Audit Run with Prompt Version",
        "task_scope": _sample_task_scope(),
        "prompt_version": PROMPT_VERSION,
    }
    resp = client.post("/api/v1/runs", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    run_id = data["id"]
    assert data["prompt_version"] == PROMPT_VERSION

    # Fetch run via API
    get_resp = client.get(f"/api/v1/runs/{run_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["prompt_version"] == PROMPT_VERSION

    # Verify event RUN_CREATED contains prompt_version
    events_resp = client.get(f"/api/v1/runs/{run_id}/events")
    assert events_resp.status_code == 200
    events = events_resp.json()
    assert len(events) >= 1
    run_created = [e for e in events if e["event_type"] == "RUN_CREATED"][0]
    assert run_created["details"]["prompt_version"] == PROMPT_VERSION


def test_patch_run_updates_prompt_version(tmp_path: Path) -> None:
    client, db_path = _setup_service_and_client(tmp_path)

    # Create run without prompt_version
    payload = {
        "name": "Audit Run without Prompt Version",
        "task_scope": _sample_task_scope(),
    }
    resp = client.post("/api/v1/runs", json=payload)
    assert resp.status_code == 201
    run_id = resp.json()["id"]
    assert resp.json()["prompt_version"] is None

    # Patch with prompt_version
    patch_resp = client.patch(f"/api/v1/runs/{run_id}", json={"prompt_version": "2026-10-03-custom"})
    assert patch_resp.status_code == 200
    assert patch_resp.json()["prompt_version"] == "2026-10-03-custom"

    # Verify persistence
    get_resp = client.get(f"/api/v1/runs/{run_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["prompt_version"] == "2026-10-03-custom"


def test_db_restart_retains_prompt_version(tmp_path: Path) -> None:
    client, db_path = _setup_service_and_client(tmp_path)

    payload = {
        "name": "Persistence Run",
        "task_scope": _sample_task_scope(),
        "prompt_version": PROMPT_VERSION,
    }
    resp = client.post("/api/v1/runs", json=payload)
    run_id = resp.json()["id"]

    # Reconnect fresh service directly to the same database file (simulating server restart)
    reloaded_service = ScopewatchService(db_path=db_path, workspace_root=tmp_path / "workspace")
    run = reloaded_service.get_run(run_id)
    assert run.prompt_version == PROMPT_VERSION

    # List runs also preserves prompt_version
    runs = reloaded_service.list_runs()
    matching = [r for r in runs if r.id == run_id]
    assert len(matching) == 1
    assert matching[0].prompt_version == PROMPT_VERSION


def test_agent_loop_persists_prompt_version_before_execution(tmp_path: Path) -> None:
    client, db_path = _setup_service_and_client(tmp_path)

    # Create run without prompt_version
    payload = {
        "name": "Agent Run",
        "task_scope": _sample_task_scope(),
    }
    resp = client.post("/api/v1/runs", json=payload)
    run_id = resp.json()["id"]
    assert resp.json()["prompt_version"] is None

    # Agent runs with custom prompt_version
    mock = MockProviderClient()
    mock.enqueue(ChatResult(
        content="I have audited the invoices.",
        tool_calls=[],
        reasoning_text="Audit finished without actions.",
        reasoning_provenance=ReasoningProvenance.UNAVAILABLE.value,
        model="mock-v1",
        profile="mock",
    ))
    dispatcher = GatewayDispatcher(base_url="http://testserver", http_client=client)
    custom_version = "2026-10-03-agent-experiment"
    loop = AgentLoop(
        run_id=run_id,
        provider_client=mock,
        dispatcher=dispatcher,
        prompt_version=custom_version,
    )
    result = loop.run()
    assert result.status == "COMPLETED"
    assert result.prompt_version == custom_version

    # Verified stored run in database
    get_resp = client.get(f"/api/v1/runs/{run_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["prompt_version"] == custom_version


def test_evidence_chain_includes_prompt_version(tmp_path: Path) -> None:
    client, db_path = _setup_service_and_client(tmp_path)

    payload = {
        "name": "Chained Run",
        "task_scope": _sample_task_scope(),
        "prompt_version": PROMPT_VERSION,
    }
    resp = client.post("/api/v1/runs", json=payload)
    run_id = resp.json()["id"]

    events = client.get(f"/api/v1/runs/{run_id}/events").json()
    chain = build_chain_from_events(run_id, events)
    assert chain.verify()
    assert len(chain) == 1
    records = chain.to_records()
    assert len(records) == 1
    # Verify the event from API details retains prompt_version
    assert events[0]["details"]["prompt_version"] == PROMPT_VERSION

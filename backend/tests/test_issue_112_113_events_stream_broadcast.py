"""Tests proving terminal event broadcast (#113) and gap-free SSE stream replay (#112)."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import pytest
from httpx import ASGITransport, AsyncClient

from scopewatch.app import create_app
from scopewatch.db import init_db
from scopewatch.events import broadcaster
from scopewatch.models import EventType, RunStatus
from scopewatch.schemas import TaskScope
from scopewatch.service import ScopewatchService

NOW = datetime.now(timezone.utc).isoformat()


@pytest.fixture
def test_setup(tmp_path: Path):
    db_file = tmp_path / "sync_test.db"
    ws = tmp_path / "workspace"
    ws.mkdir()
    init_db(db_file)
    app = create_app(db_path=db_file, workspace_root=ws)
    service = app.state.service
    scope = TaskScope(
        schema_version="1",
        task_description="Test broadcast sync",
        allowed_paths=["outputs"],
        blocked_paths=[],
        allowed_tools=["workspace"],
        allowed_operations=["read_text"],
        allowed_network_destinations=[],
        requires_approval=[],
        created_at=NOW,
    )
    run, _ = service.create_run(name="Sync Run", task_scope=scope)
    return {"service": service, "run": run, "app": app}


@pytest.mark.anyio
async def test_complete_run_broadcasts_to_subscribers(test_setup):
    """Issue #113: complete_run publishes a RUN_COMPLETED event to active subscribers."""
    service: ScopewatchService = test_setup["service"]
    run = test_setup["run"]

    queue = await broadcaster.subscribe(run.id)
    try:
        # Complete the run
        updated_run, event = service.complete_run(run.id)
        assert updated_run.status == RunStatus.COMPLETED
        assert event.event_type == EventType.RUN_COMPLETED

        # Verify event was received by subscriber queue
        received_event = await asyncio.wait_for(queue.get(), timeout=2.0)
        assert received_event.event_type == EventType.RUN_COMPLETED
        assert received_event.sequence == event.sequence
        assert received_event.run_id == run.id
    finally:
        await broadcaster.unsubscribe(run.id, queue)


@pytest.mark.anyio
async def test_fail_run_broadcasts_to_subscribers(test_setup):
    """Issue #113: fail_run publishes a SYSTEM_ERROR event to active subscribers."""
    service: ScopewatchService = test_setup["service"]
    run = test_setup["run"]

    queue = await broadcaster.subscribe(run.id)
    try:
        # Fail the run
        updated_run, event = service.fail_run(run.id, reason="Simulated crash")
        assert updated_run.status == RunStatus.FAILED
        assert event.event_type == EventType.SYSTEM_ERROR

        # Verify event was received by subscriber queue
        received_event = await asyncio.wait_for(queue.get(), timeout=2.0)
        assert received_event.event_type == EventType.SYSTEM_ERROR
        assert received_event.sequence == event.sequence
        assert received_event.details["reason"] == "Simulated crash"
    finally:
        await broadcaster.unsubscribe(run.id, queue)


@pytest.mark.anyio
async def test_stream_events_replays_after_sequence_query_param(test_setup):
    """Issue #112: /events/stream with ?after_sequence=X delivers replay events without gaps."""
    service: ScopewatchService = test_setup["service"]
    run = test_setup["run"]
    app = test_setup["app"]

    # Seed events
    service.complete_run(run.id)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # Connect with after_sequence=0
        resp = await client.get(f"/api/v1/runs/{run.id}/events/stream?after_sequence=0&limit=3")
        assert resp.status_code == 200
        text = resp.text
        assert "event: connected" in text
        assert "event: RUN_COMPLETED" in text

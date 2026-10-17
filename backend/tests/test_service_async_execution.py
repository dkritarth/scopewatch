"""Tests for asynchronous non-blocking executor execution (#189)."""

import asyncio
import time
from pathlib import Path

import pytest

from scopewatch.db import init_db
from scopewatch.models import PolicyOutcome, ExecutionStatus, ApprovalStatus
from scopewatch.schemas import TaskScope, SubmitActionRequest
from scopewatch.service import ScopewatchService
import scopewatch.service as service_mod


@pytest.fixture
def service_env(tmp_path: Path):
    db_file = tmp_path / "async_exec.db"
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "file.txt").write_text("hello async")
    init_db(db_file)
    service = ScopewatchService(db_path=db_file, workspace_root=ws)
    run, _ = service.create_run(
        name="Async Exec Run",
        task_scope=TaskScope(
            schema_version="1",
            task_description="test async execution",
            allowed_paths=["file.txt"],
            blocked_paths=[],
            allowed_tools=["workspace"],
            allowed_operations=["read_text", "delete_path"],
            allowed_network_destinations=[],
            requires_approval=["delete_path"],
            created_at="2026-10-15T00:00:00Z",
        ),
    )
    return {"service": service, "run": run, "workspace": ws}


def test_concurrent_read_while_execution_in_flight(service_env, monkeypatch: pytest.MonkeyPatch):
    """A concurrent read returns in under 0.5s while an execution is in flight (#189)."""
    service: ScopewatchService = service_env["service"]
    run = service_env["run"]

    orig_execute = service_mod.execute_action

    def slow_execute(*args, **kwargs):
        time.sleep(0.5)
        return orig_execute(*args, **kwargs)

    monkeypatch.setattr(service_mod, "execute_action", slow_execute)

    async def run_scenario():
        task = asyncio.create_task(
            service.submit_action(
                run.id,
                SubmitActionRequest(tool="workspace", operation="read_text", resource="file.txt"),
            )
        )
        await asyncio.sleep(0.05)
        t_start = time.perf_counter()
        events = service.get_events(run.id)
        duration = time.perf_counter() - t_start
        assert duration < 0.2, f"Concurrent read took {duration}s, expected < 0.2s"
        assert len(events) >= 1
        res = await task
        assert res.policy_decision.outcome == PolicyOutcome.ALLOW
        assert res.execution_receipt is not None
        assert res.execution_receipt.status == ExecutionStatus.EXECUTED

    asyncio.run(run_scenario())

"""Caller cancellation must wait for durable settlement of dispatched effects."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import threading

import pytest

from scopewatch.db import get_connection, init_db
from scopewatch.errors import ScopewatchAPIError
from scopewatch.executor import ExecutionSecurityError
from scopewatch.models import ApprovalStatus, EventType, ExecutionStatus, RunStatus
from scopewatch.schemas import SubmitActionRequest, TaskScope
from scopewatch.service import ScopewatchService


@pytest.mark.parametrize("held", [False, True], ids=["allow", "approved-hold"])
@pytest.mark.parametrize("outcome", ["success", "failed-receipt", "security-error", "internal-error"])
def test_cancelled_dispatch_settles_before_propagating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held: bool, outcome: str
) -> None:
    import scopewatch.service as module

    database = tmp_path / "gateway.db"
    init_db(database)
    workspace = tmp_path / "workspace"
    (workspace / "outputs").mkdir(parents=True)
    service = ScopewatchService(db_path=database, workspace_root=workspace)
    run, _ = service.create_run(name="Synthetic cancellation", task_scope=TaskScope(
        schema_version="1", task_description="Write a synthetic output",
        allowed_paths=["outputs"], allowed_tools=["workspace"],
        allowed_operations=["write_text"], requires_approval=["write_text"] if held else [],
        created_at=datetime.now(timezone.utc).isoformat(),
    ))
    request = SubmitActionRequest(tool="workspace", operation="write_text",
                                  resource="outputs/result.txt", arguments={"content": "synthetic effect"})
    pending = asyncio.run(service.submit_action(run.id, request)) if held else None
    original_execute = module.execute_action
    release = threading.Event()
    invocations = []

    async def scenario():
        loop = asyncio.get_running_loop()
        effect_done = loop.create_future()

        def gated_execute(*args, **kwargs):
            receipt = original_execute(*args, **kwargs)
            invocations.append(receipt)
            loop.call_soon_threadsafe(effect_done.set_result, None)
            assert release.wait(10), "test did not release completed execution"
            # The worker still borrows this connection until it returns.
            assert kwargs["db_path"].execute("SELECT 1").fetchone()[0] == 1
            if outcome == "security-error":
                raise ExecutionSecurityError("Synthetic security failure")
            if outcome == "internal-error":
                raise RuntimeError("Synthetic private executor detail")
            if outcome == "failed-receipt":
                return receipt.model_copy(update={"status": ExecutionStatus.FAILED, "error_code": "SYNTHETIC_FAILURE"})
            return receipt

        monkeypatch.setattr(module, "execute_action", gated_execute)
        operation = (service.resolve_approval(pending.approval_request.id, True) if held
                     else service.submit_action(run.id, request))
        caller = asyncio.create_task(operation)

        async def checkpoint():
            processed = loop.create_future()
            loop.call_soon(processed.set_result, None)
            await processed

        try:
            await asyncio.wait_for(effect_done, 5)
            assert (Path(run.workspace_path) / "outputs/result.txt").read_text() == "synthetic effect"
            for _ in range(2):
                caller.cancel()
                await checkpoint()
                assert not caller.done(), "cancellation escaped before execution settled"
            with pytest.raises(ScopewatchAPIError) as closing:
                service.complete_run(run.id)
            assert closing.value.code == "APPROVED_ACTION_IN_FLIGHT"
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(caller, 5)
        finally:
            release.set()
            try:
                await caller
            except (asyncio.CancelledError, Exception):
                pass

    asyncio.run(scenario())
    assert len(invocations) == 1
    action_id = invocations[0].action_request_id
    stored = service.get_action(run.id, action_id)
    expected_status = ExecutionStatus.EXECUTED if outcome == "success" else ExecutionStatus.FAILED
    assert stored.execution_receipt.status == expected_status
    if outcome == "internal-error":
        assert stored.execution_receipt.sanitized_result == {"error": "RuntimeError"}
    if held:
        assert stored.approval_request.status == ApprovalStatus.CONSUMED
        with pytest.raises(ScopewatchAPIError):
            asyncio.run(service.resolve_approval(pending.approval_request.id, True))
    connection = get_connection(database)
    try:
        assert connection.execute("SELECT count(*) FROM execution_receipts WHERE action_request_id = ?",
                                  (action_id,)).fetchone()[0] == 1
    finally:
        connection.close()
    events = service.get_events(run.id)
    terminal = [event for event in events if event.event_type in
                (EventType.EXECUTION_SUCCEEDED, EventType.EXECUTION_FAILED)]
    assert len(terminal) == 1
    assert terminal[0].execution_receipt_id == stored.execution_receipt.id
    service.complete_run(run.id)
    assert service.get_run(run.id).status == RunStatus.COMPLETED
    assert service.get_events(run.id)[-1].event_type == EventType.RUN_COMPLETED

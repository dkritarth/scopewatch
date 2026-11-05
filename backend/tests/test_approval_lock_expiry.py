"""Approval expiry is checked after acquiring the SQLite writer lock."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading

import pytest

from scopewatch.db import get_connection, init_db
from scopewatch.errors import ScopewatchAPIError
from scopewatch.models import ApprovalStatus, EventType, RunStatus
from scopewatch.schemas import SubmitActionRequest, TaskScope
from scopewatch.service import ScopewatchService


@pytest.mark.parametrize("approve", [True, False])
def test_approval_expiring_behind_writer_lock_never_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, approve: bool
) -> None:
    database = tmp_path / "gateway.db"
    init_db(database)
    workspace = tmp_path / "workspace"
    (workspace / "outputs").mkdir(parents=True)
    (workspace / "outputs/file.txt").write_text("before", encoding="utf-8")
    service = ScopewatchService(db_path=database, workspace_root=workspace)
    before = datetime.now(timezone.utc)
    run, _ = service.create_run(name="Synthetic lock expiry", task_scope=TaskScope(
        schema_version="1", task_description="Write a synthetic output",
        allowed_paths=["outputs"], allowed_tools=["workspace"],
        allowed_operations=["write_text"], requires_approval=["write_text"],
        created_at=before.isoformat(),
    ))
    response = asyncio.run(service.submit_action(run.id, SubmitActionRequest(
        tool="workspace", operation="write_text", resource="outputs/file.txt",
        arguments={"content": "expired effect"},
    )))
    approval = response.approval_request
    assert approval is not None
    lock = get_connection(database)
    lock.execute("UPDATE approval_requests SET expires_at = ? WHERE id = ?",
                 ((before + timedelta(seconds=1)).isoformat(), approval.id))
    expired = threading.Event()
    waiting = threading.Event()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return before + timedelta(seconds=2) if expired.is_set() else before

    monkeypatch.setattr("scopewatch.service.datetime", Clock)
    original_connection = service._get_conn

    def observed_connection():
        connection = original_connection()
        connection.set_trace_callback(
            lambda sql: waiting.set() if sql.upper() == "BEGIN IMMEDIATE" else None
        )
        return connection

    monkeypatch.setattr(service, "_get_conn", observed_connection)
    published = []

    async def observe_publish(run_id, event):
        connection = get_connection(database)
        try:
            row = connection.execute("SELECT status FROM approval_requests WHERE id = ?",
                                     (approval.id,)).fetchone()
            published.append((run_id, event.event_type, row["status"]))
        finally:
            connection.close()

    monkeypatch.setattr("scopewatch.service.broadcaster.publish", observe_publish)
    outcome = {}

    def resolve():
        try:
            outcome["response"] = asyncio.run(service.resolve_approval(approval.id, approve))
        except BaseException as error:
            outcome["error"] = error

    lock.execute("BEGIN IMMEDIATE")
    worker = threading.Thread(target=resolve)
    worker.start()
    try:
        assert waiting.wait(10), "resolver did not reach the blocked writer transaction"
        # Advance a controlled clock while another connection owns the lock.
        # No sleep or deadline race is needed to cross the expiry boundary.
        expired.set()
    finally:
        lock.execute("COMMIT")
        lock.close()
        worker.join(10)
    assert not worker.is_alive()
    error = outcome.get("error")
    assert isinstance(error, ScopewatchAPIError), outcome
    assert (error.code, error.status_code) == ("APPROVAL_EXPIRED", 409)
    stored = service.get_action(run.id, response.action_request.id)
    assert stored.approval_request.status == ApprovalStatus.EXPIRED
    assert stored.execution_receipt is None
    assert service.get_run(run.id).status == RunStatus.ACTIVE
    assert (Path(run.workspace_path) / "outputs/file.txt").read_text() == "before"
    events = service.get_events(run.id)
    assert sum(e.event_type == EventType.APPROVAL_EXPIRED for e in events) == 1
    assert published == [(run.id, EventType.APPROVAL_EXPIRED, ApprovalStatus.EXPIRED.value)]
    assert not any(e.event_type in (EventType.APPROVAL_GRANTED, EventType.APPROVAL_DENIED,
                                   EventType.EXECUTION_STARTED) for e in events)

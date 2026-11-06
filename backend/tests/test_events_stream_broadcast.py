"""Tests proving terminal event broadcast (#113) and gap-free SSE stream replay (#112)."""

import asyncio
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
import threading
import pytest
from httpx import ASGITransport, AsyncClient

from scopewatch.app import create_app
from scopewatch.db import init_db
from scopewatch.events import broadcaster
from scopewatch.models import EventType, RunStatus
from scopewatch.schemas import TaskScope
from scopewatch.service import ScopewatchService

NOW = datetime.now(timezone.utc).isoformat()


@pytest.mark.parametrize("bind_during_publish", [True, False], ids=["bind-during-publish", "already-waiting"])
def test_worker_publish_wakes_first_queue_reader(test_setup, monkeypatch, bind_during_publish):
    """A queue that binds its loop during publication must still wake it."""
    import scopewatch.events as events

    bus = events.EventBroadcaster()
    run = test_setup["run"]
    event = test_setup["service"].get_events(run.id)[0]
    subscribed = threading.Event()
    start_reader = threading.Event()
    reader_ready = threading.Event()
    safe_to_publish = threading.Event()
    release_publisher = threading.Event()
    published = threading.Event()
    delivered = threading.Event()
    shared = {}
    errors = []
    loop = asyncio.SelectorEventLoop()
    loop.set_debug(False)
    original_select = loop._selector.select

    def observed_select(timeout=None):
        if reader_ready.is_set() and timeout is not None and timeout > 0:
            # The loop has computed its blocking timeout. A plain call_soon
            # from another thread can no longer avoid sleeping by chance.
            safe_to_publish.set()
        return original_select(timeout)

    monkeypatch.setattr(loop._selector, "select", observed_select)

    class ReaderWaiters(deque):
        def append(self, waiter):
            super().append(waiter)
            reader_ready.set()

    async def consume():
        queue = await bus.subscribe(run.id)
        queue._getters = ReaderWaiters(queue._getters)
        gate = asyncio.Event()
        shared.update(queue=queue, gate=gate)
        subscribed.set()
        try:
            await gate.wait()
            received = await asyncio.wait_for(queue.get(), timeout=5)
            assert received == event
            delivered.set()
            reader_ready.set()
            safe_to_publish.set()
        finally:
            await bus.unsubscribe(run.id, queue)

    def run_loop():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(consume())
        except BaseException as error:
            errors.append(error)
            reader_ready.set()
            safe_to_publish.set()
        finally:
            loop.close()

    original_getattr = getattr

    def gated_getattr(obj, name, default=None):
        value = original_getattr(obj, name, default)
        if bind_during_publish and obj is shared.get("queue") and name == "_loop" and value is None:
            # Freeze the old lazy-loop snapshot, then let get() bind the
            # queue before publish resumes. New code never needs this read.
            start_reader.set()
            assert release_publisher.wait(5)
        return value

    monkeypatch.setattr(events, "getattr", gated_getattr, raising=False)

    def publish():
        try:
            bus.publish_sync(run.id, event)
        except BaseException as error:
            errors.append(error)
        finally:
            published.set()
            start_reader.set()

    subscriber = threading.Thread(target=run_loop)
    publisher = threading.Thread(target=publish)
    subscriber.start()
    try:
        assert subscribed.wait(5)
        if bind_during_publish:
            publisher.start()
            assert start_reader.wait(5)
        loop.call_soon_threadsafe(shared["gate"].set)
        assert reader_ready.wait(5)
        assert safe_to_publish.wait(5)
        if not bind_during_publish:
            publisher.start()
        release_publisher.set()
        assert published.wait(5)
        # The stream's own five-second timeout must not be its wakeup.
        promptly_delivered = delivered.wait(1)
    finally:
        release_publisher.set()
        if not loop.is_closed():
            try:
                loop.call_soon_threadsafe(lambda: None)
            except RuntimeError:
                assert loop.is_closed()
        if publisher.ident is not None:
            publisher.join(5)
        subscriber.join(6)
    assert not subscriber.is_alive() and not publisher.is_alive()
    assert errors == []
    assert promptly_delivered, "publication waited for a timer to wake the loop"
    assert not bus._subscribers


@pytest.mark.anyio
async def test_full_subscriber_queue_drops_inside_scheduled_callback(test_setup, caplog):
    from scopewatch.events import EventBroadcaster

    bus = EventBroadcaster()
    run = test_setup["run"]
    event = test_setup["service"].get_events(run.id)[0]
    queue = await bus.subscribe(run.id)
    loop = asyncio.get_running_loop()
    errors = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: errors.append(context))
    try:
        for _ in range(queue.maxsize):
            queue.put_nowait(event)
        await asyncio.to_thread(bus.publish_sync, run.id, event)
        drained = loop.create_future()
        loop.call_soon(drained.set_result, None)
        await drained
        assert queue.qsize() == queue.maxsize
        assert errors == []
        assert "Subscriber queue full" in caplog.text
    finally:
        loop.set_exception_handler(previous_handler)
        await bus.unsubscribe(run.id, queue)
    assert not bus._subscribers


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

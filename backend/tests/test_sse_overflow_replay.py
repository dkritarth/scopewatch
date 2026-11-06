"""A bounded subscriber queue must not advance past missing durable evidence."""

import asyncio

import pytest

from scopewatch.app import create_app
from scopewatch.db import init_db
from scopewatch.events import broadcaster
from scopewatch.schemas import SubmitActionRequest, TaskScope


@pytest.mark.anyio
@pytest.mark.parametrize("actions,cursor,tail_only", [
    (60, 0, False), (160, 0, False), (60, None, False), (60, 0, True),
], ids=["live-gap", "paged-gap", "live-only", "idle-tail"])
async def test_overflow_replays_durable_gaps_in_bounded_pages(tmp_path, monkeypatch, caplog, actions, cursor, tail_only):
    monkeypatch.setenv("SCOPEWATCH_AGENT_PROFILE", "mock")
    monkeypatch.setenv("SCOPEWATCH_REASONING_AUDIT", "false")
    database = tmp_path / "gateway.db"
    init_db(database)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = create_app(db_path=database, workspace_root=workspace)
    service = app.state.service
    run, _ = service.create_run(name="Synthetic subscriber overflow", task_scope=TaskScope(
        task_description="Read outputs", allowed_paths=["outputs"], allowed_tools=["workspace"],
        allowed_operations=["read_text"], created_at="2026-11-06T00:00:00Z"))
    started_read = asyncio.Event()
    release_read = asyncio.Event()
    prefix_sent = asyncio.Event()
    prefix_drained = asyncio.Event()
    disconnected = asyncio.Event()
    queues = []
    sequences = []
    reads = []
    real_subscribe = broadcaster.subscribe
    real_get_events = service.get_events
    real_wait_for = asyncio.wait_for
    final_sequence = 1 + 2 * actions + (0 if tail_only else 1)

    async def subscribe(run_id):
        queue = await real_subscribe(run_id)
        real_get = queue.get
        async def gated_get():
            started_read.set()
            await release_read.wait()
            event = await real_get()
            if event.sequence == 101: prefix_drained.set()
            return event
        queue.get = gated_get
        queues.append(queue)
        return queue

    def history(run_id, after_sequence=None, limit=None):
        page = real_get_events(run_id, after_sequence=after_sequence, limit=limit)
        reads.append((after_sequence, limit, len(page)))
        return page

    async def bounded_idle_wait(awaitable, timeout):
        # Shorten only the idle read after the retained prefix was delivered.
        # The burst gate itself retains the normal timeout and cannot mask loss.
        effective = 0.01 if tail_only and timeout == 15.0 and prefix_drained.is_set() else timeout
        return await real_wait_for(awaitable, effective)

    monkeypatch.setattr(broadcaster, "subscribe", subscribe)
    monkeypatch.setattr(service, "get_events", history)
    monkeypatch.setattr(asyncio, "wait_for", bounded_idle_wait)

    async def produce():
        await real_wait_for(started_read.wait(), 5)
        for index in range(actions):
            response = await service.submit_action(run.id, SubmitActionRequest(
                tool="workspace", operation="read_text", resource=f"forbidden/{index}.txt"))
            assert response.policy_decision.outcome.value == "DENY"
        # Flush owner-loop callbacks when combined with the thread-wakeup fix.
        drained = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(drained.set_result, None)
        await drained
        assert queues[0].maxsize == 100
        assert queues[0].qsize() == 100
        assert len(real_get_events(run.id)) == 1 + 2 * actions
        assert "Subscriber queue full" in caplog.text
        release_read.set()
        await real_wait_for(prefix_sent.wait(), 5)
        if not tail_only:
            service.complete_run(run.id)

    async def receive():
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        for line in message.get("body", b"").splitlines():
            if line.startswith(b"id: "):
                sequence = int(line[4:])
                sequences.append(sequence)
                if sequence == 101: prefix_sent.set()
                if sequence == final_sequence: disconnected.set()
            elif tail_only and line.startswith(b": keepalive"):
                disconnected.set()  # Bounds the old implementation's idle path.

    path = f"/api/v1/runs/{run.id}/events/stream"
    expected = list(range(1 if cursor is not None else 2, final_sequence + 1))
    query = f"limit={len(expected) + 1}"
    if cursor is not None: query += f"&after_sequence={cursor}"
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
             "http_version": "1.1", "method": "GET", "scheme": "http", "root_path": "",
             "path": path, "raw_path": path.encode(), "query_string": query.encode(),
             "headers": [], "server": ("testserver", 80), "client": ("127.0.0.1", 12345)}
    tasks = [asyncio.create_task(app(scope, receive, send)), asyncio.create_task(produce())]
    try:
        await real_wait_for(asyncio.gather(*tasks), 15)
        assert sequences == expected
        assert reads and all(limit == 100 and size <= 100 for _, limit, size in reads)
        assert run.id not in broadcaster._subscribers
    finally:
        release_read.set()
        disconnected.set()
        for task in tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize("missing", [False, True], ids=["reordered-duplicates", "durable-hole"])
async def test_gap_recovery_orders_events_and_never_crosses_a_durable_hole(tmp_path, monkeypatch, missing):
    from scopewatch.db import get_connection
    from test_sse_connection_ready import stream_scope

    monkeypatch.setenv("SCOPEWATCH_AGENT_PROFILE", "mock")
    monkeypatch.setenv("SCOPEWATCH_REASONING_AUDIT", "false")
    database = tmp_path / "gateway.db"
    init_db(database)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = create_app(db_path=database, workspace_root=workspace)
    service = app.state.service
    run, _ = service.create_run(name="Synthetic reordered publication", task_scope=TaskScope(
        task_description="Read outputs", allowed_paths=["outputs"], allowed_tools=["workspace"],
        allowed_operations=["read_text"], created_at="2026-11-06T00:00:00Z"))
    started = asyncio.Event()
    release = asyncio.Event()
    disconnected = asyncio.Event()
    sequences = []
    real_subscribe = broadcaster.subscribe
    real_publish = broadcaster.publish

    async def subscribe(run_id):
        queue = await real_subscribe(run_id)
        real_get = queue.get
        async def gated_get():
            started.set()
            await release.wait()
            return await real_get()
        queue.get = gated_get
        return queue

    async def suppress_publish(*args, **kwargs):
        pass

    monkeypatch.setattr(broadcaster, "subscribe", subscribe)
    monkeypatch.setattr(broadcaster, "publish", suppress_publish)

    async def produce():
        await asyncio.wait_for(started.wait(), 2)
        await service.submit_action(run.id, SubmitActionRequest(
            tool="workspace", operation="read_text", resource="forbidden/synthetic.txt"))
        service.complete_run(run.id)
        events = service.get_events(run.id)
        assert [event.sequence for event in events] == [1, 2, 3, 4]
        if missing:
            conn = get_connection(database)
            try:
                conn.execute("DELETE FROM evidence_events WHERE run_id = ? AND sequence = 2", (run.id,))
                conn.commit()
            finally:
                conn.close()
        for index in [2, 1, 2, 3]:
            await real_publish(run.id, events[index])
        release.set()

    async def receive():
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        for line in message.get("body", b"").splitlines():
            if line.startswith(b"id: "):
                sequences.append(int(line[4:]))

    tasks = [asyncio.create_task(app(stream_scope(run.id, b"after_sequence=0&limit=5"), receive, send)),
             asyncio.create_task(produce())]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), 3)
        assert sequences == ([1] if missing else [1, 2, 3, 4])
        assert run.id not in broadcaster._subscribers
    finally:
        release.set()
        disconnected.set()
        for task in tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize("cursor", [-(2**63), 0, 100, 2**63 - 1])
async def test_cursor_boundaries_preserve_subsequent_live_events(tmp_path, monkeypatch, cursor):
    from test_sse_connection_ready import stream_scope

    monkeypatch.setenv("SCOPEWATCH_AGENT_PROFILE", "mock")
    database = tmp_path / "gateway.db"
    init_db(database)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = create_app(db_path=database, workspace_root=workspace)
    service = app.state.service
    run, _ = service.create_run(name="Synthetic cursor boundary", task_scope=TaskScope(
        task_description="Read outputs", allowed_paths=["outputs"], allowed_tools=["workspace"],
        allowed_operations=["read_text"], created_at="2026-11-06T00:00:00Z"))
    disconnected = asyncio.Event()
    sequences = []

    async def receive():
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        body = message.get("body", b"")
        if b"event: connected" in body:
            service.complete_run(run.id)
        for line in body.splitlines():
            if line.startswith(b"id: "):
                sequences.append(int(line[4:]))
                if sequences[-1] == 2:
                    disconnected.set()

    expected = [1, 2] if cursor <= 0 else [2]
    query = f"after_sequence={cursor}&limit={len(expected) + 1}".encode()
    await asyncio.wait_for(app(stream_scope(run.id, query), receive, send), 2)
    assert sequences == expected
    assert run.id not in broadcaster._subscribers

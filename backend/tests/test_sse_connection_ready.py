"""The ready frame must mean new publications can reach this subscriber."""

import asyncio

import httpx
import pytest

from scopewatch.app import create_app
from scopewatch.db import init_db
from scopewatch.events import broadcaster
from scopewatch.schemas import TaskScope
from sse_starlette.sse import EventSourceResponse


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOPEWATCH_AGENT_PROFILE", "mock")
    database = tmp_path / "gateway.db"
    init_db(database)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = create_app(db_path=database, workspace_root=workspace)
    run, _ = app.state.service.create_run(name="Synthetic SSE readiness", task_scope=TaskScope(
        task_description="Read outputs", allowed_paths=["outputs"], allowed_tools=["workspace"],
        allowed_operations=["read_text"], created_at="2026-11-06T00:00:00Z"))
    return app, run.id


def stream_scope(run_id, query):
    path = f"/api/v1/runs/{run_id}/events/stream"
    return {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "GET", "scheme": "http", "root_path": "",
            "path": path, "raw_path": path.encode(), "query_string": query,
            "headers": [(b"accept", b"text/event-stream")],
            "server": ("testserver", 80), "client": ("127.0.0.1", 12345)}


@pytest.mark.anyio
@pytest.mark.parametrize("cursor", [None, 0], ids=["live-only", "replay-zero"])
async def test_completion_after_ready_is_delivered(gateway, monkeypatch, cursor):
    app, run_id = gateway
    registered = asyncio.Event()
    cleaned = asyncio.Event()
    disconnected = asyncio.Event()
    messages = []
    control_task = None
    release_generator = asyncio.Event()
    original_send = EventSourceResponse._send_with_timeout
    original_subscribe = broadcaster.subscribe
    original_unsubscribe = broadcaster.unsubscribe
    initial = app.state.service.get_events(run_id)[0]

    async def subscribe(target):
        queue = await original_subscribe(target)
        if target == run_id: registered.set()
        return queue

    async def unsubscribe(target, queue):
        await original_unsubscribe(target, queue)
        if target == run_id: cleaned.set()

    async def gated_send(response, send, message):
        await original_send(response, send, message)
        if b"event: connected" in message.get("body", b""):
            await release_generator.wait()

    monkeypatch.setattr(EventSourceResponse, "_send_with_timeout", gated_send)
    monkeypatch.setattr(broadcaster, "subscribe", subscribe)
    monkeypatch.setattr(broadcaster, "unsubscribe", unsubscribe)

    async def control_after_subscription():
        await asyncio.wait_for(registered.wait(), 2)
        # The old no-cursor generator lost completion before subscribing.
        # A later control publication lets it terminate without timer sleeps.
        await broadcaster.publish(run_id, initial)

    async def receive():
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        async def send(message):
            nonlocal control_task
            messages.append(message)
            if b"event: connected" in message.get("body", b""):
                # Actual response bytes have reached the send boundary. Keep
                # its generator paused while a second HTTP request publishes.
                response = await client.post(f"/api/v1/runs/{run_id}/complete")
                assert response.status_code == 200
                release_generator.set()
                control_task = asyncio.create_task(control_after_subscription())

        query = b"limit=2" if cursor is None else b"after_sequence=0&limit=3"
        try:
            await asyncio.wait_for(app(stream_scope(run_id, query), receive, send), 3)
            if control_task is not None: await asyncio.wait_for(control_task, 2)
            body = b"".join(message.get("body", b"") for message in messages)
            assert b"event: RUN_COMPLETED" in body
            assert body.count(b"event: RUN_COMPLETED") == 1
            assert b"id: 2" in body
            if cursor == 0:
                assert body.index(b"id: 1") < body.index(b"id: 2")
            await asyncio.wait_for(cleaned.wait(), 2)
            assert run_id not in broadcaster._subscribers
        finally:
            release_generator.set()
            disconnected.set()
            if control_task is not None and not control_task.done():
                control_task.cancel()
                await asyncio.gather(control_task, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize("stop", ["limit", "disconnect"])
async def test_ready_early_exit_cleans_its_subscription(gateway, monkeypatch, stop):
    app, run_id = gateway
    subscribed = []
    cleaned = asyncio.Event()
    disconnected = asyncio.Event()
    original_subscribe = broadcaster.subscribe
    original_unsubscribe = broadcaster.unsubscribe

    async def subscribe(target):
        queue = await original_subscribe(target)
        subscribed.append(queue)
        return queue

    async def unsubscribe(target, queue):
        await original_unsubscribe(target, queue)
        cleaned.set()

    monkeypatch.setattr(broadcaster, "subscribe", subscribe)
    monkeypatch.setattr(broadcaster, "unsubscribe", unsubscribe)

    async def receive():
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if stop == "disconnect" and b"event: connected" in message.get("body", b""):
            disconnected.set()

    await asyncio.wait_for(app(stream_scope(run_id, b"limit=1" if stop == "limit" else b""), receive, send), 3)
    assert len(subscribed) == 1
    await asyncio.wait_for(cleaned.wait(), 2)
    assert run_id not in broadcaster._subscribers


@pytest.mark.anyio
async def test_disconnect_during_ready_send_closes_iterator_without_gc(gateway, monkeypatch):
    import gc

    app, run_id = gateway
    disconnected = asyncio.Event()
    sending_ready = asyncio.Event()
    never_release = asyncio.Event()
    original_send = EventSourceResponse._send_with_timeout

    async def paused_ready_send(response, send, message):
        await original_send(response, send, message)
        if b"event: connected" in message.get("body", b""):
            sending_ready.set()
            await never_release.wait()

    monkeypatch.setattr(EventSourceResponse, "_send_with_timeout", paused_ready_send)

    async def receive():
        await sending_ready.wait()
        disconnected.set()
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    enabled = gc.isenabled()
    gc.disable()
    try:
        await asyncio.wait_for(app(stream_scope(run_id, b""), receive, send), 3)
        assert sending_ready.is_set() and disconnected.is_set()
        assert run_id not in broadcaster._subscribers
    finally:
        if enabled: gc.enable()
        gc.collect()  # Cleanup after the assertion, never used to satisfy it.

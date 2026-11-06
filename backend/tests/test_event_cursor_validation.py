"""Invalid replay cursors must fail before successful SSE headers are sent."""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from scopewatch.app import create_app
from scopewatch.db import init_db
from scopewatch.events import broadcaster
from scopewatch.schemas import TaskScope

MIN_INTEGER = -(2**63)
MAX_INTEGER = 2**63 - 1


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOPEWATCH_AGENT_PROFILE", "mock")
    database = tmp_path / "gateway.db"
    init_db(database)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = create_app(db_path=database, workspace_root=workspace)
    run, _ = app.state.service.create_run(name="Synthetic event cursor", task_scope=TaskScope(
        task_description="Read outputs", allowed_paths=["outputs"], allowed_tools=["workspace"],
        allowed_operations=["read_text"], created_at="2026-11-05T00:00:00Z"))
    app.state.service.complete_run(run.id)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, run.id
    assert run.id not in broadcaster._subscribers


def bounded_stream_request(client, url, **kwargs):
    """Capture headers and the first SSE frame, then disconnect explicitly."""
    request = client.build_request("GET", url, **kwargs)
    messages = []

    async def invoke():
        disconnected = asyncio.Event()
        received = False

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.body" and (
                b"event: connected" in message.get("body", b"") or not message.get("more_body", False)
            ):
                disconnected.set()

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                 "http_version": "1.1", "method": "GET", "scheme": "http", "root_path": "",
                 "path": request.url.path, "raw_path": request.url.path.encode(),
                 "query_string": request.url.query, "headers": [(name.lower(), value) for name, value in request.headers.raw],
                 "server": ("testserver", 80), "client": ("127.0.0.1", 12345)}
        try:
            await asyncio.wait_for(client.app(scope, receive, send), timeout=2)
        except Exception:
            # The old generator can raise after sending 200. Preserve that
            # observed response so rejection assertions expose the defect.
            if not any(message["type"] == "http.response.start" for message in messages):
                raise

    asyncio.run(invoke())
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return httpx.Response(start["status"], headers=start["headers"], content=body, request=request)


def assert_rejected(response):
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    error = response.json()["error"]
    assert error["code"] == "SCHEMA_VALIDATION_ERROR"
    assert error["message"] == "Request validation failed."
    assert error["request_id"]
    assert "event: connected" not in response.text


@pytest.mark.parametrize("endpoint", ["events", "events/stream"])
@pytest.mark.parametrize("parameter", ["after_sequence", "limit"])
@pytest.mark.parametrize("value", [MIN_INTEGER - 1, MAX_INTEGER + 1])
def test_query_overflow_is_rejected_before_streaming(gateway, endpoint, parameter, value):
    client, run_id = gateway
    # limit=1 used to mask invalid SSE cursors by returning before parsing.
    params = {"limit": 1, parameter: value}
    get = bounded_stream_request if endpoint.endswith("stream") else lambda client, url, **kwargs: client.get(url, **kwargs)
    assert_rejected(get(client, f"/api/v1/runs/{run_id}/{endpoint}", params=params))
    assert run_id not in broadcaster._subscribers


@pytest.mark.parametrize("header", [str(MAX_INTEGER + 1), "9" * 5000], ids=["overflow", "digit-limit"])
@pytest.mark.parametrize("limit", [1, 2])
def test_oversized_numeric_header_is_rejected_before_streaming(gateway, header, limit):
    client, run_id = gateway
    assert_rejected(bounded_stream_request(client, f"/api/v1/runs/{run_id}/events/stream", params={"after_sequence": 0, "limit": limit}, headers={"Last-Event-ID": header}))
    assert run_id not in broadcaster._subscribers


@pytest.mark.parametrize("header", [b"oops", b"", b"-1", b"+1", b" 1", b"\xb2"])
def test_malformed_header_falls_back_to_query_cursor(gateway, header):
    client, run_id = gateway
    response = client.get(f"/api/v1/runs/{run_id}/events/stream?after_sequence=0&limit=3", headers=[(b"Last-Event-ID", header)])
    assert response.status_code == 200
    assert "id: 1" in response.text
    assert "id: 2" in response.text
    assert response.text.index("id: 1") < response.text.index("id: 2")


@pytest.mark.parametrize("header", ["1", "0" * 5000 + "1"], ids=["ordinary", "leading-zeros"])
def test_valid_header_precedes_even_out_of_range_query_cursor(gateway, header):
    client, run_id = gateway
    response = client.get(f"/api/v1/runs/{run_id}/events/stream", params={"after_sequence": MAX_INTEGER + 1, "limit": 2}, headers={"Last-Event-ID": header})
    assert response.status_code == 200
    assert "id: 1" not in response.text
    assert "id: 2" in response.text


@pytest.mark.parametrize("parameter", ["after_sequence", "limit"])
@pytest.mark.parametrize("value", [MIN_INTEGER, MAX_INTEGER])
def test_sqlite_integer_boundaries_remain_valid(gateway, parameter, value):
    client, run_id = gateway
    response = client.get(f"/api/v1/runs/{run_id}/events", params={parameter: value})
    assert response.status_code == 200
    expected = [] if parameter == "after_sequence" and value == MAX_INTEGER else [1, 2]
    assert [event["sequence"] for event in response.json()] == expected


@pytest.mark.parametrize("limit", [0, -1])
def test_existing_zero_and_negative_limit_semantics_remain_valid(gateway, limit):
    client, run_id = gateway
    history = client.get(f"/api/v1/runs/{run_id}/events", params={"after_sequence": -1, "limit": limit})
    assert history.status_code == 200
    assert len(history.json()) == (0 if limit == 0 else 2)
    stream = client.get(f"/api/v1/runs/{run_id}/events/stream", params={"after_sequence": 0, "limit": limit})
    assert stream.status_code == 200
    assert "event: connected" in stream.text
    assert "id:" not in stream.text


def test_malformed_query_still_rejected_with_valid_header(gateway):
    client, run_id = gateway
    assert_rejected(client.get(f"/api/v1/runs/{run_id}/events/stream?after_sequence=oops&limit=1", headers={"Last-Event-ID": "1"}))


@pytest.mark.parametrize("cursor", [MIN_INTEGER, MAX_INTEGER])
def test_stream_cursor_boundaries_remain_valid(gateway, cursor):
    client, run_id = gateway
    response = client.get(f"/api/v1/runs/{run_id}/events/stream", params={"after_sequence": cursor, "limit": 1 if cursor == MAX_INTEGER else 2})
    assert response.status_code == 200
    assert ("id: 1" in response.text) is (cursor == MIN_INTEGER)


def test_header_maximum_remains_valid(gateway):
    client, run_id = gateway
    response = client.get(f"/api/v1/runs/{run_id}/events/stream?limit=1", headers={"Last-Event-ID": str(MAX_INTEGER)})
    assert response.status_code == 200
    assert "event: connected" in response.text


def test_no_cursor_stays_live_only_without_replaying_history(gateway):
    client, run_id = gateway
    response = bounded_stream_request(client, f"/api/v1/runs/{run_id}/events/stream?limit=2")
    assert response.status_code == 200
    assert "event: connected" in response.text
    assert "id:" not in response.text

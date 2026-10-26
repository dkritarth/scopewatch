"""Issue #304: bound public request bodies and reasoning payloads.

Covers the pure-ASGI ingress middleware (raw ``receive()`` sequences, no
BaseHTTPMiddleware) and the Pydantic schema bounds on reasoning and
argument strings. All content is synthetic; no network is used.
"""

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from scopewatch.app import create_app
from scopewatch.config import (
    DEFAULT_MAX_REQUEST_BODY_BYTES,
    max_request_body_bytes,
)
from scopewatch.db import init_db
from scopewatch.ingress import BodySizeLimitMiddleware
from scopewatch.schemas import (
    MAX_ARGUMENT_STRING_CHARS,
    MAX_REASONING_TRACE_CHARS,
)

LIMIT = max_request_body_bytes()


# ---------------------------------------------------------------------------
# Helpers for driving the raw ASGI app with crafted receive() sequences.
# ---------------------------------------------------------------------------


def make_scope(method: str, path: str, headers: Optional[list[tuple[str, str]]] = None) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in (headers or [])
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }


def call_asgi(
    app: Any,
    method: str,
    path: str,
    messages: Optional[list[dict[str, Any]]] = None,
    headers: Optional[list[tuple[str, str]]] = None,
) -> tuple[Optional[int], dict[str, str], bytes]:
    """Run one raw ASGI round trip; returns (status, headers, body)."""

    async def _run() -> tuple[Optional[int], dict[str, str], bytes]:
        scope = make_scope(method, path, headers)
        start: Optional[dict[str, Any]] = None
        body_parts: list[bytes] = []
        index = 0
        received: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            nonlocal index
            received.append({"called": index})
            index += 1
            if messages and index - 1 < len(messages):
                return messages[index - 1]
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
            elif message["type"] == "http.response.body":
                body_parts.append(message.get("body", b""))

        await app(scope, receive, send)
        status = start["status"] if start else None
        resp_headers = {
            key.decode("latin-1"): value.decode("latin-1")
            for key, value in (start or {}).get("headers", [])
        }
        return status, resp_headers, b"".join(body_parts)

    return asyncio.run(_run())


def request_messages(
    data: bytes,
    chunk_size: Optional[int] = None,
    more_flags: Optional[list[bool]] = None,
) -> list[dict[str, Any]]:
    """Build http.request messages, optionally split into chunks."""
    if chunk_size is None:
        return [{"type": "http.request", "body": data, "more_body": False}]
    chunks = [data[i : i + chunk_size] for i in range(0, len(data), chunk_size)] or [b""]
    messages = []
    for i, chunk in enumerate(chunks):
        more = i < len(chunks) - 1
        if more_flags is not None:
            more = more_flags[i] if i < len(more_flags) else more
        messages.append({"type": "http.request", "body": chunk, "more_body": more})
    return messages


def run_payload(name: str = "Ingress Bounds Run") -> bytes:
    body = {
        "name": name,
        "task_scope": {
            "schema_version": "1",
            "task_description": "Audit approved invoices (synthetic)",
            "allowed_paths": ["invoices/approved", "outputs"],
            "blocked_paths": ["invoices/private"],
            "allowed_tools": ["workspace"],
            "allowed_operations": ["list_directory", "read_text", "write_text"],
            "allowed_network_destinations": [],
            "requires_approval": ["delete_path"],
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    return json.dumps(body).encode()


@pytest.fixture
def raw_env(tmp_path: Path):
    """App built for raw ASGI driving, with a scratch DB and workspace."""
    db_file = tmp_path / "ingress_test.db"
    init_db(db_file)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = create_app(db_path=db_file, workspace_root=workspace)
    return app, db_file


def count_rows(db_file: Path, table: str) -> int:
    conn = sqlite3.connect(db_file)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table};").fetchone()[0])
    finally:
        conn.close()


def create_run_via_api(client: TestClient) -> str:
    resp = client.post("/api/v1/runs", content=run_payload(), headers={"content-type": "application/json"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# Middleware unit tests: the inner app must never see oversized bodies.
# ---------------------------------------------------------------------------


class RecordingApp:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.calls.append(scope)
        body = b""
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": json.dumps({"saw": len(body)}).encode()})


def test_middleware_rejects_declared_oversized_content_length_before_app() -> None:
    inner = RecordingApp()
    mw = BodySizeLimitMiddleware(inner, max_body_bytes=1024)
    status, _, body = call_asgi(
        mw,
        "POST",
        "/api/v1/runs",
        messages=request_messages(b"tiny"),
        headers=[("content-length", str(1024 + 1))],
    )
    assert status == 413
    assert json.loads(body) == {"error": "request body too large"}
    assert inner.calls == [], "oversized request must never reach the app"


def test_middleware_rejects_streamed_body_over_limit_without_content_length() -> None:
    inner = RecordingApp()
    mw = BodySizeLimitMiddleware(inner, max_body_bytes=1024)
    # 1025 bytes across messages, no content-length header at all.
    messages = request_messages(b"x" * 1025, chunk_size=400)
    status, _, body = call_asgi(mw, "POST", "/api/v1/runs", messages=messages)
    assert status == 413
    assert json.loads(body) == {"error": "request body too large"}
    assert inner.calls == [], "streamed oversized body must never reach the app"


def test_middleware_passes_split_body_under_limit_to_app() -> None:
    inner = RecordingApp()
    mw = BodySizeLimitMiddleware(inner, max_body_bytes=1024)
    messages = request_messages(b"y" * 900, chunk_size=300)
    status, _, body = call_asgi(
        mw,
        "POST",
        "/api/v1/runs",
        messages=messages,
        headers=[("content-length", "900")],
    )
    assert status == 200
    assert json.loads(body) == {"saw": 900}
    assert len(inner.calls) == 1


def test_middleware_rejects_exactly_one_byte_over_limit() -> None:
    inner = RecordingApp()
    mw = BodySizeLimitMiddleware(inner, max_body_bytes=1024)
    status, _, _ = call_asgi(
        mw,
        "POST",
        "/api/v1/runs",
        messages=request_messages(b"z" * 1025, chunk_size=1025),
        headers=[("content-length", "1025")],
    )
    assert status == 413
    assert inner.calls == []


def test_middleware_allows_body_exactly_at_limit() -> None:
    inner = RecordingApp()
    mw = BodySizeLimitMiddleware(inner, max_body_bytes=1024)
    status, _, body = call_asgi(
        mw,
        "POST",
        "/api/v1/runs",
        messages=request_messages(b"z" * 1024, chunk_size=512),
        headers=[("content-length", "1024")],
    )
    assert status == 200
    assert json.loads(body) == {"saw": 1024}


# ---------------------------------------------------------------------------
# App-level behaviour through the real middleware stack.
# ---------------------------------------------------------------------------


def test_normal_synthetic_body_creates_run(raw_env) -> None:
    app, _ = raw_env
    status, _, body = call_asgi(
        app,
        "POST",
        "/api/v1/runs",
        messages=request_messages(run_payload()),
        headers=[("content-type", "application/json"), ("content-length", str(len(run_payload())))],
    )
    assert status == 201
    assert json.loads(body)["name"] == "Ingress Bounds Run"


def test_oversized_body_returns_sanitized_413_and_no_records(raw_env) -> None:
    app, db_file = raw_env
    client = TestClient(app, raise_server_exceptions=False)
    run_id = create_run_via_api(client)

    events_before = count_rows(db_file, "evidence_events")
    status, _, body = call_asgi(
        app,
        "POST",
        f"/api/v1/runs/{run_id}/actions",
        messages=request_messages(b"x" * (LIMIT + 1), chunk_size=64 * 1024),
        headers=[("content-type", "application/json")],
    )
    assert status == 413
    assert json.loads(body) == {"error": "request body too large"}
    # No partial or buffered write leaked a first chunk through.
    assert b"workspace" not in body

    assert count_rows(db_file, "action_requests") == 0
    assert count_rows(db_file, "policy_decisions") == 0
    assert count_rows(db_file, "execution_receipts") == 0
    # The run's own RUN_CREATED event is the only evidence; the rejected
    # request adds none.
    assert count_rows(db_file, "evidence_events") == events_before
    assert run_id  # run itself is untouched, created before the oversized call


def test_oversized_content_length_on_actions_creates_nothing(raw_env) -> None:
    app, db_file = raw_env
    client = TestClient(app, raise_server_exceptions=False)
    run_id = create_run_via_api(client)

    status, _, body = call_asgi(
        app,
        "POST",
        f"/api/v1/runs/{run_id}/actions",
        messages=request_messages(b"{}"),
        headers=[("content-length", str(LIMIT + 1)), ("content-type", "application/json")],
    )
    assert status == 413
    assert json.loads(body) == {"error": "request body too large"}
    assert count_rows(db_file, "action_requests") == 0
    assert count_rows(db_file, "execution_receipts") == 0


def test_body_split_across_messages_under_limit_creates_run(raw_env) -> None:
    app, _ = raw_env
    payload = run_payload()
    status, _, body = call_asgi(
        app,
        "POST",
        "/api/v1/runs",
        messages=request_messages(payload, chunk_size=100),
        headers=[("content-type", "application/json"), ("content-length", str(len(payload)))],
    )
    assert status == 201
    assert json.loads(body)["name"] == "Ingress Bounds Run"


def test_get_and_health_are_exempt_from_body_limit(raw_env) -> None:
    app, _ = raw_env
    # GET carries no meaningful body; a large declared content-length on a
    # public read must not turn into a 413.
    status, _, _ = call_asgi(
        app,
        "GET",
        "/api/v1/health",
        messages=request_messages(b"a" * (LIMIT + 1), chunk_size=64 * 1024),
        headers=[("content-length", str(LIMIT + 1))],
    )
    assert status == 200


def test_sse_stream_route_is_exempt(raw_env) -> None:
    app, _ = raw_env
    # Unknown run: the route itself answers (404). The point is that the
    # ingress middleware must not answer first with 413.
    status, _, body = call_asgi(
        app,
        "GET",
        "/api/v1/runs/no-such-run/events/stream",
        messages=request_messages(b"a" * (LIMIT + 1), chunk_size=64 * 1024),
        headers=[("content-length", str(LIMIT + 1))],
    )
    assert status == 404
    assert b"too large" not in body


def test_post_to_sse_stream_route_with_oversized_chunked_body_gets_413(raw_env) -> None:
    app, _ = raw_env
    status, _, body = call_asgi(
        app,
        "POST",
        "/api/v1/runs/x/events/stream",
        messages=request_messages(b"a" * (LIMIT + 1), chunk_size=64 * 1024),
    )
    assert status == 413
    assert json.loads(body) == {"error": "request body too large"}


# ---------------------------------------------------------------------------
# Schema bounds: reasoning traces, summaries, argument strings.
# ---------------------------------------------------------------------------


@pytest.fixture
def client_env(tmp_path: Path):
    db_file = tmp_path / "schema_bounds_test.db"
    init_db(db_file)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = create_app(db_path=db_file, workspace_root=workspace)
    client = TestClient(app, raise_server_exceptions=False)
    return client, db_file


def _oversized_trace() -> str:
    return "t" * (MAX_REASONING_TRACE_CHARS + 1)


def test_oversized_reasoning_trace_returns_422_without_echo(client_env) -> None:
    client, db_file = client_env
    run_id = create_run_via_api(client)
    payload = {
        "tool": "workspace",
        "operation": "read_text",
        "resource": "invoices/approved/vendor-a.txt",
        "arguments": {"path": "invoices/approved/vendor-a.txt"},
        "exposed_reasoning_trace": _oversized_trace(),
    }
    resp = client.post(
        f"/api/v1/runs/{run_id}/actions",
        json=payload,
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 422
    text = resp.text
    assert "tttttttttt" not in text, "the oversized content must never be echoed"
    assert _oversized_trace() not in text
    # A rejected action must not leave evidence behind either.
    assert count_rows(db_file, "action_requests") == 0
    assert count_rows(db_file, "execution_receipts") == 0


def test_oversized_reasoning_summary_returns_422(client_env) -> None:
    client, _ = client_env
    run_id = create_run_via_api(client)
    from scopewatch.schemas import MAX_REASONING_SUMMARY_CHARS

    payload = {
        "tool": "workspace",
        "operation": "read_text",
        "resource": "invoices/approved/vendor-a.txt",
        "arguments": {"path": "invoices/approved/vendor-a.txt"},
        "reasoning_summary": "s" * (MAX_REASONING_SUMMARY_CHARS + 1),
    }
    resp = client.post(f"/api/v1/runs/{run_id}/actions", json=payload)
    assert resp.status_code == 422
    assert "ssssssssss" not in resp.text


def test_oversized_argument_string_returns_422(client_env) -> None:
    client, _ = client_env
    run_id = create_run_via_api(client)
    payload = {
        "tool": "workspace",
        "operation": "write_text",
        "resource": "outputs/note.txt",
        "arguments": {"path": "outputs/note.txt", "content": "c" * (MAX_ARGUMENT_STRING_CHARS + 1)},
    }
    resp = client.post(f"/api/v1/runs/{run_id}/actions", json=payload)
    assert resp.status_code == 422
    assert "cccccccccc" not in resp.text


def test_oversized_resolution_reason_returns_422(client_env) -> None:
    from scopewatch.schemas import MAX_RESOLUTION_REASON_CHARS

    client, _ = client_env
    run_id = create_run_via_api(client)
    action = client.post(
        f"/api/v1/runs/{run_id}/actions",
        json={
            "tool": "workspace",
            "operation": "delete_path",
            "resource": "outputs/note.txt",
            "arguments": {"path": "outputs/note.txt"},
        },
    )
    assert action.status_code == 201
    approval_id = action.json()["approval_request"]["id"]
    resp = client.post(
        f"/api/v1/approvals/{approval_id}/approve",
        json={"resolution_reason": "r" * (MAX_RESOLUTION_REASON_CHARS + 1)},
    )
    assert resp.status_code == 422
    assert "rrrrrrrrrr" not in resp.text


def test_normal_reasoning_payload_still_accepted(client_env) -> None:
    client, _ = client_env
    run_id = create_run_via_api(client)
    payload = {
        "tool": "workspace",
        "operation": "read_text",
        "resource": "invoices/approved/vendor-a.txt",
        "arguments": {"path": "invoices/approved/vendor-a.txt"},
        "reasoning_summary": "Read the approved vendor invoice to compare totals.",
        "exposed_reasoning_trace": "step 1: pick file; step 2: read; step 3: compare",
    }
    resp = client.post(f"/api/v1/runs/{run_id}/actions", json=payload)
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# Configuration: env override with sane default.
# ---------------------------------------------------------------------------


def test_default_limit_is_256_kib() -> None:
    assert DEFAULT_MAX_REQUEST_BODY_BYTES == 256 * 1024


def test_env_override_changes_limit(raw_env, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = raw_env
    monkeypatch.setenv("SCOPEWATCH_MAX_BODY_BYTES", "1024")
    rebuilt = create_app(
        db_path=raw_env[1].with_name("env_override.db"),
        workspace_root=Path(app.state.service.workspace_root),
    )
    status, _, _ = call_asgi(
        rebuilt,
        "POST",
        "/api/v1/runs",
        messages=request_messages(b"q" * 2048, chunk_size=1024),
        headers=[("content-type", "application/json")],
    )
    assert status == 413
    status, _, _ = call_asgi(
        rebuilt,
        "POST",
        "/api/v1/runs",
        messages=request_messages(b"q" * 100),
        headers=[("content-type", "application/json"), ("content-length", "100")],
    )
    assert status == 422  # reached FastAPI validation, so under the 1 KiB limit


def test_malformed_env_override_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOPEWATCH_MAX_BODY_BYTES", "not-a-number")
    assert max_request_body_bytes() == DEFAULT_MAX_REQUEST_BODY_BYTES
    monkeypatch.setenv("SCOPEWATCH_MAX_BODY_BYTES", "0")
    assert max_request_body_bytes() == DEFAULT_MAX_REQUEST_BODY_BYTES

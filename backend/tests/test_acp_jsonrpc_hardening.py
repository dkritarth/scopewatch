"""Regression tests for malformed JSON-RPC input and HOLD without an action ID."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from scopewatch.acp_adapter import AcpClientAdapter, HoldConfig


def _adapter(handler: Any) -> tuple[AcpClientAdapter, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = httpx.Client(transport=httpx.MockTransport(wrapped), base_url="http://gateway.invalid")
    return AcpClientAdapter(http, "run-1", hold=HoldConfig(timeout_s=0.5, poll_interval_s=0.01)), seen


def _never(request: httpx.Request) -> httpx.Response:
    raise AssertionError("gateway must not be called")


@pytest.mark.parametrize("request_obj", [[], "x", 7, None, [{"jsonrpc": "2.0"}]])
def test_non_object_request_gets_invalid_request_not_a_crash(request_obj: Any) -> None:
    adapter, seen = _adapter(_never)
    resp = adapter.handle_jsonrpc(request_obj)
    assert resp["error"]["code"] == -32600
    assert resp["id"] is None
    assert not seen


@pytest.mark.parametrize("params", ["abc", [1, 2], 5, True])
def test_non_object_params_get_invalid_params(params: Any) -> None:
    adapter, seen = _adapter(_never)
    resp = adapter.handle_jsonrpc(
        {"jsonrpc": "2.0", "id": 1, "method": "fs/read_text_file", "params": params}
    )
    assert resp["error"]["code"] == -32602
    assert not seen


@pytest.mark.parametrize("method", [["fs/read_text_file"], {"a": 1}, 5])
def test_non_string_method_gets_invalid_request(method: Any) -> None:
    adapter, seen = _adapter(_never)
    resp = adapter.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": method})
    assert resp["error"]["code"] == -32600
    assert not seen


@pytest.mark.parametrize("bad_id", [[1], {"a": 1}, True, 1.5])
def test_invalid_request_id_is_not_echoed(bad_id: Any) -> None:
    adapter, _ = _adapter(_never)
    resp = adapter.handle_jsonrpc({"jsonrpc": "2.0", "id": bad_id, "method": "initialize"})
    assert resp["error"]["code"] == -32600
    assert resp["id"] is None


@pytest.mark.parametrize("action_request", [None, {}, {"id": None}, {"id": 5}, {"id": ""}])
def test_hold_without_usable_action_id_fails_closed_without_polling(action_request: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "action_request": action_request,
                "policy_decision": {"outcome": "HOLD", "reason_code": "X"},
            },
        )

    adapter, seen = _adapter(handler)
    resp = adapter.handle_jsonrpc(
        {"jsonrpc": "2.0", "id": 1, "method": "fs/delete_file", "params": {"path": "a.txt"}}
    )
    assert resp["error"]["data"]["reason_code"] == "HOLD_MISSING_ACTION_ID"
    assert [r.method for r in seen] == ["POST"], "must not poll /actions/None"


def test_unknown_method_message_does_not_echo_caller_input() -> None:
    adapter, seen = _adapter(_never)
    method = "x/" + "A" * 5000
    resp = adapter.handle_jsonrpc({"jsonrpc": "2.0", "id": 9, "method": method})
    assert resp["id"] == 9
    assert resp["error"]["code"] == -32601
    assert "A" * 10 not in resp["error"]["message"]
    assert len(resp["error"].get("data", {}).get("method", "")) <= 64
    assert not seen

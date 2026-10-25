"""Synthetic gated regressions for review and terminal deadline races (#283)."""

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading
import time
from typing import Any

import pytest

from tests.test_agent_loop import create_test_run, test_env, wait_for_approval
from scopewatch.agent.loop import AgentLoop
from scopewatch.agent.tools import GatewayDispatcher, convert_tool_call_to_submit_request
from scopewatch.providers.client import ChatResult, MockProviderClient


def read_provider() -> MockProviderClient:
    provider = MockProviderClient()
    provider.enqueue(ChatResult(content="Read invoice", model="mock", profile="mock", tool_calls=[{
        "id": "read", "type": "function", "function": {
            "name": "read_text", "arguments": json.dumps({"path": "invoices/approved/vendor-a.txt"}),
        },
    }]))
    provider.enqueue(ChatResult(content="Finished.", model="mock", profile="mock", tool_calls=[]))
    return provider


def test_approval_after_last_pending_snapshot_waits_for_receipt(
    test_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scopewatch.service as service

    client = test_env["client"]
    run_id = create_test_run(client, requires_approval=["read_text"])
    pending, return_pending = threading.Event(), threading.Event()
    executing, release = threading.Event(), threading.Event()
    boundary_checked, model_resumed = threading.Event(), threading.Event()
    execute = service.execute_action

    def gated_execute(*args, **kwargs):
        executing.set()
        assert release.wait(10)
        return execute(*args, **kwargs)

    monkeypatch.setattr(service, "execute_action", gated_execute)

    class Dispatcher(GatewayDispatcher):
        def list_approvals(self, run):
            approvals = super().list_approvals(run)
            if approvals and approvals[0].status == "PENDING":
                pending.set()
                assert return_pending.wait(10)
            elif executing.is_set() and not release.is_set():
                boundary_checked.set()
            return approvals

    provider = read_provider()
    complete = provider.complete
    observed = {}

    def observed_complete(messages, **kwargs):
        if any(m.get("role") == "tool" for m in messages):
            model_resumed.set()
            boundary_checked.set()
            observed["feedback"] = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
            observed["receipt"] = client.get(
                f"/api/v1/runs/{run_id}/actions/{observed['action_id']}"
            ).json()["execution_receipt"]
        return complete(messages, **kwargs)

    monkeypatch.setattr(provider, "complete", observed_complete)
    loop = AgentLoop(run_id=run_id, provider_client=provider, dispatcher=Dispatcher(http_client=client),
                     approval_timeout_s=5, poll_interval_s=.001)
    with ThreadPoolExecutor(max_workers=2) as workers:
        future = workers.submit(loop.run)
        try:
            assert pending.wait(5)
            approval = wait_for_approval(client, run_id)
            observed["action_id"] = approval["action_request_id"]
            review = workers.submit(client.post, f"/api/v1/approvals/{approval['id']}/approve", json={})
            assert executing.wait(5)
            loop.approval_timeout_s = 0
            return_pending.set()
            assert boundary_checked.wait(5)
            resumed_before_receipt = model_resumed.is_set()
        finally:
            return_pending.set()
            release.set()
        assert review.result(timeout=10).status_code == 200
        result = future.result(timeout=10)
    assert not resumed_before_receipt, observed
    assert observed["receipt"]["status"] == "EXECUTED"
    assert observed["feedback"][0]["content"] == "Vendor A: Amount $1,250.00\n"
    assert result.status == "COMPLETED"
    assert "not executed" not in result.final_response


def test_completion_retry_crossing_wall_deadline_fails_after_receipt(
    test_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scopewatch.service as service

    client = test_env["client"]
    run_id = create_test_run(client, requires_approval=["read_text"])
    dispatcher = GatewayDispatcher(http_client=client)
    held = dispatcher.submit_action(run_id, convert_tool_call_to_submit_request(
        "read_text", {"path": "invoices/approved/vendor-a.txt"},
    ))
    executing, release, refused = threading.Event(), threading.Event(), threading.Event()
    execute, post = service.execute_action, client.post

    def gated_execute(*args, **kwargs):
        executing.set()
        assert release.wait(10)
        return execute(*args, **kwargs)

    def observed_post(url, *args, **kwargs):
        response = post(url, *args, **kwargs)
        if url.endswith("/complete") and response.status_code == 409:
            refused.set()
        return response

    monkeypatch.setattr(service, "execute_action", gated_execute)
    monkeypatch.setattr(client, "post", observed_post)
    provider = MockProviderClient()
    provider.enqueue(ChatResult(content="Finished.", model="mock", profile="mock", tool_calls=[]))
    loop = AgentLoop(run_id=run_id, provider_client=provider, dispatcher=dispatcher,
                     wall_clock_timeout_s=.2)
    with ThreadPoolExecutor(max_workers=2) as workers:
        review = workers.submit(client.post, f"/api/v1/approvals/{held.approval_request.id}/approve", json={})
        try:
            assert executing.wait(5)
            future = workers.submit(loop.run)
            assert refused.wait(5)
            # Cross the real agent deadline only after the gateway refused completion.
            threading.Event().wait(.25)
            assert not future.done()
        finally:
            release.set()
        assert review.result(timeout=10).status_code == 200
        result = future.result(timeout=10)
    assert result.status == "FAILED"
    assert "Wall clock timeout" in result.error
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "FAILED"
    action = client.get(f"/api/v1/runs/{run_id}/actions/{held.action_request.id}").json()
    assert action["approval_request"]["status"] == "CONSUMED"
    assert action["execution_receipt"]["status"] == "EXECUTED"
    events = client.get(f"/api/v1/runs/{run_id}/events").json()
    kinds = [event["event_type"] for event in events]
    assert "RUN_COMPLETED" not in kinds
    assert kinds.index("EXECUTION_SUCCEEDED") < kinds.index("SYSTEM_ERROR")


def test_review_timeout_atomically_expires_pending_and_is_idempotent(test_env: dict[str, Any]) -> None:
    client = test_env["client"]
    run_id = create_test_run(client, requires_approval=["read_text"])
    held = GatewayDispatcher(http_client=client).submit_action(
        run_id, convert_tool_call_to_submit_request("read_text", {"path": "invoices/approved/vendor-a.txt"}),
    )
    approval_id = held.approval_request.id
    for _ in range(2):
        response = client.post(f"/api/v1/approvals/{approval_id}/expire")
        assert response.status_code == 200
        assert response.json()["status"] == "EXPIRED"
        assert response.json()["resolved_by"] == "system"
    assert client.post(f"/api/v1/approvals/{approval_id}/approve", json={}).status_code == 409
    action = client.get(f"/api/v1/runs/{run_id}/actions/{held.action_request.id}").json()
    assert action["execution_receipt"] is None
    events = client.get(f"/api/v1/runs/{run_id}/events").json()
    expired = [event for event in events if event["event_type"] == "APPROVAL_EXPIRED"]
    assert len(expired) == 1
    assert expired[0]["actor"] == "system"
    assert expired[0]["details"]["reason"] == "Agent approval wait timed out."
    assert not any(event["event_type"] == "EXECUTION_STARTED" for event in events)
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "ACTIVE"


def test_unreviewed_timeout_feedback_matches_expired_evidence(test_env: dict[str, Any]) -> None:
    client = test_env["client"]
    run_id = create_test_run(client, requires_approval=["read_text"])
    result = AgentLoop(run_id=run_id, provider_client=read_provider(),
                       dispatcher=GatewayDispatcher(http_client=client), approval_timeout_s=0).run()
    assert result.status == "COMPLETED"
    assert "not executed" in result.final_response
    feedback = [json.loads(m["content"]) for m in result.messages if m.get("role") == "tool"]
    assert feedback[0]["status"] == "TIMEOUT"
    action = client.get(f"/api/v1/runs/{run_id}/actions/{result.actions[0].action_request.id}").json()
    assert action["approval_request"]["status"] == "EXPIRED"
    assert action["execution_receipt"] is None


@pytest.mark.parametrize("resolution, status", [("approve", "CONSUMED"), ("deny", "DENIED")])
def test_review_timeout_preserves_settled_review(
    test_env: dict[str, Any], resolution: str, status: str,
) -> None:
    client = test_env["client"]
    run_id = create_test_run(client, requires_approval=["read_text"])
    held = GatewayDispatcher(http_client=client).submit_action(
        run_id, convert_tool_call_to_submit_request("read_text", {"path": "invoices/approved/vendor-a.txt"}),
    )
    approval_id = held.approval_request.id
    assert client.post(f"/api/v1/approvals/{approval_id}/{resolution}", json={}).status_code == 200
    response = client.post(f"/api/v1/approvals/{approval_id}/expire")
    assert response.status_code == 200
    assert response.json()["status"] == status
    events = client.get(f"/api/v1/runs/{run_id}/events").json()
    assert not any(event["event_type"] == "APPROVAL_EXPIRED" for event in events)


def test_timeout_wins_against_reviewer_with_stale_pending_read(
    test_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scopewatch.service import ScopewatchService

    client = test_env["client"]
    run_id = create_test_run(client, requires_approval=["read_text"])
    held = GatewayDispatcher(http_client=client).submit_action(
        run_id, convert_tool_call_to_submit_request("read_text", {"path": "invoices/approved/vendor-a.txt"}),
    )
    approval_id = held.approval_request.id
    ready, release = threading.Event(), threading.Event()
    resolve_workspace = ScopewatchService._resolve_run_workspace

    def gated_workspace(*args, **kwargs):
        workspace = resolve_workspace(*args, **kwargs)
        ready.set()
        assert release.wait(10)
        return workspace

    monkeypatch.setattr(ScopewatchService, "_resolve_run_workspace", gated_workspace)
    with ThreadPoolExecutor(max_workers=1) as workers:
        review = workers.submit(client.post, f"/api/v1/approvals/{approval_id}/approve", json={})
        try:
            assert ready.wait(5)
            response = client.post(f"/api/v1/approvals/{approval_id}/expire")
        finally:
            release.set()
        assert review.result(timeout=10).status_code == 409
    assert response.status_code == 200
    assert response.json()["status"] == "EXPIRED"
    action = client.get(f"/api/v1/runs/{run_id}/actions/{held.action_request.id}").json()
    assert action["execution_receipt"] is None
    events = client.get(f"/api/v1/runs/{run_id}/events").json()
    assert not any(event["event_type"] == "EXECUTION_STARTED" for event in events)


def test_completion_retry_refreshes_deadline_budget_under_write_lock(
    test_env: dict[str, Any],
) -> None:
    client = test_env["client"]
    db_file = test_env["db_file"]
    run_id = create_test_run(client, requires_approval=[])

    provider = MockProviderClient()
    provider.enqueue(ChatResult(content="Finished.", model="mock", profile="mock", tool_calls=[]))

    class LockingDispatcher(GatewayDispatcher):
        def complete_run(self, run_id, *, deadline=None):
            holder = sqlite3.connect(str(db_file), timeout=10.0, isolation_level=None, check_same_thread=False)
            holder.execute("PRAGMA busy_timeout = 10000")
            acquired = threading.Event()

            def grab():
                holder.execute("BEGIN IMMEDIATE")
                acquired.set()
                time.sleep(0.6)
                holder.execute("COMMIT")
                holder.close()

            grabber = threading.Thread(target=grab, daemon=True)
            grabber.start()
            assert acquired.wait(5)
            try:
                return super().complete_run(run_id, deadline=deadline)
            finally:
                grabber.join(10)

    loop = AgentLoop(run_id=run_id, provider_client=provider, dispatcher=LockingDispatcher(http_client=client),
                     wall_clock_timeout_s=0.25)
    result = loop.run()
    assert result.status == "FAILED"
    assert "Wall clock timeout" in (result.error or "")
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "FAILED"
    events = client.get(f"/api/v1/runs/{run_id}/events").json()
    kinds = [e["event_type"] for e in events]
    assert "RUN_COMPLETED" not in kinds

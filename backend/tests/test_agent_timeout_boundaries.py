"""Synthetic gated regressions for review and terminal deadline races (#283)."""

from concurrent.futures import ThreadPoolExecutor
import json
import threading
from typing import Any

import httpx
import pytest

from tests.scripted_clock import ScriptedClock
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

    clock = ScriptedClock()
    test_env["app"].state.service.time_fn = clock
    client = test_env["client"]
    run_id = create_test_run(client, requires_approval=["read_text"])
    dispatcher = GatewayDispatcher(http_client=client, time_fn=clock, sleep_fn=clock.sleep)
    held = dispatcher.submit_action(run_id, convert_tool_call_to_submit_request(
        "read_text", {"path": "invoices/approved/vendor-a.txt"},
    ))
    executing, release, refused = threading.Event(), threading.Event(), threading.Event()
    return_refusal = threading.Event()
    execute, post = service.execute_action, client.post

    def gated_execute(*args, **kwargs):
        executing.set()
        assert release.wait(10)
        return execute(*args, **kwargs)

    def observed_post(url, *args, **kwargs):
        response = post(url, *args, **kwargs)
        if url.endswith("/complete") and response.status_code == 409:
            refused.set()
            assert return_refusal.wait(5)
        return response

    monkeypatch.setattr(service, "execute_action", gated_execute)
    monkeypatch.setattr(client, "post", observed_post)
    provider = MockProviderClient()
    provider.enqueue(ChatResult(content="Finished.", model="mock", profile="mock", tool_calls=[]))
    loop = AgentLoop(run_id=run_id, provider_client=provider, dispatcher=dispatcher,
                     wall_clock_timeout_s=.2, time_fn=clock, sleep_fn=clock.sleep)
    with ThreadPoolExecutor(max_workers=2) as workers:
        review = workers.submit(client.post, f"/api/v1/approvals/{held.approval_request.id}/approve", json={})
        try:
            assert executing.wait(5)
            future = workers.submit(loop.run)
            assert refused.wait(5)
            # Cross the deadline only after the gateway refused completion.
            clock.advance(.25)
            return_refusal.set()
            assert not future.done()
        finally:
            return_refusal.set()
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
    clock = ScriptedClock()
    test_env["app"].state.service.time_fn = clock
    client = test_env["client"]
    run_id = create_test_run(client, requires_approval=["read_text"])
    result = AgentLoop(run_id=run_id, provider_client=read_provider(),
                       dispatcher=GatewayDispatcher(http_client=client, time_fn=clock, sleep_fn=clock.sleep),
                       approval_timeout_s=0.1, poll_interval_s=0.05,
                       time_fn=clock, sleep_fn=clock.sleep).run()
    assert clock.sleeps == [0.05, 0.05]
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


@pytest.mark.parametrize("lock_wait", [0.25, 0.35])
def test_completion_retry_refreshes_deadline_budget_under_write_lock(
    test_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, lock_wait: float,
) -> None:
    """A deferred completion retry recomputes its deadline budget under real SQLite lock contention.

    Acceptance criteria for #283:
    1. First completion attempt encounters 409 APPROVED_ACTION_IN_FLIGHT and retries.
    2. Monotonic time elapses before the second completion request is dispatched.
    3. The second request recomputes remaining deadline budget (decreasing sent budget).
    4. The second request arrives before the deadline, then waits on the real SQLite
       write lock held by receipt persistence. The lock wait exceeds the refreshed remaining
       budget but is LESS than the initial stale budget.
    5. The gateway rejects the second attempt with 409 COMPLETION_DEADLINE_EXCEEDED.
    6. The loop and stored run fail closed (FAILED), with zero RUN_COMPLETED evidence.
    """
    import scopewatch.service as service
    from scopewatch.repository import ScopewatchRepository

    clock = ScriptedClock()
    client = test_env["client"]
    run_id = create_test_run(client, requires_approval=["read_text"])
    dispatcher = GatewayDispatcher(http_client=client, time_fn=clock, sleep_fn=clock.sleep)
    held = dispatcher.submit_action(
        run_id,
        convert_tool_call_to_submit_request(
            "read_text", {"path": "invoices/approved/vendor-a.txt"},
        ),
    )
    approval_id = held.approval_request.id

    executing = threading.Event()
    release_execution = threading.Event()
    first_refused = threading.Event()
    return_first_refusal = threading.Event()
    persisting = threading.Event()
    release_receipt = threading.Event()
    second_entered = threading.Event()

    real_execute = service.execute_action
    real_create_receipt = ScopewatchRepository.create_execution_receipt
    real_complete_svc = test_env["app"].state.service.complete_run
    real_post = client.post

    def gated_execute(*args: Any, **kwargs: Any) -> Any:
        executing.set()
        assert release_execution.wait(10), "release_execution timed out"
        return real_execute(*args, **kwargs)

    def gated_create_receipt(*args: Any, **kwargs: Any) -> Any:
        # Called inside `with db_transaction(conn):`, so conn holds BEGIN IMMEDIATE
        persisting.set()
        assert release_receipt.wait(10), "release_receipt timed out"
        return real_create_receipt(*args, **kwargs)

    complete_svc_calls = 0

    def observed_complete_svc(*args: Any, **kwargs: Any) -> Any:
        nonlocal complete_svc_calls
        complete_svc_calls += 1
        return real_complete_svc(*args, **kwargs)

    def service_time() -> float:
        now = clock()
        if complete_svc_calls == 2:
            second_entered.set()
        return now

    test_env["app"].state.service.time_fn = service_time

    sent_budgets: list[float] = []
    completion_errors: list[str] = []
    complete_post_calls = 0

    def tracking_post(url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        nonlocal complete_post_calls
        if url.endswith("/complete") and run_id in url:
            complete_post_calls += 1
            params = kwargs.get("params") or {}
            if isinstance(params, dict) and "deadline_budget" in params:
                sent_budgets.append(float(params["deadline_budget"]))
            resp = real_post(url, *args, **kwargs)
            if resp.status_code == 409:
                completion_errors.append(resp.json()["error"]["code"])
            if complete_post_calls == 1 and resp.status_code == 409:
                first_refused.set()
                # Consume time before returning so the next attempt dispatches with less remaining budget
                clock.advance(0.25)
                assert return_first_refusal.wait(5), "Receipt lock was not acquired"
            return resp
        return real_post(url, *args, **kwargs)

    monkeypatch.setattr(service, "execute_action", gated_execute)
    monkeypatch.setattr(ScopewatchRepository, "create_execution_receipt", gated_create_receipt)
    monkeypatch.setattr(test_env["app"].state.service, "complete_run", observed_complete_svc)
    monkeypatch.setattr(client, "post", tracking_post)

    provider = MockProviderClient()
    provider.enqueue(ChatResult(content="Finished.", model="mock", profile="mock", tool_calls=[]))

    wall_clock = 0.55
    loop = AgentLoop(
        run_id=run_id,
        provider_client=provider,
        dispatcher=GatewayDispatcher(http_client=client, time_fn=clock, sleep_fn=clock.sleep),
        wall_clock_timeout_s=wall_clock,
        time_fn=clock, sleep_fn=clock.sleep,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        review_future = pool.submit(
            client.post, f"/api/v1/approvals/{approval_id}/approve", json={}
        )
        try:
            assert executing.wait(5), "Action never began executing"
            start_time = clock()
            loop_future = pool.submit(loop.run)

            # 1. First completion attempt encounters 409 APPROVED_ACTION_IN_FLIGHT
            assert first_refused.wait(5), "First complete attempt was not refused"

            # 2. Let execution finish and enter receipt persistence where SQLite write lock is held
            release_execution.set()
            assert persisting.wait(5), "Receipt persistence never acquired lock"
            return_first_refusal.set()

            # 3. Second complete request arrives and waits for the SQLite write lock
            assert second_entered.wait(5), "Second complete attempt never entered service"

            # Verify the second request started within the deadline
            elapsed_at_second = clock() - start_time
            assert elapsed_at_second < wall_clock, f"Second request dispatched after deadline: {elapsed_at_second}s"

            # The real SQLite lock spans exactly the refreshed budget or longer.
            # Both waits remain shorter than the stale initial budget.
            clock.advance(lock_wait)
            assert not loop_future.done(), "Loop terminated prematurely before lock release"
        finally:
            return_first_refusal.set()
            release_execution.set()
            release_receipt.set()

        review_resp = review_future.result(timeout=10)
        assert review_resp.status_code == 200, f"Approval resolution failed: {review_resp.text}"
        result = loop_future.result(timeout=10)

    # Assertions
    assert complete_post_calls >= 2, f"Expected >= 2 complete POSTs, got {complete_post_calls}"
    assert len(sent_budgets) >= 2, f"Expected >= 2 sent budgets, got {sent_budgets}"
    assert sent_budgets[:2] == pytest.approx([0.55, 0.25])
    assert lock_wait >= 0.25
    assert lock_wait < sent_budgets[0]
    assert completion_errors == ["APPROVED_ACTION_IN_FLIGHT", "COMPLETION_DEADLINE_EXCEEDED"]
    assert sent_budgets[1] < sent_budgets[0], (
        f"Deadline budget was not refreshed on retry: initial={sent_budgets[0]}, retry={sent_budgets[1]}"
    )

    assert result.status == "FAILED", f"Expected FAILED result, got {result.status}"
    stored_run = client.get(f"/api/v1/runs/{run_id}").json()
    assert stored_run["status"] == "FAILED", f"Stored run status is {stored_run['status']}"

    events = client.get(f"/api/v1/runs/{run_id}/events").json()
    event_types = [e["event_type"] for e in events]
    assert "RUN_COMPLETED" not in event_types, f"RUN_COMPLETED event found: {event_types}"
    assert "SYSTEM_ERROR" in event_types, f"Expected SYSTEM_ERROR event: {event_types}"
